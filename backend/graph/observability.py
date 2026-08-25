"""节点观测：耗时、输入输出摘要、失败记录。

两层，各自独立可用：

  1. **结构化日志**（始终生效）。每个节点执行完写一条含节点名、耗时、摘要的记录，
     同时把同样内容作为 NodeEvent 写进 state——后者供 U12 的 SSE 进度推送复用，
     不需要另建一套进度上报机制。

  2. **LangSmith**（环境变量开关）。开时把 API key 与项目名写入 langchain 约定的
     环境变量，由 langchain 自身的 tracer 接管。关时什么都不做。

为什么日志层不做成「LangSmith 不可用时的降级」而是始终生效：开发期看执行顺序与耗时
不该依赖外部服务可达，而 SSE 进度也不能建立在 LangSmith 上。

**节点内不放非幂等副作用。** checkpoint 恢复时节点从函数头重跑，副作用会重复执行。
这里的日志与计时是幂等的（重跑多打一条记录，无状态污染），但真实节点里的写文件、
发请求、扣费必须自己做幂等保护。
"""

from __future__ import annotations

import inspect
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar

from backend.config import Settings
from backend.graph.state import NodeEvent, NodeFailure

logger = logging.getLogger("codepilot.graph")

# 装饰器要保留节点的输入类型，否则被包装后签名退化成 Any，langgraph 的 add_node
# 就无法校验「这个节点吃的是完整 state 还是扇出分片」。
NodeInputT = TypeVar("NodeInputT")
NodeInputT_contra = TypeVar("NodeInputT_contra", contravariant=True)

# 装饰器保持传入类型不变——它对同步与异步节点都成立，而 overload 无法按参数区分
# 两者（签名相同，只有返回类型不同）。
NodeT = TypeVar("NodeT")


class GraphNode(Protocol[NodeInputT_contra]):
    """同步节点的调用形状。

    为什么用 Protocol 而不是 `Callable[[X], dict]`：langgraph 的 `add_node` 接受的是
    参数名为 `state` 的 Protocol，而 mypy 把 `Callable[[X], Y]` 当作仅位置参数的函数
    ——它不能按 `state=...` 调用，因此不满足那个协议。用同形状的 Protocol 标注，类型
    检查才能真正校验节点接线，而不是靠 ignore 掩掉。
    """

    def __call__(self, state: NodeInputT_contra) -> dict[str, Any]: ...


class AsyncGraphNode(Protocol[NodeInputT_contra]):
    """异步节点的调用形状。

    生产节点是 async（ReAct 循环与 LLM 调用本就异步），stub 节点是同步。两种形状都要
    支持：langgraph 两者都接受，而把 stub 改成 async 只会让它们的测试凭空多一层
    await——那套 stub 的价值恰在于确定性与简单。
    """

    def __call__(self, state: NodeInputT_contra) -> Awaitable[dict[str, Any]]: ...


# 节点可以是同步或异步。builder 与 NodeSet 用这个别名，两种实现都能装进去。
AnyGraphNode = GraphNode[NodeInputT_contra] | AsyncGraphNode[NodeInputT_contra]


def configure_tracing(settings: Settings) -> bool:
    """按配置开启 LangSmith。返回是否已开启。

    langchain 的 tracer 读环境变量而非显式参数，故这里只做环境变量注入。缺 key 时
    即使开关为真也不开——否则 langchain 会在每次调用时报认证错误，噪声盖过有用输出。
    """
    if not settings.langsmith_tracing or not settings.langsmith_api_key:
        # 显式关掉：避免进程环境里残留的开关意外生效，让「配置关闭」名副其实。
        os.environ.pop("LANGSMITH_TRACING", None)
        os.environ.pop("LANGCHAIN_TRACING_V2", None)
        return False

    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    return True


def summarize(value: object) -> str:
    """把节点产出压成一行摘要。

    只读长度与键名，不读内容：state 里有整份符号表与依赖图，把它们塞进日志会让单条
    记录到几十 KB，反而看不清执行流。
    """
    if isinstance(value, dict):
        parts = []
        for key, item in sorted(value.items()):
            if isinstance(item, (list, tuple, dict, set)):
                parts.append(f"{key}={len(item)}")
            elif isinstance(item, str):
                parts.append(f"{key}={len(item)}c" if len(item) > 40 else f"{key}={item}")
            else:
                parts.append(f"{key}={type(item).__name__}")
        return " ".join(parts) or "(空)"
    return type(value).__name__


def _failure_payload(
    name: str, scope: str, exc: Exception, elapsed_ms: int
) -> dict[str, Any]:
    logger.warning(
        "节点 %s 失败 scope=%s %dms: %s: %s",
        name,
        scope or "-",
        elapsed_ms,
        type(exc).__name__,
        exc,
    )
    return {
        "module_failures": [
            NodeFailure(node=name, scope=scope, error=f"{type(exc).__name__}: {exc}")
        ],
        "events": [
            NodeEvent(
                node=name,
                duration_ms=elapsed_ms,
                summary=f"失败：{type(exc).__name__}",
                failed=True,
            )
        ],
    }


def _success_payload(name: str, result: dict[str, Any], elapsed_ms: int) -> dict[str, Any]:
    summary = summarize(result)
    logger.info("节点 %s %dms %s", name, elapsed_ms, summary)
    events = [NodeEvent(node=name, duration_ms=elapsed_ms, summary=summary)]
    # 节点自己也可能写 events（真实节点里的中间进度）。合并而非覆盖。
    return {**result, "events": [*result.get("events", []), *events]}


def observed(
    name: str, scope_key: str | None = None, fatal: bool = False
) -> Callable[[NodeT], NodeT]:
    """把节点函数包成「记录耗时 + 兜住异常」的形式。同步与异步节点都支持。

    异常兜底放在这里而不是各节点内部，理由是实测行为：节点抛异常会中断整图，扇出中
    一个模块失败会让整次分析失败。统一在装饰器里转成 NodeFailure，各节点就不必重复
    写 try/except，也不会漏写一处就让 R10 失效。

    **fatal=True 的节点在记录失败后重新抛出。** 兜住所有异常在管道前缀上是错的：ingest
    失败意味着没有 workdir，parse 随之 KeyError，cluster 再失败……每个节点依次静默失败，
    任务最终报告「完成」加一份空报告，而真正的原因（仓库不存在）被埋在事件流里。AE6 要求
    准入失败可区分，所以那类节点的失败必须终止整次分析。

    分支节点（module_agent、reviewer）保持兜住：单模块失败不影响其余模块是 R10 的要求。

    scope_key 指出失败记录里该写哪个对象——扇出节点的输入是模块分片，失败时要能说清
    是哪个模块失败，而不只是「module_agent 失败了」。

    按被装饰函数是否为协程函数分派：生产节点是 async，stub 节点是同步。让 stub 也改成
    async 只会给它们的测试凭空加一层 await，而那套 stub 的价值恰在确定性与简单。
    """

    def decorate(fn: Any) -> Any:
        if inspect.iscoroutinefunction(fn):

            async def wrapped_async(state: Any) -> dict[str, Any]:
                started = time.perf_counter()
                try:
                    result = await fn(state)
                except Exception as exc:  # noqa: BLE001 — 见上方说明
                    payload = _failure_payload(
                        name,
                        _extract_scope(state, scope_key),
                        exc,
                        int((time.perf_counter() - started) * 1000),
                    )
                    if fatal:
                        raise
                    return payload
                return _success_payload(
                    name, result, int((time.perf_counter() - started) * 1000)
                )

            wrapped_async.__name__ = f"observed_{name}"
            return wrapped_async

        def wrapped(state: Any) -> dict[str, Any]:
            started = time.perf_counter()
            try:
                result = fn(state)
            except Exception as exc:  # noqa: BLE001 — 见上方说明
                payload = _failure_payload(
                    name,
                    _extract_scope(state, scope_key),
                    exc,
                    int((time.perf_counter() - started) * 1000),
                )
                if fatal:
                    raise
                return payload
            return _success_payload(name, result, int((time.perf_counter() - started) * 1000))

        wrapped.__name__ = f"observed_{name}"
        return wrapped

    return decorate


def _extract_scope(state: object, scope_key: str | None) -> str:
    if scope_key is None or not isinstance(state, dict):
        return ""
    value = state.get(scope_key)
    # 扇出分片里 scope 常是 dataclass（Module），取其 name 比 repr 可读。
    return str(getattr(value, "name", value) or "")
