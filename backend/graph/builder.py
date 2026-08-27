"""图结构组装。

节点实现通过 NodeSet 注入，而非在本模块内直接实现。这样做的理由是执行顺序：本单元
要先用 stub 节点（不调 LLM）验证全图连通性与 reducer 累积，再由后续单元逐个替换为
真实节点。若节点实现写死在 builder 里，每次替换都要改图结构代码，而图结构本身是这
一层唯一需要保证正确的东西。

节点拓扑（对应技术设计的 LangGraph 节点图）：

    START -> ingest -> parse -> cluster -> planner -+-> [Send 扇出] -> module_agent
                                              |     |                       |
                                              |     |                  synthesize -> END
                                              |     +-> chunk_and_index ---------> END
                                              |
                                    -> select_files -> reviewer ----------------> END

**「并行」在这张图上的准确含义是 superstep 级，不是节点级。** LangGraph 按 superstep
推进：同一 superstep 内的节点并发执行，而下一个 superstep 必须等当前 superstep 全部
结束才启动。所以画在同一层不等于同时开跑，画在不同层则必然串行。实际排布是：

    superstep 4 : planner, select_files
    superstep 5 : module_agent(×N), reviewer, chunk_and_index
    superstep 6 : synthesize

这一点此前被这张注释图误导过：索引曾挂在 cluster 上，与 planner、select_files 同层，
于是模块扇出要等索引跑完（fastapi 实测 30.5 分钟）才启动，而注释图看起来三条分支是
并行的。索引的入边因此改到 planner 之后——那是一个调度决定，见下面 add_edge 处的说明。

评审分支不接收报告内容（KTD9）：它从 cluster 取骨架，与 synthesize 无数据依赖。这在
图结构上就成立，不靠约定——reviewer 读不到 report 字段，因为它在 synthesize 之前或
并行执行。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from backend.graph.observability import AnyGraphNode
from backend.graph.state import AnalysisState, ModulePlan, ModuleTask
from backend.static_analysis.models import DependencyGraph, Module, ParseOutcome

# 节点签名分两种，区别是输入形态而非风格：多数节点读完整 state，扇出节点只读
# `Send` 递来的分片（实测确认它看不到父 state）。分开标注让类型检查能挡住「把
# 分片节点接到普通边上」这类接错——那种错误在运行时表现为 KeyError，离根因很远。
StateNodeFn = AnyGraphNode[AnalysisState]
ShardNodeFn = AnyGraphNode[ModuleTask]

# 扇出目标节点名与汇聚节点名。路由函数要用到，抽成常量避免字符串散落。
MODULE_AGENT = "module_agent"
SYNTHESIZE = "synthesize"


@dataclass(frozen=True)
class NodeSet:
    """一套节点实现。字段名与图中的节点名一一对应。"""

    ingest: StateNodeFn
    parse: StateNodeFn
    cluster: StateNodeFn
    planner: StateNodeFn
    module_agent: ShardNodeFn
    synthesize: StateNodeFn
    select_files: StateNodeFn
    reviewer: StateNodeFn
    chunk_and_index: StateNodeFn


def fanout_router(state: AnalysisState) -> list[Send] | str:
    """planner 之后的扇出路由。返回 Send 列表，或在无模块时直接跳到汇聚节点。

    **无模块时必须显式返回汇聚节点名，不能返回空列表。** 实测确认：路由函数返回 []
    时不调度任何任务，而下游的 synthesize 也永远不执行——图正常结束但没有产出，
    表现为「静默少了一份报告」而非报错。这是本单元最容易埋进去的缺陷，因为正常路径
    的测试全绿。

    扇出宽度上限由 planner 负责（KTD16），这里不再截断——两处都截会让实际宽度取决于
    两个上限的先后顺序，难以解释。
    """
    planned = state.get("planned_modules") or []
    if not planned:
        return SYNTHESIZE

    workdir = state.get("workdir", "")
    graph = state.get("dependency_graph")
    parse_outcome = state.get("parse_outcome")

    return [
        Send(MODULE_AGENT, _module_task(plan, workdir, graph, parse_outcome))
        for plan in planned
    ]


def _module_task(
    plan: ModulePlan,
    workdir: str,
    graph: DependencyGraph | None,
    parse_outcome: ParseOutcome | None,
) -> ModuleTask:
    """构造单个模块的 state 分片。

    只带该模块自身需要的切片，不带全图与完整符号表——分片会被序列化 N 份（N 为扇出
    宽度），带全量数据时内存与序列化成本乘以 N。

    带上 Planner 的深挖档位与理由：子 Agent 需要知道挖多深（deep 档读更多文件），
    而理由让它知道这个模块为什么被选中，避免分析偏离 Planner 的判断。
    """
    module = plan.module
    members = set(module.files)

    neighbour_edges: dict[str, tuple[str, ...]] = {}
    edges = graph.edges if graph is not None else {}
    for path in module.files:
        outbound = tuple(sorted(t for t in edges.get(path, frozenset()) if t not in members))
        if outbound:
            neighbour_edges[path] = outbound

    symbol_index: dict[str, tuple[str, ...]] = {}
    member_files = []
    for parsed in parse_outcome.parsed if parse_outcome is not None else []:
        if parsed.path in members:
            symbol_index[parsed.path] = tuple(s.qualified_name for s in parsed.symbols)
            member_files.append(parsed)

    # 图切片：成员节点加它们指向的邻居。find_references 靠反向边定界检索，
    # 只带成员会让「谁引用了这个符号」在模块内也查不全（导入方可能是邻居）。
    slice_nodes = sorted(members | {t for targets in neighbour_edges.values() for t in targets})
    slice_edges = {
        node: frozenset(edges.get(node, frozenset())) & set(slice_nodes)
        for node in slice_nodes
    }

    return ModuleTask(
        module=module,
        workdir=workdir,
        neighbour_edges=neighbour_edges,
        symbol_index=symbol_index,
        depth=plan.depth,
        selection_reason=plan.reason,
        parse_outcome=ParseOutcome(parsed=member_files),
        graph=DependencyGraph(nodes=tuple(slice_nodes), edges=slice_edges),
    )


def build_analysis_graph(
    nodes: NodeSet,
    checkpointer: BaseCheckpointSaver[Any] | bool | None = None,
) -> Any:
    """组装并编译分析图。

    checkpointer 默认用内存实现：本单元只需要图能跑通与可恢复的语义，持久化
    checkpointer 要等到 U12 有了会话概念才有意义。传 False 可完全不挂 checkpointer。
    """
    builder = StateGraph(AnalysisState)

    builder.add_node("ingest", nodes.ingest)
    builder.add_node("parse", nodes.parse)
    builder.add_node("cluster", nodes.cluster)
    builder.add_node("planner", nodes.planner)
    builder.add_node(MODULE_AGENT, nodes.module_agent)
    builder.add_node(SYNTHESIZE, nodes.synthesize)
    builder.add_node("select_files", nodes.select_files)
    builder.add_node("reviewer", nodes.reviewer)
    builder.add_node("chunk_and_index", nodes.chunk_and_index)

    builder.add_edge(START, "ingest")
    builder.add_edge("ingest", "parse")
    builder.add_edge("parse", "cluster")

    # cluster 扇出两条分支。索引分支挂在 planner 之后而非这里，理由见下。
    builder.add_edge("cluster", "planner")
    builder.add_edge("cluster", "select_files")

    # 报告链路：planner 动态扇出到模块子 Agent，再汇聚。
    builder.add_conditional_edges("planner", fanout_router, [MODULE_AGENT, SYNTHESIZE])
    builder.add_edge(MODULE_AGENT, SYNTHESIZE)
    builder.add_edge(SYNTHESIZE, END)

    # 评审链路。
    builder.add_edge("select_files", "reviewer")
    builder.add_edge("reviewer", END)

    # 索引链路。**入边是 planner，不是 cluster——这是一个调度决定，不是数据依赖。**
    #
    # chunk_and_index 只读 parse_outcome 与 centrality，两者在 cluster 之后就齐了，
    # 所以按数据依赖它本该挂在 cluster 上。但 LangGraph 的 superstep 语义会让那样排布
    # 产生一个可观测的坏后果：cluster 的三个后继同属一个 superstep，而下一个 superstep
    # （模块扇出、reviewer）必须等**整个**当前 superstep 结束才启动。索引是这里最慢的
    # 一段（fastapi 实测 30.5 分钟，占 36.6 分钟总跨度的 83%），于是用户提交后要干等
    # 30 分钟才看到第一条模块分析——实测「索引跑完前 chat 调用一直停在 2 次」。
    #
    # 把它挪到 planner 之后，它就与模块扇出、reviewer 同属一个 superstep，三者真正并行：
    #
    #   superstep N   : planner, select_files
    #   superstep N+1 : module_agent(×N), reviewer, chunk_and_index   ← 并行
    #   superstep N+2 : synthesize
    #
    # 总跨度从 36.6 分钟降到约 31 分钟（索引 30.5 + synthesize），而模块分析的可见时刻
    # 从 30 分钟提到约 1 分钟。代价是引入一条并不存在的数据依赖——planner 的产出对索引
    # 毫无用处，这条边纯粹是为了把它排进下一个 superstep。不接受这个代价的替代方案是把
    # 索引拆成 start/await 两个节点并用进程级注册表传 asyncio.Task，那能再省 0.5 分钟，
    # 但要多维护一套悬挂任务的清理路径。
    #
    # planner 失败不影响它：planner 用 @observed 兜住异常并转成 state 数据（不 fatal），
    # 所以无论 planner 走没走成 LLM 路径，这条边都会到达。
    builder.add_edge("planner", "chunk_and_index")
    builder.add_edge("chunk_and_index", END)

    if checkpointer is False:
        return builder.compile()
    return builder.compile(checkpointer=checkpointer or InMemorySaver())
