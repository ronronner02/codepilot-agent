"""进度事件：从图节点到 SSE 流（KTD8）。

**传递机制用 LangGraph 的 astream，不在节点里铺队列。** 实测确认
`astream(stream_mode="updates")` 每个 superstep 吐出 `{节点名: state 增量}`，而增量里已经
含 `observed` 装饰器写的 NodeEvent。所以进度不需要改任何节点——在节点里传递队列会让每个
节点签名多一个参数，而那种改动最容易漏掉一处，漏掉的那个节点就在进度里静默消失。

**阶段名是给人看的，不是节点名。** `chunk_and_index`、`synthesize` 对用户无意义。映射到
「建立索引」、「汇总报告」这类描述，而百分比按阶段权重算。

权重不等分：模块分析占的时间远超其余阶段之和（每个模块一次 ReAct 循环，实测单模块几十秒
到几分钟）。等分权重会让进度条在 40% 处停很久，那比没有进度条更让人怀疑是不是卡死了。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("codepilot.progress")


class Stage(str, Enum):
    """用户可见的分析阶段。顺序即执行顺序。"""

    QUEUED = "queued"
    FETCHING = "fetching"
    PARSING = "parsing"
    SKELETON_READY = "skeleton_ready"
    PLANNING = "planning"
    ANALYZING_MODULES = "analyzing_modules"
    INDEXING = "indexing"
    REVIEWING = "reviewing"
    REPORT_READY = "report_ready"
    DONE = "done"
    FAILED = "failed"


# 阶段的中文标签。SSE 事件直接带上，前端不必再维护一份映射——两份映射会漂移，
# 而漂移的表现是界面显示英文枚举名。
STAGE_LABELS: dict[Stage, str] = {
    Stage.QUEUED: "排队中",
    Stage.FETCHING: "拉取仓库",
    Stage.PARSING: "解析代码",
    Stage.SKELETON_READY: "结构骨架就绪",
    Stage.PLANNING: "规划深挖范围",
    Stage.ANALYZING_MODULES: "分析模块",
    Stage.INDEXING: "建立向量索引",
    Stage.REVIEWING: "代码评审",
    Stage.REPORT_READY: "汇总报告",
    Stage.DONE: "完成",
    Stage.FAILED: "失败",
}

# 节点名到阶段。
#
# select_files 与 reviewer 都归到「代码评审」：前者是后者的准备步骤，对用户是一件事。
_NODE_STAGES: dict[str, Stage] = {
    "ingest": Stage.FETCHING,
    "parse": Stage.PARSING,
    "cluster": Stage.SKELETON_READY,
    "planner": Stage.PLANNING,
    "module_agent": Stage.ANALYZING_MODULES,
    "chunk_and_index": Stage.INDEXING,
    "select_files": Stage.REVIEWING,
    "reviewer": Stage.REVIEWING,
    "synthesize": Stage.REPORT_READY,
}

# 各阶段完成后的累计进度。
#
# 模块分析占 30%-75%（45 个百分点）的理由：它是唯一随仓库规模线性增长的阶段，实测单模块
# 几十秒到几分钟，12 个模块就是全程的绝大部分。其余阶段各几秒到几十秒。
_STAGE_PROGRESS: dict[Stage, int] = {
    Stage.QUEUED: 0,
    Stage.FETCHING: 10,
    Stage.PARSING: 20,
    Stage.SKELETON_READY: 30,
    Stage.PLANNING: 35,
    Stage.ANALYZING_MODULES: 75,
    Stage.INDEXING: 82,
    Stage.REVIEWING: 90,
    Stage.REPORT_READY: 98,
    Stage.DONE: 100,
}

_MODULE_STAGE_START = _STAGE_PROGRESS[Stage.PLANNING]
_MODULE_STAGE_END = _STAGE_PROGRESS[Stage.ANALYZING_MODULES]


@dataclass(frozen=True)
class ProgressEvent:
    """一条进度事件。

    percent 可选（计划：「事件含阶段名与可选百分比」）：阶段边界能给出确切进度，而阶段
    内部的推进未必可量化。给不出时留 None，前端显示不确定态而非假装精确。
    """

    stage: Stage
    label: str
    detail: str = ""
    percent: int | None = None
    failed: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "stage": self.stage.value,
            "label": self.label,
            "detail": self.detail,
            "percent": self.percent,
            "failed": self.failed,
        }


@dataclass
class ProgressTracker:
    """把节点增量翻译成进度事件。

    有状态，两个原因：

    模块分析要按「已完成 N 个 / 共 M 个」算进度，而 M 只有在 planner 产出后才知道。

    **百分比必须单调。** 同一 superstep 内多个节点并发完成，顺序不跟随阶段权重，直接用
    权重会让进度条回退。回退比不动更让人怀疑出了问题，所以这里记住已发布的最高值，低于
    它的一律抬平。

    两种排布下都会回退，只是回退处不同：索引挂在 cluster 之后时实测顺序为
    indexing(82) → reviewing(90) → planning(35) → analyzing_modules（90% 退回 35%）；
    索引改挂 planner 之后（当前排布）则是 planning(35) → reviewing(90) →
    analyzing_modules(35~75 插值) → indexing(82) → report_ready(98)，回退发生在
    reviewing 之后。抬平对两者都成立。

    **索引现在是最后完成的长尾，而它的耗时不可预估**——未命中缓存时是分钟到半小时级
    （fastapi 实测 30.5 分钟），命中时是秒级。所以 INDEXING 的固定权重（82）在未命中时
    偏低：进度会停在 reviewing 抬平后的 90% 等很久。要修得让权重随缓存命中与否变化，
    那是另一件事（进度权重的动态化），不在本次三项修复内。

    抬平而非重排事件：事件的时序是真实的（那个节点确实那时完成的），只有百分比这个
    概括量需要单调。丢弃或缓存事件会让详情信息延迟到达。
    """

    planned_modules: int = 0
    completed_modules: int = 0
    seen_stages: set[Stage] = field(default_factory=set)
    highest_percent: int = 0

    def observe(self, node: str, delta: dict[str, object]) -> list[ProgressEvent]:
        """处理一个节点的 state 增量，产出零或多条事件。"""
        stage = _NODE_STAGES.get(node)
        if stage is None:
            return []

        if node == "planner":
            planned = delta.get("planned_modules")
            self.planned_modules = len(planned) if isinstance(planned, list) else 0

        if node == "module_agent":
            return [self._module_event(delta)]

        self.seen_stages.add(stage)
        return [
            ProgressEvent(
                stage=stage,
                label=STAGE_LABELS[stage],
                detail=self._detail_for(node, delta),
                percent=self._monotonic(_STAGE_PROGRESS.get(stage)),
                failed=_delta_failed(delta),
            )
        ]

    def _monotonic(self, percent: int | None) -> int | None:
        """抬平到已发布的最高值，并记录新高。"""
        if percent is None:
            return None
        self.highest_percent = max(self.highest_percent, percent)
        return self.highest_percent

    def _module_event(self, delta: dict[str, object]) -> ProgressEvent:
        """模块分析的单次完成事件。

        扇出的每个模块各自产出一次增量，所以这里被调用 N 次。进度在 PLANNING 与
        ANALYZING_MODULES 之间按完成比例插值。
        """
        self.completed_modules += 1
        self.seen_stages.add(Stage.ANALYZING_MODULES)

        total = max(self.planned_modules, self.completed_modules)
        span = _MODULE_STAGE_END - _MODULE_STAGE_START
        raw = _MODULE_STAGE_START + int(span * self.completed_modules / max(total, 1))
        percent = self._monotonic(raw)

        failed = _delta_failed(delta)
        analyses = delta.get("module_analyses")
        name = ""
        if isinstance(analyses, list) and analyses:
            name = getattr(analyses[0], "module_name", "")
        if not name:
            failures = delta.get("module_failures")
            if isinstance(failures, list) and failures:
                name = getattr(failures[0], "scope", "")

        suffix = "分析失败" if failed else "分析完成"
        detail = (
            f"{name} {suffix}（{self.completed_modules}/{total}）"
            if name
            else f"已完成 {self.completed_modules}/{total} 个模块"
        )
        return ProgressEvent(
            stage=Stage.ANALYZING_MODULES,
            label=STAGE_LABELS[Stage.ANALYZING_MODULES],
            detail=detail,
            percent=percent,
            failed=failed,
        )

    def _detail_for(self, node: str, delta: dict[str, object]) -> str:
        """阶段的具体说明。

        取已有的产出字段而非另写描述：`index_note` 已经说明了缓存命中与否，
        `plan_rationale` 已经说明了挑了几个模块——重写一遍会与它们漂移。
        """
        if node == "parse":
            outcome = delta.get("parse_outcome")
            parsed = len(getattr(outcome, "parsed", []) or [])
            unparsed = len(getattr(outcome, "unparsed", []) or [])
            return f"符号级解析 {parsed} 个文件，未解析 {unparsed} 个"
        if node == "cluster":
            modules = delta.get("modules")
            graph = delta.get("dependency_graph")
            edges = sum(len(t) for t in getattr(graph, "edges", {}).values()) if graph else 0
            count = len(modules) if isinstance(modules, list) else 0
            return f"{count} 个模块，{edges} 条依赖边"
        if node == "planner":
            return f"选定 {self.planned_modules} 个模块深挖"
        if node == "chunk_and_index":
            note = delta.get("index_note")
            return str(note)[:160] if note else ""
        if node == "select_files":
            targets = delta.get("review_targets")
            return f"挑选 {len(targets)} 个文件待检" if isinstance(targets, list) else ""
        if node == "reviewer":
            findings = delta.get("findings")
            return f"{len(findings)} 条发现" if isinstance(findings, list) else ""
        if node == "synthesize":
            summary = delta.get("report_validation_summary")
            return str(summary)[:160] if summary else ""
        return ""


def _delta_failed(delta: dict[str, object]) -> bool:
    """增量里是否表示失败。

    两个来源都要看：

      events 里的 failed 标记——observed 装饰器捕获异常时打的。
      本次增量自带的 module_failures——节点主动把失败作为数据返回时（analyze_module 在
      LLM 调用失败时正是这样做的，它不抛异常，见 R10 的要求）。

    只看 events 会漏掉后者，表现为「模块失败了但进度显示成功」。

    看的是**本次增量**里的 module_failures，而非累积后的 state 字段：那个字段带 reducer，
    扇出时前一个模块的失败会留在里面，用它判断会让后续成功的模块也显示为失败。
    """
    events = delta.get("events")
    if isinstance(events, list) and any(
        getattr(event, "failed", False) for event in events
    ):
        return True
    failures = delta.get("module_failures")
    return bool(isinstance(failures, list) and failures)


def terminal_event(failed: bool, detail: str = "") -> ProgressEvent:
    """终止事件。SSE 流以它结尾，前端据此关闭连接。

    失败时 percent 留 None 而非 100：失败不是「完成了」，给 100 会让进度条显示成功。
    """
    stage = Stage.FAILED if failed else Stage.DONE
    return ProgressEvent(
        stage=stage,
        label=STAGE_LABELS[stage],
        detail=detail,
        percent=None if failed else 100,
        failed=failed,
    )
