"""分析任务的注册表与执行。

**任务在内存里，不持久化。** 分析结果的持久化载体是向量索引（U9 的缓存），而任务状态是
过程信息——重启后未完成的任务无法恢复（图的 checkpointer 也是内存实现），把它写盘会造出
一批永远停在中途的僵尸任务。二次提交同一仓库会命中索引缓存并秒级完成，所以重启的代价
不是「重跑分析」而是「重新提交一次」。

**进度队列与结果分离。** SSE 消费队列，读结果端点看任务记录。分开的理由是生命周期不同：
队列在 SSE 断连后就无人消费（前端可能刷新页面），而结果要在分析完成后长期可读。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from backend.api.progress import ProgressEvent, ProgressTracker, Stage, terminal_event
from backend.config import Settings
from backend.graph.builder import build_analysis_graph
from backend.graph.real_nodes import make_real_nodes
from backend.ingest.guards import RepoRejected, parse_repo_url
from backend.providers.llm import LLMProvider

logger = logging.getLogger("codepilot.tasks")

# 单个任务的进度事件缓存上限。
#
# 有上限的理由：SSE 客户端可能一直不连接（用户提交后关了页面），而扇出会持续产出事件。
# 无界队列在那种情况下会一直涨。超出即丢弃最旧的——进度是时序信息，旧事件的价值远低于新的。
MAX_BUFFERED_EVENTS = 200


@dataclass
class AnalysisTask:
    """一次分析任务的全部状态。"""

    task_id: str
    repo_url: str
    repo: str
    stage: Stage = Stage.QUEUED
    completed: bool = False
    failed: bool = False
    error: str = ""
    final_state: dict[str, Any] = field(default_factory=dict)

    # 已产出的事件（供后连接的 SSE 客户端补看）与实时队列。
    history: list[ProgressEvent] = field(default_factory=list)
    subscribers: list[asyncio.Queue[ProgressEvent | None]] = field(default_factory=list)

    def publish(self, event: ProgressEvent) -> None:
        """记录并广播一条事件。"""
        self.stage = event.stage
        self.history.append(event)
        if len(self.history) > MAX_BUFFERED_EVENTS:
            del self.history[: len(self.history) - MAX_BUFFERED_EVENTS]
        for queue in self.subscribers:
            queue.put_nowait(event)

    def finish(self, event: ProgressEvent) -> None:
        """终止：广播最后一条事件并给每个订阅者发结束哨兵。"""
        self.publish(event)
        for queue in self.subscribers:
            # None 是结束哨兵。用它而非关闭队列——asyncio.Queue 没有 close，
            # 而让消费者靠超时判断结束会让正常结束也要等一个超时周期。
            queue.put_nowait(None)

    def subscribe(self) -> asyncio.Queue[ProgressEvent | None]:
        """新订阅者。已完成的任务立刻收到历史与哨兵，不必特殊处理。"""
        queue: asyncio.Queue[ProgressEvent | None] = asyncio.Queue()
        for event in self.history:
            queue.put_nowait(event)
        if self.completed or self.failed:
            queue.put_nowait(None)
        else:
            self.subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[ProgressEvent | None]) -> None:
        if queue in self.subscribers:
            self.subscribers.remove(queue)


class TaskRegistry:
    """任务的创建、查询与后台执行。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tasks: dict[str, AnalysisTask] = {}
        self._runners: dict[str, asyncio.Task[None]] = {}

    def get(self, task_id: str) -> AnalysisTask | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[AnalysisTask]:
        return list(self._tasks.values())

    def create(self, repo_url: str) -> AnalysisTask:
        """建任务。地址非法时抛 RepoRejected，由路由层转成可区分错误（AE6）。

        地址解析放在这里而非后台：非法地址应当在提交时就返回 400，而不是先返回 202
        再让用户去拉进度才发现失败。
        """
        ref = parse_repo_url(repo_url)
        task = AnalysisTask(
            task_id=uuid.uuid4().hex[:16], repo_url=repo_url, repo=ref.slug
        )
        self._tasks[task.task_id] = task
        return task

    def start(self, task: AnalysisTask) -> None:
        """把分析放到后台跑。

        不 await：分析是分钟级的，提交端点必须立刻返回（202 语义）。
        """
        self._runners[task.task_id] = asyncio.create_task(self._run(task))

    async def _run(self, task: AnalysisTask) -> None:
        tracker = ProgressTracker()
        provider = LLMProvider(self._settings) if self._settings.deepseek_api_key else None
        nodes = make_real_nodes(self._settings, provider)
        graph = build_analysis_graph(nodes)

        try:
            async for chunk in graph.astream(
                {"repo_url": task.repo_url},
                {"configurable": {"thread_id": task.task_id}},
                stream_mode="updates",
            ):
                for node, delta in chunk.items():
                    if not isinstance(delta, dict):
                        continue
                    for event in tracker.observe(node, delta):
                        task.publish(event)
                    task.final_state.update(delta)

        except RepoRejected as exc:
            # 准入与克隆的失败：AE6 要求可区分，reason 由路由层读取。
            task.failed = True
            task.error = str(exc)
            task.completed = True
            task.finish(terminal_event(failed=True, detail=str(exc)))
            logger.info("任务 %s 因准入失败终止：%s", task.task_id, exc)
            return
        except Exception as exc:  # noqa: BLE001 — 任何失败都要让任务收敛到终态
            task.failed = True
            task.error = f"{type(exc).__name__}: {exc}"
            task.completed = True
            task.finish(terminal_event(failed=True, detail=task.error))
            logger.exception("任务 %s 执行失败", task.task_id)
            return

        task.completed = True
        task.finish(terminal_event(failed=False, detail="分析完成"))

    async def shutdown(self) -> None:
        """取消未完成的任务。应用关闭时调用，避免留下悬挂的协程。"""
        for runner in self._runners.values():
            if not runner.done():
                runner.cancel()
        for runner in self._runners.values():
            try:
                await runner
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
