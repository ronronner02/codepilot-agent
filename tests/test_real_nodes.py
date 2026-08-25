"""生产节点集的接线。

不测各单元的业务逻辑（那些各有自己的测试文件），只测这一层的职责：state 字段的进出
是否对得上、没有 provider 时是否走各自的降级路径而非整图失败。

接线错误的表现形式是「某个字段没人填」，运行时是 KeyError 或空产出，离根因很远——所以
这层的测试价值在于把那类错误挡在接线阶段。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.graph.builder import build_analysis_graph
from backend.graph.real_nodes import collect_repo_files, make_real_nodes
from backend.static_analysis.models import ParsedFile, ParseOutcome
from tests.support import make_settings


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "core.py").write_text(
        "def helper(v):\n    return v * 2\n", encoding="utf-8"
    )
    (root / "pkg" / "api.py").write_text(
        "from pkg.core import helper\n\n\ndef go():\n    return helper(1)\n", encoding="utf-8"
    )
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dep.js").write_text("x\n", encoding="utf-8")
    (root / "README.md").write_text("# hi\n", encoding="utf-8")
    return root


class TestFileCollection:
    def test_skips_noise_directories(self, repo: Path) -> None:
        """node_modules 之类进解析会让规模与耗时失控，且与架构理解无关。"""
        files = collect_repo_files(repo)
        assert "pkg/core.py" in files
        assert not any("node_modules" in f for f in files)

    def test_includes_non_source_files(self, repo: Path) -> None:
        """非源码文件仍要进清单——它们计入文件树与规模统计（语言范围决定）。"""
        assert "README.md" in collect_repo_files(repo)

    def test_returns_repo_relative_posix_paths(self, repo: Path) -> None:
        """全链路用仓库相对 posix 路径：报告引用、缓存键、MCP 工具参数都依赖这个约定。"""
        files = collect_repo_files(repo)
        assert all(not Path(f).is_absolute() for f in files)
        assert all("\\" not in f for f in files)


class TestParseAndCluster:
    async def test_parse_node_fills_outcome(self, repo: Path) -> None:
        nodes = make_real_nodes(make_settings())
        result = await nodes.parse({"workdir": str(repo)})  # type: ignore[operator,arg-type]
        outcome = result["parse_outcome"]
        assert isinstance(outcome, ParseOutcome)
        assert {f.path for f in outcome.parsed} == {"pkg/core.py", "pkg/api.py"}

    async def test_cluster_node_fills_four_fields(self, repo: Path) -> None:
        """下游三条分支都从 cluster 取骨架，缺一个字段就有一条分支拿不到输入。"""
        nodes = make_real_nodes(make_settings())
        parsed = await nodes.parse({"workdir": str(repo)})  # type: ignore[operator,arg-type]
        result = await nodes.cluster(  # type: ignore[operator]
            {"workdir": str(repo), "parse_outcome": parsed["parse_outcome"]}  # type: ignore[arg-type]
        )
        for field in ("dependency_graph", "modules", "entrypoints", "centrality"):
            assert field in result, f"cluster 未产出 {field}"

    async def test_cluster_edges_reflect_imports(self, repo: Path) -> None:
        nodes = make_real_nodes(make_settings())
        parsed = await nodes.parse({"workdir": str(repo)})  # type: ignore[operator,arg-type]
        result = await nodes.cluster(  # type: ignore[operator]
            {"workdir": str(repo), "parse_outcome": parsed["parse_outcome"]}  # type: ignore[arg-type]
        )
        graph = result["dependency_graph"]
        assert "pkg/core.py" in graph.edges["pkg/api.py"]  # type: ignore[union-attr,index]


class TestSelectFiles:
    async def test_select_files_fills_targets_and_basis(self, repo: Path) -> None:
        """basis 是 R17 要求的「检查范围说明」的一部分，不能只给路径列表。"""
        nodes = make_real_nodes(make_settings())
        parsed = await nodes.parse({"workdir": str(repo)})  # type: ignore[operator,arg-type]
        clustered = await nodes.cluster(  # type: ignore[operator]
            {"workdir": str(repo), "parse_outcome": parsed["parse_outcome"]}  # type: ignore[arg-type]
        )
        result = await nodes.select_files(  # type: ignore[operator]
            {
                "workdir": str(repo),
                "dependency_graph": clustered["dependency_graph"],
                "centrality": clustered["centrality"],
            }  # type: ignore[arg-type]
        )
        assert result["review_targets"]
        assert result["review_selection_basis"]


class TestNoProviderDegradation:
    """没有 provider 时各节点走自己的降级路径，而非整图失败。

    这个形态是调试静态层时的常用配置——能跑出骨架、评审的结构类检查与缺失说明。
    """

    async def test_planner_uses_rules_only(self, repo: Path) -> None:
        nodes = make_real_nodes(make_settings(), provider=None)
        parsed = await nodes.parse({"workdir": str(repo)})  # type: ignore[operator,arg-type]
        clustered = await nodes.cluster(  # type: ignore[operator]
            {"workdir": str(repo), "parse_outcome": parsed["parse_outcome"]}  # type: ignore[arg-type]
        )
        result = await nodes.planner({**clustered, "workdir": str(repo)})  # type: ignore[operator,arg-type]
        assert "未配置 LLM" in str(result["plan_rationale"])

    async def test_module_agent_records_failure_not_empty_analysis(self, repo: Path) -> None:
        """返回空分析会让报告以为这个模块「没什么可说的」。"""
        from backend.static_analysis.models import Module

        nodes = make_real_nodes(make_settings(), provider=None)
        result = await nodes.module_agent(  # type: ignore[operator]
            {
                "module": Module(
                    name="pkg",
                    files=("pkg/core.py",),
                    internal_edges=0,
                    external_edges=0,
                    origin="directory",
                ),
                "workdir": str(repo),
            }  # type: ignore[arg-type]
        )
        assert "module_failures" in result
        assert "未配置 LLM provider" in result["module_failures"][0].error  # type: ignore[index]

    async def test_synthesize_reports_reason(self, repo: Path) -> None:
        nodes = make_real_nodes(make_settings(), provider=None)
        result = await nodes.synthesize(  # type: ignore[operator]
            {
                "repo_url": "u",
                "workdir": str(repo),
                "parse_outcome": ParseOutcome(
                    parsed=[ParsedFile(path="pkg/core.py", language="python")]
                ),
            }  # type: ignore[arg-type]
        )
        assert "未配置 LLM provider" in result["report"].summary  # type: ignore[union-attr]

    async def test_missing_state_becomes_failure_not_crash(self, repo: Path) -> None:
        """节点缺输入时应转成 NodeFailure，而非让整图崩掉。

        observed 装饰器承担这个转换——实测确认节点抛异常会中断整图。
        """
        nodes = make_real_nodes(make_settings(), provider=None)
        result = await nodes.chunk_and_index({"workdir": str(repo)})  # type: ignore[operator,arg-type]
        assert "module_failures" in result
        assert "KeyError" in result["module_failures"][0].error  # type: ignore[index]


class TestIndexNode:
    """索引节点的接线。embedding 走假 provider——这里验证的是节点怎么用缓存，
    不是向量化本身（那由 test_indexer.py 覆盖）。
    """

    def _settings(self, tmp_path: Path):  # type: ignore[no-untyped-def]
        return make_settings(workspace_root=tmp_path / "ws")

    async def _run_index(self, repo: Path, settings, monkeypatch):  # type: ignore[no-untyped-def]
        calls: list[int] = []

        class _Fake:
            @property
            def identity(self) -> str:
                return "fake:m:v1"

            @property
            def dimension(self) -> int:
                return 8

            async def embed_texts(self, texts: list[str]) -> list[list[float]]:
                calls.append(len(texts))
                return [[0.1] * 8 for _ in texts]

        monkeypatch.setattr(
            "backend.graph.real_nodes.build_embedding_provider", lambda _s: _Fake()
        )
        nodes = make_real_nodes(settings, provider=None)
        parsed = await nodes.parse({"workdir": str(repo)})  # type: ignore[operator,arg-type]
        result = await nodes.chunk_and_index(  # type: ignore[operator]
            {
                "workdir": str(repo),
                "repo_url": "https://github.com/acme/widget",
                "commit_sha": "a" * 40,
                "parse_outcome": parsed["parse_outcome"],
            }  # type: ignore[arg-type]
        )
        return result, calls

    async def test_first_run_builds_index(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = self._settings(tmp_path)
        result, calls = await self._run_index(repo, settings, monkeypatch)
        assert result["index_cache_hit"] is False
        assert int(result["indexed_chunks"]) > 0  # type: ignore[arg-type]
        assert calls, "首次索引应调用 embedding"

    async def test_second_run_hits_cache_without_embedding(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AE3：二次提交同一仓库跳过向量化。"""
        settings = self._settings(tmp_path)
        await self._run_index(repo, settings, monkeypatch)
        result, calls = await self._run_index(repo, settings, monkeypatch)
        assert result["index_cache_hit"] is True
        assert calls == [], "命中缓存时不该有 embedding 调用"

    async def test_note_explains_cache_state(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R5 要求未命中时呈现进度，界面需要知道命中状态与原因。"""
        settings = self._settings(tmp_path)
        first, _ = await self._run_index(repo, settings, monkeypatch)
        assert "首次索引" in str(first["index_note"])
        second, _ = await self._run_index(repo, settings, monkeypatch)
        assert "命中" in str(second["index_note"])

    async def test_repo_url_variants_share_one_index(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同一仓库的不同地址写法指向同一份代码，不该各建一份索引。"""
        from backend.graph.real_nodes import _repo_slug

        assert _repo_slug("https://github.com/acme/widget") == "acme/widget"
        assert _repo_slug("https://github.com/acme/widget.git") == "acme/widget"


class TestGraphIntegration:
    async def test_full_graph_runs_without_provider(self, repo: Path) -> None:
        """整图在没有 LLM 的情况下跑通，产出静态骨架与缺失说明。

        这验证的是接线：九个节点的字段进出全部对得上，没有哪个节点因缺输入而炸。
        ingest 被跳过（它要网络），从 parse 起注入 state。
        """
        settings = make_settings()
        nodes = make_real_nodes(settings, provider=None)

        # 用 stub 的 ingest 替掉真实的——真实 ingest 要克隆仓库。
        from dataclasses import replace

        from backend.graph.observability import observed

        @observed("ingest")
        async def local_ingest(state: dict) -> dict:  # type: ignore[type-arg]
            from backend.graph.state import LanguageProfile

            return {
                "workdir": str(repo),
                "commit_sha": "0" * 40,
                "language_profile": LanguageProfile(
                    total_files=3, parseable_files=2, by_language={"Python": 2}
                ),
            }

        graph = build_analysis_graph(replace(nodes, ingest=local_ingest))  # type: ignore[arg-type]
        result = await graph.ainvoke(
            {"repo_url": "https://github.com/acme/widget"},
            {"configurable": {"thread_id": "wiring"}},
        )

        # 静态层的产出必须齐全。
        assert result["parse_outcome"].symbol_count > 0
        assert result["modules"]
        assert result["review_targets"]
        # 报告存在且说明了为什么没有内容。
        assert result["report"].missing is not None
        # 评审的结构类检查不依赖 LLM，应有产出。
        assert result["review_report"].outcomes

    async def test_every_returned_field_is_declared_in_state(self, repo: Path) -> None:
        """节点返回但未在 AnalysisState 声明的字段会被 LangGraph 静默丢弃。

        实测踩到过：review_report、review_selection_basis、index_note、admission_note
        四个字段因未声明而凭空消失——运行时不报错，只是下游读不到。这条测试把那类接线
        错误挡在这一层。
        """
        from backend.graph.state import AnalysisState

        settings = make_settings()
        nodes = make_real_nodes(settings, provider=None)
        declared = set(AnalysisState.__annotations__)

        from backend.graph.state import LanguageProfile

        # 逐个调用不需要网络的节点，检查它们的产出字段都已声明。
        parsed = await nodes.parse({"workdir": str(repo)})  # type: ignore[operator,arg-type]
        clustered = await nodes.cluster(  # type: ignore[operator]
            {"workdir": str(repo), "parse_outcome": parsed["parse_outcome"]}  # type: ignore[arg-type]
        )
        selected = await nodes.select_files(  # type: ignore[operator]
            {
                "workdir": str(repo),
                "dependency_graph": clustered["dependency_graph"],
                "centrality": clustered["centrality"],
            }  # type: ignore[arg-type]
        )
        indexed = await nodes.chunk_and_index({"workdir": str(repo)})  # type: ignore[operator,arg-type]
        reviewed = await nodes.reviewer(  # type: ignore[operator]
            {
                "workdir": str(repo),
                "dependency_graph": clustered["dependency_graph"],
                "modules": clustered["modules"],
                "entrypoints": clustered["entrypoints"],
                "review_targets": selected["review_targets"],
            }  # type: ignore[arg-type]
        )

        for label, payload in (
            ("parse", parsed),
            ("cluster", clustered),
            ("select_files", selected),
            ("chunk_and_index", indexed),
            ("reviewer", reviewed),
        ):
            undeclared = set(payload) - declared
            assert not undeclared, f"{label} 返回了未声明字段，会被静默丢弃：{undeclared}"

    async def test_events_recorded_for_every_node(self, repo: Path) -> None:
        """观测覆盖全部节点：漏一个就有一段执行过程在 trace 里看不见（R25）。"""
        settings = make_settings()
        nodes = make_real_nodes(settings, provider=None)

        from dataclasses import replace

        from backend.graph.observability import observed
        from backend.graph.state import LanguageProfile

        @observed("ingest")
        async def local_ingest(state: dict) -> dict:  # type: ignore[type-arg]
            return {
                "workdir": str(repo),
                "commit_sha": "x",
                "language_profile": LanguageProfile(),
            }

        graph = build_analysis_graph(replace(nodes, ingest=local_ingest))  # type: ignore[arg-type]
        result = await graph.ainvoke(
            {"repo_url": "u"}, {"configurable": {"thread_id": "events"}}
        )
        recorded = {e.node for e in result["events"]}
        assert {
            "ingest",
            "parse",
            "cluster",
            "planner",
            "synthesize",
            "select_files",
            "reviewer",
            "chunk_and_index",
        } <= recorded
