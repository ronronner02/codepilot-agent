"""LangGraph 的 state schema。

state 字段设计是本单元的核心。判据只有一条：**这个字段会不会在同一个 superstep 里
被多个节点写入。**

会 -> 必须带 reducer。不带会直接抛 InvalidUpdateError（实测确认，不是静默覆盖）：
    InvalidUpdateError: At key 'x': Can receive only one value per step.
    Use an Annotated key to handle multiple values.

不会 -> 不带 reducer，覆盖语义。默认选它，因为 reducer 意味着「累积」，用在只有单个
写入方的字段上会让后续读者误以为这里可能有多份数据。

本图有两处并发写入：
  1. 模块子 Agent 扇出（KTD2 的 `Send`）——同一节点被并行调度多次。
  2. cluster 之后的三条分支（报告 / 评审 / 索引）并行执行。

所以 module_analyses、module_failures（第 1 处）与 events（第 2 处，三条分支都写）
带 reducer；其余字段各由单一节点产出，用覆盖语义。
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Annotated, TypedDict

from backend.static_analysis.models import (
    DependencyGraph,
    EntryPoint,
    Module,
    ParseOutcome,
)


@dataclass(frozen=True)
class ModuleAnalysis:
    """单个模块子 Agent 的产出。"""

    module_name: str
    summary: str
    cited_paths: tuple[str, ...] = ()
    """结论引用的文件路径。报告校验（U8）据此判断结论是否可追溯（R7）。"""
    tool_calls: tuple[str, ...] = ()
    """工具调用序列。U7 的 Verification 要求 trace 里能看到它，也便于判断分析深度。"""
    rounds_used: int = 0
    limitation: str = ""
    """非空表示分析不完整（轮次用尽、检测到打转）。报告须标注，不能当成完整分析。"""


@dataclass(frozen=True)
class NodeFailure:
    """节点级失败记录。

    为什么需要它：实测确认节点抛异常会中断整图（`RuntimeError` 直接冒泡到
    `invoke`），扇出中的一个模块失败会让整次分析失败。所以节点必须自己兜住异常，
    把失败转成数据写进 state——这是 R10（部分失败不影响整体）的机制基础，而不是
    可选的健壮性修饰。
    """

    node: str
    scope: str
    """失败的具体对象（模块名、文件路径等），无则为空串。"""
    error: str


@dataclass(frozen=True)
class NodeEvent:
    """节点执行记录。供开发期观测（R25）与 SSE 进度推送（U12）共用。"""

    node: str
    duration_ms: int
    summary: str
    failed: bool = False


class ModuleTask(TypedDict, total=False):
    """扇出给单个模块子 Agent 的 state 分片。

    实测确认：`Send(node, payload)` 让目标节点只看到 payload，看不到父 state。所以
    分片必须自带子 Agent 需要的全部上下文——这不是优化选择，是硬约束。

    分片带什么，是本单元最需要想清楚的一处。原则是「够用且有界」：
      - module：待分析的模块（含成员文件列表）。
      - workdir：读文件的根目录。子 Agent 的工具据此定位文件，并复用 U1 的路径校验。
      - neighbour_edges：该模块成员指向模块外的边。这是「这个模块如何与外界交互」的
        依据，缺了它子 Agent 只能看到孤立的文件堆。
      - symbol_index：成员文件的符号名列表，供子 Agent 决定读哪个文件的哪一段。

    不带完整符号表与全图：那会让每个 Send 的 payload 随仓库规模线性增长，扇出 12 路
    时（KTD16 的上限）内存与序列化成本乘以 12。子 Agent 需要更多信息时通过工具按需
    读取（U7），而不是预先塞进分片。
    """

    module: Module
    workdir: str
    neighbour_edges: dict[str, tuple[str, ...]]
    symbol_index: dict[str, tuple[str, ...]]
    depth: str
    """Planner 定的深挖档位（`deep` / `standard`），决定子 Agent 读多少文件。"""
    selection_reason: str
    """Planner 挑中它的理由。让子 Agent 的分析不偏离 Planner 的判断。"""
    parse_outcome: ParseOutcome
    """**只含本模块成员文件**的解析结果。

    工具层的 find_definition 需要 Symbol 对象（含行号），symbol_index 的名字列表不够。
    带模块级切片而非全仓库符号表：切片受模块规模约束（几十个文件），不随仓库规模膨胀，
    符合分片「够用且有界」的原则。

    代价是子 Agent 查不到模块外的符号定义。这是分片设计的直接后果，且方向正确——
    子 Agent 的职责是讲清这一个模块，跨模块关系由汇总节点从完整依赖图重建。
    """
    graph: DependencyGraph
    """**只含本模块成员与其直接邻居**的依赖图切片。供 find_references 定界检索。"""


@dataclass(frozen=True)
class ModulePlan:
    """Planner 对单个模块的深挖决定。

    为什么不只存 Module：R8 要求决策依据可在产出中体现，而「为什么挑这个模块」是
    逐模块的信息，单个 rationale 字符串承载不了。报告里要能说明「这几个模块被深挖，
    依据是什么」，否则读者无法判断分析范围是否合理。

    rule_score 与 reason 分开保留：前者是确定性排序的得分（可复现、可核对），后者是
    模型的解释。两者不同源，混在一起会让读者无法判断哪部分是算出来的。
    """

    module: Module
    priority: int
    """1 为最高。截断时保留数字小的。"""
    reason: str
    rule_score: float
    depth: str = "standard"
    """`deep` 或 `standard`。决定子 Agent 读多少文件、分析多细。"""


@dataclass
class LanguageProfile:
    """语言构成。U2 的 language_detect 产出的等价形态，避免 graph 层反向依赖 ingest。"""

    total_files: int = 0
    parseable_files: int = 0
    by_language: dict[str, int] = field(default_factory=dict)


class AnalysisState(TypedDict, total=False):
    """一次仓库分析的完整 state。

    用 `total=False`：节点按阶段逐步填充，图刚启动时只有 repo_url。全字段必填会让
    每个节点都要构造无关字段的占位值。
    """

    # ── 输入 ──
    repo_url: str

    # ── ingest 产出 ──
    #
    # **未在此声明的字段会被 LangGraph 静默丢弃。** 实测踩到过：节点返回了
    # review_report / index_note 等字段，因未声明而凭空消失——运行时不报错，只是下游
    # 读不到。新增节点产出时必须同步在这里声明。
    workdir: str
    admission_note: str
    """准入检查的规模摘要。排查「为什么这个仓库被拒」时需要它。"""
    commit_sha: str
    language_profile: LanguageProfile

    # ── parse 产出 ──
    parse_outcome: ParseOutcome

    # ── cluster 产出。三条下游分支的共同输入 ──
    dependency_graph: DependencyGraph
    modules: list[Module]
    entrypoints: list[EntryPoint]
    centrality: dict[str, float]

    # ── planner 产出 ──
    planned_modules: list[ModulePlan]
    plan_rationale: str
    """整体决策说明：排序依据、截断位置与原因、是否降级为规则排序（R8）。"""

    # ── 模块子 Agent 扇出产出。并发写入，必须带 reducer ──
    module_analyses: Annotated[list[ModuleAnalysis], operator.add]
    module_failures: Annotated[list[NodeFailure], operator.add]

    # ── synthesize 产出 ──
    report: object
    """ArchitectureReport 对象。用 object 标注避免 graph 层反向依赖 report 包的类型，
    实际类型见 backend.report.schema.ArchitectureReport。"""
    report_validation_summary: str
    """校验结果摘要。让「有多少结论因无法核验被丢弃」可被程序读取，而非埋在日志里。"""
    unsupported_claims: list[str]
    """无法追溯到文件路径的结论。U8 的校验产出，非空即说明报告未达 R7。"""

    # ── 评审分支产出（KTD9：不接收报告内容）──
    review_targets: list[str]
    review_selection_basis: str
    """文件挑选依据。R17 要求说明检查范围，「为什么是这些文件」是其中一部分。"""
    review_report: object
    """ReviewReport 对象。用 object 标注避免 graph 层反向依赖 review 包的类型，
    实际类型见 backend.review.models.ReviewReport。"""
    findings: list[str]

    # ── 索引分支产出 ──
    indexed_chunks: int
    index_note: str
    """索引状态说明。「索引了 0 个切块」与「索引未实现」含义不同，需可区分。"""
    index_cache_hit: bool
    """本次是否命中索引缓存。界面据此决定要不要显示进度（R5）。"""
    index_digest: str
    """索引集合的标识。问答链路据此定位要检索哪个集合。"""
    index_identity: str
    """provider 标识 + 切块策略版本，构成 U9 缓存键的一部分（KTD4）。"""

    # ── 跨节点观测。三条分支并行写入，必须带 reducer ──
    events: Annotated[list[NodeEvent], operator.add]
