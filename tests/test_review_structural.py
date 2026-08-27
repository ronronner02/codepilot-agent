"""结构类检查。

这类检查完全不调 LLM，所以断言可以写死——「这里有循环依赖」是事实而非意见，测试
断言具体的命中数与参与文件是恰当的。

零命中与未执行必须读起来不同（R17、AE2），这是本文件重点覆盖的一条。
"""

from __future__ import annotations

from backend.review.models import CheckStatus, FindingCategory, Severity
from backend.review.structural import (
    check_large_modules,
    check_layering,
    count_unreferenced_files,
    run_structural_checks,
)
from backend.static_analysis.graph import find_cycles
from backend.static_analysis.models import (
    DependencyGraph,
    EntryKind,
    EntryPoint,
    Granularity,
    Module,
)


def _graph(edges: dict[str, list[str]], extra_nodes: tuple[str, ...] = ()) -> DependencyGraph:
    nodes = sorted({*edges, *(t for ts in edges.values() for t in ts), *extra_nodes})
    edge_map = {n: set(edges.get(n, ())) for n in nodes}
    return DependencyGraph(
        nodes=tuple(nodes),
        edges={n: frozenset(t) for n, t in edge_map.items()},
        cycles=find_cycles(nodes, edge_map),
    )


def _module(name: str, files: tuple[str, ...], internal: int = 0, external: int = 0) -> Module:
    return Module(
        name=name,
        files=files,
        internal_edges=internal,
        external_edges=external,
        origin="directory",
    )


def _entry(path: str) -> EntryPoint:
    return EntryPoint(path=path, kind=EntryKind.MANIFEST, evidence="测试固定入口")


class TestCycles:
    def test_cycle_reported_with_participants(self) -> None:
        graph = _graph({"a.py": ["b.py"], "b.py": ["c.py"], "c.py": ["a.py"]})
        outcome = run_structural_checks(graph, [_module("root", tuple(graph.nodes))], [])
        cycles = [f for f in outcome.findings if f.kind == "circular_dependency"]
        assert len(cycles) == 1
        assert set((cycles[0].path, *cycles[0].related_paths)) == {"a.py", "b.py", "c.py"}

    def test_cycle_evidence_shows_the_ring(self) -> None:
        graph = _graph({"a.py": ["b.py"], "b.py": ["a.py"]})
        outcome = run_structural_checks(graph, [_module("root", tuple(graph.nodes))], [])
        cycle = next(f for f in outcome.findings if f.kind == "circular_dependency")
        assert "->" in cycle.evidence
        assert cycle.evidence.count("->") >= 2, "依据应给出完整环路"

    def test_long_cycle_is_higher_severity(self) -> None:
        """两文件互导常是有意的；跨 4 个以上文件的环通常是分层失控。"""
        short = _graph({"a.py": ["b.py"], "b.py": ["a.py"]})
        long_ring = {f"m{i}.py": [f"m{(i + 1) % 5}.py"] for i in range(5)}
        long_graph = _graph(long_ring)

        short_finding = next(
            f
            for f in run_structural_checks(short, [_module("r", tuple(short.nodes))], []).findings
            if f.kind == "circular_dependency"
        )
        long_finding = next(
            f
            for f in run_structural_checks(
                long_graph, [_module("r", tuple(long_graph.nodes))], []
            ).findings
            if f.kind == "circular_dependency"
        )
        assert short_finding.severity is Severity.MEDIUM
        assert long_finding.severity is Severity.HIGH

    def test_acyclic_graph_reports_no_cycle_finding(self) -> None:
        graph = _graph({"a.py": ["b.py"], "b.py": ["c.py"], "c.py": []})
        outcome = run_structural_checks(graph, [_module("root", tuple(graph.nodes))], [])
        assert not [f for f in outcome.findings if f.kind == "circular_dependency"]


class TestCallTimeCycles:
    """仅调用期成立的环只计数，不出发现。

    针对的是 fastapi 实跑那次误报：环上两条边是函数作用域的延迟导入，导入期不成立，
    而报出来的是「3 个文件构成循环依赖」。报出这类环等于让读者去修一处本就正确的规避
    措施——同一份报告的依赖关系一节反而把它描述为「主动规避循环依赖的例证」。
    """

    def _with_call_time(
        self, ring: tuple[str, ...], edges: dict[str, list[str]]
    ) -> DependencyGraph:
        """构造一个只含调用期环的图。cycles 为空、call_time_cycles 含该环。"""
        nodes = sorted({*edges, *(t for ts in edges.values() for t in ts)})
        return DependencyGraph(
            nodes=tuple(nodes),
            edges={n: frozenset(edges.get(n, ())) for n in nodes},
            cycles=(),
            call_time_cycles=(ring,),
        )

    def test_call_time_cycle_produces_no_finding(self) -> None:
        graph = self._with_call_time(
            ("a.py", "b.py", "c.py"),
            {"a.py": ["b.py"], "b.py": ["c.py"], "c.py": ["a.py"]},
        )
        outcome = run_structural_checks(graph, [_module("root", tuple(graph.nodes))], [])
        assert not [f for f in outcome.findings if f.kind == "circular_dependency"]

    def test_call_time_cycle_counted_in_scope(self) -> None:
        """不报出但要计数——「有几处这样的规避」对读者是有意义的规模信息。"""
        graph = self._with_call_time(
            ("a.py", "b.py"), {"a.py": ["b.py"], "b.py": ["a.py"]}
        )
        outcome = run_structural_checks(graph, [_module("root", tuple(graph.nodes))], [])
        assert "1 处环仅在调用期成立" in outcome.scope
        assert "延迟导入" in outcome.scope

    def test_scope_omits_note_when_no_call_time_cycles(self) -> None:
        """没有这类环时不多一句话——全量路径是默认，说明只在偏离默认时给。"""
        graph = _graph({"a.py": ["b.py"], "b.py": []})
        outcome = run_structural_checks(graph, [_module("root", tuple(graph.nodes))], [])
        assert "调用期" not in outcome.scope

    def test_scope_states_cycles_are_import_time_only(self) -> None:
        """范围说明要点明「循环依赖」这一项的口径已收窄，否则读者按旧口径理解。"""
        graph = _graph({"a.py": ["b.py"], "b.py": ["a.py"]})
        outcome = run_structural_checks(graph, [_module("root", tuple(graph.nodes))], [])
        assert "仅导入期成立的环" in outcome.scope

    def test_import_time_and_call_time_cycles_coexist(self) -> None:
        """两类环同时存在时，只有导入期那个出发现，调用期那个进计数。"""
        graph = DependencyGraph(
            nodes=("p.py", "q.py", "r.py", "s.py"),
            edges={
                "p.py": frozenset({"q.py"}),
                "q.py": frozenset({"p.py"}),
                "r.py": frozenset({"s.py"}),
                "s.py": frozenset({"r.py"}),
            },
            cycles=(("p.py", "q.py"),),
            call_time_cycles=(("r.py", "s.py"),),
        )
        outcome = run_structural_checks(graph, [_module("root", tuple(graph.nodes))], [])
        cycles = [f for f in outcome.findings if f.kind == "circular_dependency"]
        assert len(cycles) == 1
        assert set((cycles[0].path, *cycles[0].related_paths)) == {"p.py", "q.py"}
        assert "1 处环仅在调用期成立" in outcome.scope


class TestZeroHitDistinguishable:
    def test_clean_graph_is_executed_with_zero_hits(self) -> None:
        """零命中必须是「已检查」而非留空——AE2 的核心要求。"""
        graph = _graph({"pkg/a.py": ["pkg/b.py"], "pkg/b.py": []})
        outcome = run_structural_checks(
            graph, [_module("pkg", ("pkg/a.py", "pkg/b.py"), internal=1)], [_entry("pkg/a.py")]
        )
        assert outcome.status is CheckStatus.EXECUTED
        assert outcome.hit_count == 0
        assert "已检查" in outcome.describe()
        assert "命中 0 条" in outcome.describe()

    def test_scope_names_the_checks_performed(self) -> None:
        """零命中时 scope 承担全部信息量，必须列出查了哪些项。"""
        graph = _graph({"pkg/a.py": ["pkg/b.py"], "pkg/b.py": []})
        outcome = run_structural_checks(
            graph, [_module("pkg", ("pkg/a.py", "pkg/b.py"))], [_entry("pkg/a.py")]
        )
        for expected in ("循环依赖", "层级违规", "超大模块"):
            assert expected in outcome.scope
        # 零入度文件只做聚合统计，不算检查项，但计数与理由仍须可见。
        assert "无仓库内导入方" in outcome.scope

    def test_empty_graph_is_skipped_not_zero_hit(self) -> None:
        outcome = run_structural_checks(DependencyGraph(), [], [])
        assert outcome.status is CheckStatus.SKIPPED
        assert outcome.reason
        assert "未执行" in outcome.describe()

    def test_skipped_and_zero_hit_read_differently(self) -> None:
        """这两种情况在产出中不得表现相同（AE2 明文要求）。"""
        clean = _graph({"pkg/a.py": ["pkg/b.py"], "pkg/b.py": []})
        zero_hit = run_structural_checks(
            clean, [_module("pkg", ("pkg/a.py", "pkg/b.py"))], [_entry("pkg/a.py")]
        )
        skipped = run_structural_checks(DependencyGraph(), [], [])
        assert zero_hit.describe() != skipped.describe()
        assert zero_hit.hit_count == skipped.hit_count == 0

    def test_degraded_graph_is_skipped_with_reason(self) -> None:
        """目录级粒度下文件级判据会产出措辞与对象不符的结论，跳过并说明。"""
        graph = DependencyGraph(
            nodes=("pkg", "lib"),
            edges={"pkg": frozenset({"lib"}), "lib": frozenset()},
            granularity=Granularity.DIRECTORY,
            degraded_reason="文件数超出上限",
        )
        outcome = run_structural_checks(graph, [], [])
        assert outcome.status is CheckStatus.SKIPPED
        assert "降级" in outcome.reason
        assert "文件数超出上限" in outcome.reason


class TestLayering:
    def test_minority_reverse_edge_flagged(self) -> None:
        """8 条 api->core 加 1 条 core->api，那 1 条是可疑的反向依赖。"""
        edges: dict[str, list[str]] = {}
        for i in range(8):
            edges[f"api/h{i}.py"] = [f"core/s{i}.py"]
            edges[f"core/s{i}.py"] = []
        edges["core/s0.py"] = ["api/h0.py"]

        graph = _graph(edges)
        modules = [
            _module("api", tuple(sorted(p for p in graph.nodes if p.startswith("api/")))),
            _module("core", tuple(sorted(p for p in graph.nodes if p.startswith("core/")))),
        ]
        findings = check_layering(graph, modules)
        assert len(findings) == 1
        assert findings[0].path == "core/s0.py"
        assert findings[0].related_paths == ("api/h0.py",)

    def test_evidence_states_the_ratio(self) -> None:
        edges: dict[str, list[str]] = {f"api/h{i}.py": [f"core/s{i}.py"] for i in range(8)}
        for i in range(8):
            edges[f"core/s{i}.py"] = []
        edges["core/s0.py"] = ["api/h0.py"]
        graph = _graph(edges)
        modules = [
            _module("api", tuple(sorted(p for p in graph.nodes if p.startswith("api/")))),
            _module("core", tuple(sorted(p for p in graph.nodes if p.startswith("core/")))),
        ]
        evidence = check_layering(graph, modules)[0].evidence
        assert "9 条边" in evidence
        assert "%" in evidence, "依据应给出占比，读者才能判断这条是否值得管"

    def test_balanced_bidirectional_not_flagged(self) -> None:
        """两方向边数相当说明本就是双向协作，不构成层级违规。

        这类情况若确实成环，由循环依赖检查覆盖。
        """
        edges: dict[str, list[str]] = {}
        for i in range(4):
            edges[f"a/x{i}.py"] = [f"b/y{i}.py"]
            edges[f"b/y{i}.py"] = [f"a/x{(i + 1) % 4}.py"]
        graph = _graph(edges)
        modules = [
            _module("a", tuple(sorted(p for p in graph.nodes if p.startswith("a/")))),
            _module("b", tuple(sorted(p for p in graph.nodes if p.startswith("b/")))),
        ]
        assert check_layering(graph, modules) == []

    def test_too_few_edges_not_flagged(self) -> None:
        """边太少时比例不具统计意义——1 对 0 的占比是 0，但那只说明两模块几乎无关系。"""
        graph = _graph({"a/x.py": ["b/y.py"], "b/y.py": []})
        modules = [_module("a", ("a/x.py",)), _module("b", ("b/y.py",))]
        assert check_layering(graph, modules) == []


class TestLargeModules:
    def test_oversized_module_flagged(self) -> None:
        files = tuple(f"big/f{i}.py" for i in range(45))
        findings = check_large_modules([_module("big", files, internal=20, external=3)])
        assert len(findings) == 1
        assert "45 个文件" in findings[0].message
        assert len(findings[0].related_paths) == 44

    def test_evidence_carries_cohesion_numbers(self) -> None:
        """内外边数是判断「该不该拆」的依据，不能只给文件数。"""
        files = tuple(f"big/f{i}.py" for i in range(45))
        evidence = check_large_modules([_module("big", files, internal=20, external=3)])[0].evidence
        assert "20" in evidence
        assert "3" in evidence

    def test_threshold_is_tunable(self) -> None:
        files = tuple(f"m/f{i}.py" for i in range(10))
        assert check_large_modules([_module("m", files)], max_files=40) == []
        assert len(check_large_modules([_module("m", files)], max_files=5)) == 1


class TestUnreferencedFiles:
    """零入度文件只做聚合统计，不产出逐条发现。

    基准仓库实测结论：这个信号误报率接近 100%。fastapi 的 958 个零入度文件里 956 是
    测试与示例；ghostfolio 排除测试目录后剩下的 113 个也全是 jest.config.ts、
    prisma/seed.mts、*.stories.ts 这类由外部工具调用的文件——它们的调用方在仓库之外。
    逐条报出会产出近千条发现，把真正有价值的循环依赖彻底淹没。
    """

    def test_counts_zero_indegree_non_entry(self) -> None:
        graph = _graph({"orphan.py": ["lib.py"], "lib.py": []})
        assert count_unreferenced_files(graph, []) == 1

    def test_entrypoint_not_counted(self) -> None:
        graph = _graph({"main.py": ["lib.py"], "lib.py": []})
        assert count_unreferenced_files(graph, [_entry("main.py")]) == 0

    def test_not_reported_as_findings(self) -> None:
        """近千条 LOW 级发现会淹没真问题，所以这一项不进 findings。"""
        graph = _graph({f"orphan{i}.py": ["lib.py"] for i in range(20)} | {"lib.py": []})
        outcome = run_structural_checks(graph, [_module("root", tuple(graph.nodes))], [])
        assert not [f for f in outcome.findings if f.kind == "unreferenced_file"]

    def test_count_disclosed_in_scope(self) -> None:
        """信息不丢：计数与「为什么不逐条列」都写进 scope。"""
        graph = _graph({"orphan.py": ["lib.py"], "lib.py": []})
        outcome = run_structural_checks(graph, [_module("root", tuple(graph.nodes))], [])
        assert "1 个文件无仓库内导入方" in outcome.scope
        assert "误报率过高" in outcome.scope
        assert "符号级死代码" in outcome.scope


class TestFindingContract:
    def test_every_finding_carries_required_fields(self) -> None:
        """R16：每条发现必须有路径、行号、类型与判断依据。"""
        edges: dict[str, list[str]] = {"a.py": ["b.py"], "b.py": ["a.py"], "orphan.py": ["a.py"]}
        graph = _graph(edges)
        outcome = run_structural_checks(
            graph, [_module("root", tuple(graph.nodes))], [], max_module_files=2
        )
        assert outcome.findings
        for finding in outcome.findings:
            assert finding.path
            assert finding.line >= 0
            assert finding.kind
            assert finding.evidence, f"{finding.kind} 缺判断依据，无法核验"
            assert finding.message != finding.evidence, "依据应说明凭据，而非重复描述"
            assert finding.category is FindingCategory.STRUCTURAL

    def test_findings_order_is_deterministic(self) -> None:
        edges: dict[str, list[str]] = {"a.py": ["b.py"], "b.py": ["a.py"], "orphan.py": ["a.py"]}
        graph = _graph(edges)
        modules = [_module("root", tuple(graph.nodes))]
        first = run_structural_checks(graph, modules, [])
        second = run_structural_checks(graph, modules, [])
        assert [(f.kind, f.path) for f in first.findings] == [
            (f.kind, f.path) for f in second.findings
        ]
