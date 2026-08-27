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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from time import time
from typing import Any

from backend.api.gate import ConcurrencyGate, QueueFull
from backend.api.progress import ProgressEvent, ProgressTracker, Stage, terminal_event
from backend.config import Settings
from backend.graph.builder import build_analysis_graph
from backend.graph.real_nodes import make_real_nodes
from backend.ingest.guards import RejectReason, RepoRejected, parse_repo_url
from backend.providers.llm import GuestCredentials, LLMProvider
from backend.workspace.quota import touch_repo

logger = logging.getLogger("codepilot.tasks")

# 单个任务的进度事件缓存上限。
#
# 有上限的理由：SSE 客户端可能一直不连接（用户提交后关了页面），而扇出会持续产出事件。
# 无界队列在那种情况下会一直涨。超出即丢弃最旧的——进度是时序信息，旧事件的价值远低于新的。
MAX_BUFFERED_EVENTS = 200

# 累积语义的 state 字段。与 AnalysisState 里带 `Annotated[..., operator.add]` 的字段一致。
#
# **为什么必须在这里单独处理。** `graph.astream(stream_mode="updates")` 逐节点吐增量，而
# 扇出的每一路 module_agent 各吐一条 `{"module_analyses": [一个]}`。用 dict.update 合并
# 就是逐条覆盖——9 个模块跑完，final_state 里只剩最后一个。reducer 只作用于图内部的
# state，不会替流式消费方做累积。
#
# 这个缺陷的表现极不显眼：模块数少时两个键各出现一次，看起来一切正常（`module_analyses`
# 与 `module_failures` 是不同的键，互不覆盖）；只有同一个键被多路写入时才丢数据，而丢的
# 是「除最后一路以外的全部」，界面上表现为「模块详情大多是空的」而非报错。
_ACCUMULATING_KEYS = frozenset({"module_analyses", "module_failures", "events"})


def merge_delta(final_state: dict[str, Any], delta: dict[str, Any]) -> None:
    """把一条节点增量并进任务的最终 state。累积字段追加，其余覆盖。"""
    for key, value in delta.items():
        if key in _ACCUMULATING_KEYS and isinstance(value, list):
            existing = final_state.get(key)
            if isinstance(existing, list):
                existing.extend(value)
            else:
                # 复制而非直接持有：增量里的列表由图持有，就地扩展会污染图的 state。
                final_state[key] = list(value)
        else:
            final_state[key] = value


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

    # 提交时刻（wall clock 秒）。历史列表按它倒序（R-45），所以用真实时间而非 monotonic。
    created_at: float = field(default_factory=time)

    # 提交方标识（限流用的那个键）。网络类失败要按它退还配额，见 _refund_quota。
    # 不存 IP 明文以外的东西：这个值本就是限流器的键，不引入新的可识别信息。
    client_key: str = ""

    # 排队位置（1-based）。0 表示不在队列中——已启动或已终止。
    #
    # 存在任务上而非只存在闸门里：读结果端点要能给出这个数字（R-54 要求显示排队位置），
    # 而那时闸门可能已经放行了它。放行时置 0，两处状态因此不会不一致。
    queue_position: int = 0

    @property
    def queued(self) -> bool:
        """是否还在排队（尚未开跑）。排队中的任务不进历史列表（R-46 的延伸）。"""
        return self.queue_position > 0 and not self.completed and not self.failed

    # 访客凭证。任务持有期驻内存，不进 final_state（那是要落盘的）。
    #
    # `repr=False` 是必需的：dataclass 默认的 __repr__ 会把全部字段拼进字符串，而任务
    # 对象会出现在异常回溯、调试输出与第三方库的错误信息里——那些路径不经过 logger，
    # 审查 `logger.*` 调用发现不了它们。
    credentials: GuestCredentials | None = field(default=None, repr=False)

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


@dataclass(frozen=True)
class TaskContext:
    """文件与检索端点需要的最小任务事实。

    **为什么不直接传 `AnalysisTask`。** 重启后任务只存在于落盘历史里，那时没有 `AnalysisTask`
    可传——但分析确实发生过、结果确实可读。直接传任务对象会让这两个端点只能回答
    「任务不存在」，而用户此刻正看着那份报告（KTD10 的第四态在实现上就失守于此）。

    `workdir` 为空表示工作副本不可用：可能还没克隆完，也可能被配额清理或重启后不在了。
    两者由 `from_disk` 区分——前者是「等一下」，后者是「代码副本已清理」。
    """

    task_id: str
    repo_slug: str
    commit_sha: str
    workdir: str
    completed: bool
    stage: str
    from_disk: bool = False


class TaskRegistry:
    """任务的创建、查询与后台执行。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tasks: dict[str, AnalysisTask] = {}
        self._runners: dict[str, asyncio.Task[None]] = {}
        self._gate = ConcurrencyGate(
            max_concurrent=settings.max_concurrent_analyses,
            max_queued=settings.max_queued_analyses,
        )
        # 关停中不再放行排队任务。见 shutdown 的说明。
        self._shutting_down = False
        # 分析完成时的落盘回调。由 routes 注入，见 _persist 的说明。
        self._on_persist: Callable[[AnalysisTask], None] | None = None
        # 网络类失败时退还限流配额的回调。同样由 routes 注入——限流器归路由层持有，
        # 注册表不该知道它的存在，只知道「这次失败该退配额」。
        self._on_refund: Callable[[str], object] | None = None

    def set_persist_hook(self, hook: Callable[[AnalysisTask], None]) -> None:
        """注册落盘回调（U21）。不注册即不落盘——P1 尾部可整段不做。"""
        self._on_persist = hook

    def set_refund_hook(self, hook: Callable[[str], object]) -> None:
        """注册限流配额退还回调。不注册则不退（例如直接构造 registry 的测试）。"""
        self._on_refund = hook

    def _refund_quota(self, task: AnalysisTask) -> None:
        """退还该任务占用的限流配额。失败只记日志——退不掉不该再制造一个错误。"""
        if self._on_refund is None or not task.client_key:
            return
        try:
            self._on_refund(task.client_key)
            logger.info("任务 %s 因网络失败退还限流配额", task.task_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("退还限流配额失败：%s", exc)

    @property
    def gate(self) -> ConcurrencyGate:
        """闸门。`/api/health` 读它的在途数与队列长度（KTD11 的信号）。"""
        return self._gate

    def get(self, task_id: str) -> AnalysisTask | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[AnalysisTask]:
        return list(self._tasks.values())

    def running_repos(self) -> set[str]:
        """在跑任务的仓库工作副本路径。配额清理据此排除在跑仓库（U7）。

        取 state 里的 workdir 而非按 slug 推算路径：推算要复制一遍命名规则，而那条规则
        在 ingest 节点里，两处一旦漂移就会清掉正在分析的副本——那正是本方法要防的事。
        """
        paths: set[str] = set()
        for task in self._tasks.values():
            if task.completed or task.failed:
                continue
            workdir = str(task.final_state.get("workdir", "")).strip()
            if workdir:
                paths.add(workdir)
        return paths

    def discard(self, task: AnalysisTask) -> None:
        """撤销一个尚未启动的任务记录。

        **为什么需要它。** `create` 之后还有两道门（磁盘配额、队列满），它们拒绝时任务已经
        在注册表里了。不撤销的话会留下一个永停在 QUEUED 的僵尸任务——它会出现在历史列表里
        且看起来可以恢复，而它从未启动过（违反 R-46）。

        只对未启动的任务生效：已经在跑的任务要走正常的终止路径，那里有闸门名额要归还。
        """
        if task.task_id in self._runners:
            return
        self._tasks.pop(task.task_id, None)

    def create(
        self,
        repo_url: str,
        credentials: GuestCredentials | None = None,
        client_key: str = "",
    ) -> AnalysisTask:
        """建任务。地址非法时抛 RepoRejected，由路由层转成可区分错误（AE6）。

        地址解析放在这里而非后台：非法地址应当在提交时就返回 400，而不是先返回 202
        再让用户去拉进度才发现失败。
        """
        ref = parse_repo_url(repo_url)
        task = AnalysisTask(
            task_id=uuid.uuid4().hex[:16],
            repo_url=repo_url,
            repo=ref.slug,
            credentials=credentials,
            client_key=client_key,
        )
        self._tasks[task.task_id] = task
        return task

    def start(self, task: AnalysisTask) -> None:
        """把分析放到后台跑，或在闸门满时排队（R-54）。

        不 await：分析是分钟级的，提交端点必须立刻返回（202 语义）。

        抛 QueueFull 时调用方转成可区分的拒绝——队列也满意味着系统确实接不下了，
        这时排队位置本身已无意义。
        """
        admission = self._gate.acquire(task.task_id)
        if not admission.admitted:
            task.queue_position = admission.position
            logger.info(
                "任务 %s 进入排队，位置 %d（在跑 %d/%d）",
                task.task_id,
                admission.position,
                self._gate.running_count,
                self._gate.max_concurrent,
            )
            return
        self._launch(task)

    def _launch(self, task: AnalysisTask) -> None:
        """真正起协程。已持有闸门名额时调用。"""
        task.queue_position = 0
        self._runners[task.task_id] = asyncio.create_task(self._run(task))

    def _release_and_promote(self, task_id: str) -> None:
        """归还名额并放行队首。

        **在任务成功与失败两条路径上都必须执行**，否则失败的任务永久占住名额、队列不再
        前进。放在 _run 的 finally 里就是为此。
        """
        promoted_id = self._gate.release(task_id)
        # 放行后其余排队任务各前移一位，位置要跟着刷新——否则界面上的「第 3 位」会一直
        # 停在 3，用户看不到队列在动。
        for index, queued_id in enumerate(self._gate.queued_ids(), start=1):
            queued = self._tasks.get(queued_id)
            if queued is not None:
                queued.queue_position = index

        if promoted_id is None or self._shutting_down:
            return
        promoted = self._tasks.get(promoted_id)
        if promoted is None:
            # 任务记录已不在（不该发生）。名额已归还，把它让给下一个而非空占。
            self._release_and_promote(promoted_id)
            return
        logger.info("放行排队任务 %s", promoted_id)
        self._launch(promoted)

    async def _run(self, task: AnalysisTask) -> None:
        try:
            await self._execute(task)
        finally:
            self._release_and_promote(task.task_id)

    async def _execute(self, task: AnalysisTask) -> None:
        tracker = ProgressTracker()
        # 凭证只在构造 provider 时用一次，不进 state、不进日志（BR-004）。
        #
        # 访客凭证缺失时 provider 为 None：不回落服务端凭证（NA-03）。提交端点已经拦住
        # 了这种情形，这里是第二道——直接调 registry 的测试与将来的其它调用方绕不过它。
        provider = (
            LLMProvider(self._settings, credentials=task.credentials)
            if task.credentials and task.credentials.api_key
            else None
        )
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
                    merge_delta(task.final_state, delta)

        except RepoRejected as exc:
            # 准入与克隆的失败：AE6 要求可区分，reason 由路由层读取。
            task.failed = True
            task.error = str(exc)
            task.completed = True
            # 网络类失败退还限流配额：那是我们这侧的问题，不该扣用户的额度。
            # 克隆已经重试过（见 ingest/clone.py 的 CLONE_MAX_ATTEMPTS），走到这里说明
            # 重试也没救回来——用户此刻最需要的是「能立刻再试一次」而不是被锁一小时。
            if exc.reason is RejectReason.NETWORK_ERROR:
                self._refund_quota(task)
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
        # 刷新工作副本的 mtime，让它在配额清理的 LRU 排序里靠后（U7）。不刷的话一个被
        # 反复分析的仓库会因 mtime 停在首次克隆时刻而最先被删——那恰是最该留的那个。
        workdir = str(task.final_state.get("workdir", "")).strip()
        if workdir:
            touch_repo(Path(workdir))
        self._persist(task)
        task.finish(terminal_event(failed=False, detail="分析完成"))

    def _persist(self, task: AnalysisTask) -> None:
        """落盘已完成的分析（U21）。

        持久化失败只记 warning，不影响任务的完成状态——一次已经产出报告的分析不该因为写不
        进磁盘而变成失败。

        `on_persist` 由 routes 注入（它持有 state → ResultResponse 的映射）。注册表不自己
        做那份映射：那会让同一份映射存在两处，而两处漂移的表现是「落盘的字段比接口少」。
        """
        if self._on_persist is None:
            return
        try:
            self._on_persist(task)
        except Exception as exc:  # noqa: BLE001 — 落盘失败不能拖垮任务
            logger.warning("落盘任务 %s 失败：%s", task.task_id, exc)

    async def shutdown(self) -> None:
        """取消未完成的任务。应用关闭时调用，避免留下悬挂的协程。

        **先置关停标记再取消。** 被取消的任务会走 `_run` 的 finally 归还名额，而归还会
        放行队首——那等于在关停过程中启动新分析，既拖长关停又向正在迭代的 `_runners`
        插入条目（实测：`RuntimeError: dictionary changed size during iteration`）。

        迭代快照而非原字典：即便标记生效，也不让「关停期间字典是否会变」成为一个需要
        推理的前提。
        """
        self._shutting_down = True
        runners = list(self._runners.values())
        for runner in runners:
            if not runner.done():
                runner.cancel()
        for runner in runners:
            try:
                await runner
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
