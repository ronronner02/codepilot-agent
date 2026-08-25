"""不调 LLM 的 stub 节点集。

用途有两个，都不是临时脚手架：

  1. 本单元的验证手段。扇出机制、reducer 累积、失败隔离这些问题在有 LLM 噪声时很难
     定位——一次分析要几十秒、结果不确定、失败可能来自模型也可能来自图。stub 让这些
     缺陷在毫秒级确定性运行里暴露。
  2. 后续单元的回归基线。U6-U11 逐个替换真实节点时，图结构本身是否还正确，靠这套
     stub 继续验证。所以它随代码长期存在，不在 U5 结束后删除。

stub 的产出形状与真实节点一致（字段名、类型、数量关系），内容是固定假数据。形状一致
是关键——形状不一致的 stub 只能验证「图能跑」，验证不了「数据能流对」。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from backend.graph.builder import NodeSet
from backend.graph.observability import observed
from backend.graph.state import (
    AnalysisState,
    LanguageProfile,
    ModuleAnalysis,
    ModulePlan,
    ModuleTask,
)
from backend.static_analysis.models import (
    DependencyGraph,
    EntryKind,
    EntryPoint,
    Module,
    ParseOutcome,
)

# stub 的固定模块数。取 3 而非 1：单个模块跑通不能说明扇出正确，reducer 的累积语义
# 只在多份产出汇聚时才被真正检验。
DEFAULT_STUB_MODULES = 3


def _fake_modules(count: int) -> list[Module]:
    return [
        Module(
            name=f"stub_module_{i}",
            files=(f"stub/{i}/a.py", f"stub/{i}/b.py"),
            internal_edges=1,
            external_edges=0,
            origin="directory",
        )
        for i in range(count)
    ]


def _fake_graph(modules: Iterable[Module]) -> DependencyGraph:
    files = [f for module in modules for f in module.files]
    edges: dict[str, frozenset[str]] = {}
    for module in modules:
        first, second = module.files
        edges[first] = frozenset({second})
        edges[second] = frozenset()
    return DependencyGraph(nodes=tuple(files), edges=edges)


def make_stub_nodes(
    module_count: int = DEFAULT_STUB_MODULES,
    failing_modules: frozenset[str] = frozenset(),
) -> NodeSet:
    """构造一套 stub 节点。

    module_count 为 0 时 planner 产出空的深挖列表，用于验证「扇出宽度为 0 不卡死、
    汇聚节点仍执行」——这条路径在正常测试里不会被走到，必须能单独构造。

    failing_modules 让指定模块的子 Agent 抛异常，用于验证失败隔离：其余模块仍完成，
    失败被记录成数据而非中断整图。
    """

    @observed("ingest")
    def ingest(state: AnalysisState) -> dict[str, Any]:
        return {
            "workdir": "/stub/workdir",
            "commit_sha": "0" * 40,
            "language_profile": LanguageProfile(
                total_files=module_count * 2,
                parseable_files=module_count * 2,
                by_language={"Python": module_count * 2},
            ),
        }

    @observed("parse")
    def parse(state: AnalysisState) -> dict[str, Any]:
        return {"parse_outcome": ParseOutcome()}

    @observed("cluster")
    def cluster(state: AnalysisState) -> dict[str, Any]:
        modules = _fake_modules(module_count)
        return {
            "modules": modules,
            "dependency_graph": _fake_graph(modules),
            "entrypoints": [
                EntryPoint(path="stub/main.py", kind=EntryKind.CONVENTION, evidence="stub")
            ],
            "centrality": {f: 1.0 / max(len(modules) * 2, 1) for m in modules for f in m.files},
        }

    @observed("planner")
    def planner(state: AnalysisState) -> dict[str, Any]:
        modules = state.get("modules") or []
        return {
            "planned_modules": [
                ModulePlan(
                    module=module,
                    priority=index + 1,
                    reason=f"stub：第 {index + 1} 优先",
                    rule_score=1.0 - index * 0.1,
                    depth="deep" if index == 0 else "standard",
                )
                for index, module in enumerate(modules)
            ],
            "plan_rationale": f"stub：全部 {len(modules)} 个模块纳入深挖",
        }

    @observed("module_agent", scope_key="module")
    def module_agent(state: ModuleTask) -> dict[str, Any]:
        # 参数名统一为 state：分片就是这个节点的 state，langgraph 的节点协议也要求
        # 该参数可按 `state=` 传入。
        module = state["module"]
        if module.name in failing_modules:
            raise RuntimeError(f"stub 故意失败：{module.name}")
        return {
            "module_analyses": [
                ModuleAnalysis(
                    module_name=module.name,
                    summary=f"stub 分析：{module.name}（{len(module.files)} 个文件）",
                    cited_paths=module.files,
                )
            ]
        }

    @observed("synthesize")
    def synthesize(state: AnalysisState) -> dict[str, Any]:
        analyses = state.get("module_analyses") or []
        return {
            "report": "\n".join(a.summary for a in analyses) or "（无模块分析）",
            "unsupported_claims": [],
        }

    @observed("select_files")
    def select_files(state: AnalysisState) -> dict[str, Any]:
        scores = state.get("centrality") or {}
        ranked = sorted(scores, key=lambda p: (-scores[p], p))
        return {"review_targets": ranked[:5]}

    @observed("reviewer")
    def reviewer(state: AnalysisState) -> dict[str, Any]:
        targets = state.get("review_targets") or []
        # KTD9 的图上体现：reviewer 读不到 report——它与 synthesize 无数据依赖。
        assert "report" not in state, "评审分支不应接收报告内容（KTD9）"
        return {"findings": [f"stub 发现：{t}" for t in targets[:2]]}

    @observed("chunk_and_index")
    def chunk_and_index(state: AnalysisState) -> dict[str, Any]:
        outcome = state.get("parse_outcome")
        parsed = len(getattr(outcome, "parsed", []) or [])
        return {"indexed_chunks": parsed, "index_identity": "stub-provider/stub-model/v1"}

    return NodeSet(
        ingest=ingest,
        parse=parse,
        cluster=cluster,
        planner=planner,
        module_agent=module_agent,
        synthesize=synthesize,
        select_files=select_files,
        reviewer=reviewer,
        chunk_and_index=chunk_and_index,
    )
