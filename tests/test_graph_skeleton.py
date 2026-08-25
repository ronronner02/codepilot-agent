"""图骨架：连通性、扇出与 reducer、失败隔离、trace 开关。

这些测试的价值在于它们验证的是「机制」而非「输出」。扇出丢重、零扇出静默跳过汇聚、
一个子任务失败拖垮整图——这三类缺陷在接上 LLM 后极难定位（一次运行几十秒、结果不
确定、失败来源不明），但在 stub 下是毫秒级确定性的。
"""

from __future__ import annotations

import logging

from backend.graph.builder import build_analysis_graph, fanout_router
from backend.graph.observability import configure_tracing, summarize
from backend.graph.state import AnalysisState, ModulePlan
from backend.graph.stub_nodes import make_stub_nodes
from backend.static_analysis.models import Module
from tests.support import make_settings as _settings


def _plan(
    name: str,
    files: tuple[str, ...],
    depth: str = "standard",
    reason: str = "测试固定理由",
) -> ModulePlan:
    return ModulePlan(
        module=Module(
            name=name,
            files=files,
            internal_edges=len(files) - 1,
            external_edges=0,
            origin="directory",
        ),
        priority=1,
        reason=reason,
        rule_score=0.5,
        depth=depth,
    )

THREAD = {"configurable": {"thread_id": "test"}}


def _run(module_count: int = 3, failing: frozenset[str] = frozenset()) -> AnalysisState:
    graph = build_analysis_graph(make_stub_nodes(module_count, failing))
    return graph.invoke({"repo_url": "https://github.com/o/r"}, THREAD)


class TestConnectivity:
    def test_full_graph_runs_start_to_end(self) -> None:
        result = _run()
        assert result["workdir"] == "/stub/workdir"
        assert result["report"]
        assert result["findings"]
        assert result["index_identity"] == "stub-provider/stub-model/v1"

    def test_all_three_branches_execute(self) -> None:
        """cluster 之后的三条分支都要跑到——漏一条表现为「某份产出缺失」而非报错。"""
        nodes = {e.node for e in _run()["events"]}
        assert {"synthesize", "reviewer", "chunk_and_index"} <= nodes

    def test_node_order_respects_dependencies(self) -> None:
        order = [e.node for e in _run()["events"]]
        assert order.index("ingest") < order.index("parse") < order.index("cluster")
        assert order.index("cluster") < order.index("planner")
        assert order.index("planner") < order.index("synthesize")
        assert order.index("select_files") < order.index("reviewer")

    def test_runs_without_checkpointer(self) -> None:
        graph = build_analysis_graph(make_stub_nodes(2), checkpointer=False)
        assert graph.invoke({"repo_url": "u"})["report"]


class TestFanout:
    def test_three_subtasks_accumulate_without_loss_or_duplication(self) -> None:
        result = _run(module_count=3)
        analyses = result["module_analyses"]
        assert len(analyses) == 3
        assert {a.module_name for a in analyses} == {
            "stub_module_0",
            "stub_module_1",
            "stub_module_2",
        }

    def test_wide_fanout_accumulates_exactly(self) -> None:
        """宽扇出下的丢重问题只在数量较大时才稳定复现。"""
        result = _run(module_count=12)
        assert len(result["module_analyses"]) == 12
        assert len({a.module_name for a in result["module_analyses"]}) == 12

    def test_zero_fanout_still_reaches_synthesize(self) -> None:
        """零扇出时汇聚节点必须仍执行，且收到空列表。

        实测确认路由函数返回空列表时 synthesize 永远不执行——图正常结束但没有报告。
        这是静默缺陷：正常路径的测试全绿，只有零模块的仓库会中招。
        """
        result = _run(module_count=0)
        assert result.get("module_analyses", []) == []
        assert result["report"] == "（无模块分析）"
        assert any(e.node == "synthesize" for e in result["events"])

    def test_router_returns_synthesize_when_no_modules(self) -> None:
        assert fanout_router({"planned_modules": []}) == "synthesize"

    def test_router_returns_one_send_per_module(self) -> None:
        plans = [_plan("m0", ("a.py",)), _plan("m1", ("b.py",))]
        sends = fanout_router({"planned_modules": plans, "workdir": "/w"})
        assert isinstance(sends, list)
        assert len(sends) == 2
        assert {s.arg["module"].name for s in sends} == {"m0", "m1"}

    def test_shard_carries_planner_depth_and_reason(self) -> None:
        """子 Agent 要知道挖多深，以及这个模块为什么被选中。"""
        plans = [_plan("m0", ("a.py",), depth="deep", reason="核心业务逻辑")]
        sends = fanout_router({"planned_modules": plans, "workdir": "/w"})
        assert isinstance(sends, list)
        assert sends[0].arg["depth"] == "deep"
        assert sends[0].arg["selection_reason"] == "核心业务逻辑"


class TestShardIsolation:
    def test_shard_carries_only_module_scoped_context(self) -> None:
        """分片不带全图与完整符号表：它会被序列化 N 份，N 是扇出宽度。"""
        plans = [_plan("m0", ("pkg/a.py", "pkg/b.py"))]
        sends = fanout_router({"planned_modules": plans, "workdir": "/w"})
        assert isinstance(sends, list)
        shard = sends[0].arg
        assert set(shard) == {
            "module",
            "workdir",
            "neighbour_edges",
            "symbol_index",
            "depth",
            "selection_reason",
            "parse_outcome",
            "graph",
        }

    def test_shard_parse_outcome_limited_to_members(self) -> None:
        """解析切片只含成员文件——工具层要 Symbol 对象（含行号），但不该带全仓库符号表。

        切片受模块规模约束（几十个文件），不随仓库规模膨胀，这才让分片有意义。
        """
        from backend.static_analysis.models import ParsedFile, ParseOutcome, Symbol, SymbolKind

        def _parsed(path: str) -> ParsedFile:
            return ParsedFile(
                path=path,
                language="python",
                symbols=[
                    Symbol(
                        name="f",
                        kind=SymbolKind.FUNCTION,
                        path=path,
                        start_line=1,
                        end_line=2,
                    )
                ],
            )

        outcome = ParseOutcome(
            parsed=[_parsed("pkg/a.py"), _parsed("pkg/b.py"), _parsed("other/z.py")]
        )
        sends = fanout_router(
            {
                "planned_modules": [_plan("m0", ("pkg/a.py", "pkg/b.py"))],
                "workdir": "/w",
                "parse_outcome": outcome,
            }
        )
        assert isinstance(sends, list)
        shard_paths = {f.path for f in sends[0].arg["parse_outcome"].parsed}
        assert shard_paths == {"pkg/a.py", "pkg/b.py"}, "不应带入模块外的文件"

    def test_shard_graph_includes_members_and_neighbours(self) -> None:
        """图切片要含邻居：find_references 靠反向边定界，只带成员会让导入方查不到。"""
        from backend.static_analysis.models import DependencyGraph

        graph = DependencyGraph(
            nodes=("pkg/a.py", "pkg/b.py", "other/c.py", "far/d.py"),
            edges={
                "pkg/a.py": frozenset({"other/c.py"}),
                "pkg/b.py": frozenset(),
                "other/c.py": frozenset(),
                "far/d.py": frozenset(),
            },
        )
        sends = fanout_router(
            {
                "planned_modules": [_plan("m0", ("pkg/a.py", "pkg/b.py"))],
                "workdir": "/w",
                "dependency_graph": graph,
            }
        )
        assert isinstance(sends, list)
        slice_nodes = set(sends[0].arg["graph"].nodes)
        assert slice_nodes == {"pkg/a.py", "pkg/b.py", "other/c.py"}
        assert "far/d.py" not in slice_nodes, "无关文件不应进切片"

    def test_shard_neighbour_edges_exclude_intra_module_edges(self) -> None:
        """邻接边只记指向模块外的——组内边是模块自身结构，子 Agent 读文件就能看到。"""
        from backend.static_analysis.models import DependencyGraph

        graph = DependencyGraph(
            nodes=("pkg/a.py", "pkg/b.py", "other/c.py"),
            edges={
                "pkg/a.py": frozenset({"pkg/b.py", "other/c.py"}),
                "pkg/b.py": frozenset(),
                "other/c.py": frozenset(),
            },
        )
        sends = fanout_router(
            {
                "planned_modules": [_plan("m", ("pkg/a.py", "pkg/b.py"))],
                "workdir": "/w",
                "dependency_graph": graph,
            }
        )
        assert isinstance(sends, list)
        assert sends[0].arg["neighbour_edges"] == {"pkg/a.py": ("other/c.py",)}


class TestFailureIsolation:
    def test_one_failing_subtask_does_not_stop_others(self) -> None:
        """实测确认节点抛异常会中断整图，所以失败必须在节点内转成数据。"""
        result = _run(module_count=3, failing=frozenset({"stub_module_1"}))
        assert len(result["module_analyses"]) == 2
        assert {a.module_name for a in result["module_analyses"]} == {
            "stub_module_0",
            "stub_module_2",
        }

    def test_failure_recorded_with_scope(self) -> None:
        result = _run(module_count=3, failing=frozenset({"stub_module_1"}))
        failures = result["module_failures"]
        assert len(failures) == 1
        assert failures[0].node == "module_agent"
        assert failures[0].scope == "stub_module_1"
        assert "RuntimeError" in failures[0].error

    def test_report_still_produced_after_partial_failure(self) -> None:
        result = _run(module_count=3, failing=frozenset({"stub_module_0"}))
        assert result["report"]
        assert any(e.failed for e in result["events"])

    def test_all_subtasks_failing_still_reaches_synthesize(self) -> None:
        failing = frozenset({f"stub_module_{i}" for i in range(3)})
        result = _run(module_count=3, failing=failing)
        assert result.get("module_analyses", []) == []
        assert len(result["module_failures"]) == 3
        assert result["report"] == "（无模块分析）"


class TestStateSemantics:
    def test_reducer_fields_accumulate(self) -> None:
        result = _run(module_count=3)
        assert len(result["module_analyses"]) == 3
        # events 由九个节点分别写入，累积而非覆盖。
        assert len(result["events"]) >= 9

    def test_non_reducer_field_is_overwritten(self) -> None:
        """无 reducer 的字段是覆盖语义：planner 写入的 rationale 只有一份。"""
        result = _run(module_count=3)
        assert isinstance(result["plan_rationale"], str)
        assert result["plan_rationale"].startswith("stub：全部 3")

    def test_events_carry_duration(self) -> None:
        assert all(e.duration_ms >= 0 for e in _run()["events"])


class TestObservability:
    def test_graph_runs_with_tracing_disabled(self) -> None:
        """trace 开关关闭时图正常运行，不因缺少 LangSmith 配置报错。"""
        assert configure_tracing(_settings(langsmith_tracing=False)) is False
        assert _run()["report"]

    def test_tracing_not_enabled_without_api_key(self) -> None:
        """开关为真但缺 key 时不开——否则每次调用都报认证错误，噪声盖过有用输出。"""
        assert configure_tracing(_settings(langsmith_tracing=True, langsmith_api_key="")) is False

    def test_tracing_enabled_sets_env(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
        enabled = configure_tracing(
            _settings(langsmith_tracing=True, langsmith_api_key="sk-test", langsmith_project="p")
        )
        import os

        assert enabled is True
        assert os.environ["LANGSMITH_TRACING"] == "true"
        assert os.environ["LANGSMITH_PROJECT"] == "p"
        monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
        monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)

    def test_node_execution_logged(self, caplog) -> None:  # type: ignore[no-untyped-def]
        """结构化日志始终生效，不依赖 LangSmith 可达。"""
        with caplog.at_level(logging.INFO, logger="codepilot.graph"):
            _run(module_count=1)
        messages = [r.getMessage() for r in caplog.records]
        assert any("节点 ingest" in m for m in messages)
        assert any("节点 synthesize" in m for m in messages)

    def test_failure_logged_with_scope(self, caplog) -> None:  # type: ignore[no-untyped-def]
        with caplog.at_level(logging.WARNING, logger="codepilot.graph"):
            _run(module_count=2, failing=frozenset({"stub_module_0"}))
        messages = [r.getMessage() for r in caplog.records]
        assert any("stub_module_0" in m and "失败" in m for m in messages)

    def test_summary_does_not_inline_large_payloads(self) -> None:
        """摘要只读长度与键名——state 里有整份符号表，塞进日志会让单条记录几十 KB。"""
        summary = summarize({"nodes": list(range(5000)), "text": "x" * 500})
        assert "nodes=5000" in summary
        assert "text=500c" in summary
        assert "xxxx" not in summary
