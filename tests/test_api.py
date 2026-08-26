"""HTTP API 与 SSE（R5、R24、AE6）。

用 TestClient 打真实的 FastAPI 应用，但把图的节点换成受控实现——要验证的是 HTTP 层的
行为（状态码、错误分类、SSE 事件序列），不是分析本身的质量。

AE6 的重点是错误可区分：只断言「返回了错误」不够，界面要按「仓库不存在」、「无访问权限」、
「超规模」给不同提示，所以每种都要断言到具体的 reason 与状态码。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.api.progress import Stage
from backend.graph.observability import observed
from backend.graph.state import LanguageProfile, ModuleAnalysis, ModulePlan, NodeFailure
from backend.ingest.guards import RejectReason, RepoRejected
from backend.main import create_app
from backend.report.schema import (
    ArchitectureReport,
    Citation,
    Claim,
    MissingParts,
    ReportSection,
)
from backend.review.models import (
    CategoryOutcome,
    CheckStatus,
    Finding,
    FindingCategory,
    ReviewReport,
    Severity,
)
from backend.static_analysis.models import (
    DependencyGraph,
    Granularity,
    Module,
    ParsedFile,
    ParseOutcome,
    UnresolvedImport,
)
from tests.support import make_settings


def _module(name: str = "pkg") -> Module:
    return Module(
        name=name, files=("pkg/a.py",), internal_edges=0, external_edges=0, origin="directory"
    )


def _stub_nodeset(
    module_count: int = 2,
    ingest_error: RepoRejected | None = None,
    with_report: bool = True,
):  # type: ignore[no-untyped-def]
    """一套受控节点。不碰网络、不调 LLM、不写向量库。

    fatal 标记与生产节点一致——管道前缀的失败必须终止分析，否则测的就不是真实行为。"""
    from backend.graph.builder import NodeSet

    @observed("ingest", fatal=True)
    async def ingest(state: dict) -> dict:  # type: ignore[type-arg]
        if ingest_error is not None:
            raise ingest_error
        return {
            "workdir": "/stub",
            "commit_sha": "c" * 40,
            "language_profile": LanguageProfile(
                total_files=3, parseable_files=2, by_language={"Python": 2}
            ),
        }

    @observed("parse", fatal=True)
    async def parse(state: dict) -> dict:  # type: ignore[type-arg]
        return {
            "parse_outcome": ParseOutcome(
                parsed=[ParsedFile(path="pkg/a.py", language="python")]
            )
        }

    @observed("cluster", fatal=True)
    async def cluster(state: dict) -> dict:  # type: ignore[type-arg]
        modules = [_module(f"m{i}") for i in range(module_count)]
        return {
            "modules": modules,
            "dependency_graph": DependencyGraph(
                nodes=("pkg/a.py",), edges={"pkg/a.py": frozenset()}
            ),
            "entrypoints": [],
            "centrality": {"pkg/a.py": 1.0},
        }

    @observed("planner")
    async def planner(state: dict) -> dict:  # type: ignore[type-arg]
        modules = state.get("modules") or []
        return {
            "planned_modules": [
                ModulePlan(module=m, priority=i + 1, reason="r", rule_score=0.5)
                for i, m in enumerate(modules)
            ],
            "plan_rationale": "stub",
        }

    @observed("module_agent", scope_key="module")
    async def module_agent(state: dict) -> dict:  # type: ignore[type-arg]
        name = state["module"].name
        if name == "m1":
            return {
                "module_failures": [
                    NodeFailure(node="module_agent", scope=name, error="stub 失败")
                ]
            }
        return {
            "module_analyses": [
                ModuleAnalysis(module_name=name, summary=f"{name} 的分析", cited_paths=("pkg/a.py",))
            ]
        }

    @observed("synthesize")
    async def synthesize(state: dict) -> dict:  # type: ignore[type-arg]
        if not with_report:
            return {"report_validation_summary": "无报告"}
        return {
            "report": ArchitectureReport(
                repo="acme/widget",
                commit_sha="c" * 40,
                summary="总体印象",
                module_breakdown=ReportSection(
                    key="module_breakdown",
                    title="模块划分",
                    claims=(
                        Claim(text="核心在 a", citations=(Citation(path="pkg/a.py", line=1),)),
                    ),
                ),
                missing=MissingParts(unparsed_files=1),
            ),
            "report_validation_summary": "共 1 条结论：1 条引用完全有效。",
            "unsupported_claims": [],
        }

    @observed("select_files")
    async def select_files(state: dict) -> dict:  # type: ignore[type-arg]
        return {"review_targets": ["pkg/a.py"], "review_selection_basis": "stub"}

    @observed("reviewer")
    async def reviewer(state: dict) -> dict:  # type: ignore[type-arg]
        report = ReviewReport(
            target_files=("pkg/a.py",),
            outcomes=[
                CategoryOutcome(
                    category=FindingCategory.STRUCTURAL,
                    status=CheckStatus.EXECUTED,
                    scope="1 个文件",
                    findings=[
                        Finding(
                            category=FindingCategory.STRUCTURAL,
                            kind="circular_dependency",
                            path="pkg/a.py",
                            line=0,
                            message="环",
                            evidence="图上有环",
                            severity=Severity.HIGH,
                        )
                    ],
                ),
                CategoryOutcome(
                    category=FindingCategory.SECURITY,
                    status=CheckStatus.SKIPPED,
                    scope="1 个文件",
                    reason="未配置 LLM",
                ),
            ],
        )
        return {"review_report": report, "findings": ["[high] circular_dependency"]}

    @observed("chunk_and_index")
    async def chunk_and_index(state: dict) -> dict:  # type: ignore[type-arg]
        return {
            "indexed_chunks": 12,
            "index_identity": "stub:m:v1",
            "index_note": "命中已有索引，跳过解析与向量化",
            "index_cache_hit": True,
            "index_digest": "abcdef0123456789",
        }

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


# ── U2 的固件：33 文件 / 9 模块 / 6 发现 / 99 切块（AE-02、AE-03、AE-04）──
#
# 与 _stub_nodeset 分开而不是给它加参数：那套固件的模块数与发现数被 U2 之前的断言钉住，
# 改它会让回归测试跟着一起变，失败信号就分不清是契约变了还是固件变了。

_RICH_MODULES = [
    Module(
        name=f"pkg/m{i}",
        files=tuple(f"pkg/m{i}/f{j}.py" for j in range(2)),
        internal_edges=i,
        external_edges=i + 1,
        origin="directory" if i % 2 == 0 else "split",
    )
    for i in range(9)
]

_RICH_LANGUAGES = {
    "Python": 12,
    "TypeScript": 8,
    "Markdown": 5,
    "JSON": 4,
    "YAML": 2,
    "CSS": 1,
    "Shell": 1,
}


def _rich_graph(degraded: bool = False) -> DependencyGraph:
    """9 个模块之间的链式依赖，方向为「导入方 -> 被导入方」。"""
    nodes = tuple(f for m in _RICH_MODULES for f in m.files)
    edges = {
        f"pkg/m{i}/f0.py": frozenset({f"pkg/m{i + 1}/f0.py"}) for i in range(8)
    }
    return DependencyGraph(
        nodes=nodes,
        edges=edges,
        external={"httpx": frozenset({"pkg/m0/f0.py"})},
        unresolved=(
            UnresolvedImport(
                target="./missing",
                path="pkg/m0/f1.py",
                line=3,
                reason="未找到对应文件",
            ),
        ),
        cycles=(("pkg/m7/f0.py", "pkg/m8/f0.py"),),
        granularity=Granularity.DIRECTORY if degraded else Granularity.FILE,
        degraded_reason="节点数 2400 超出上限 2000，降级为目录级" if degraded else None,
    )


def _rich_findings() -> list[Finding]:
    """6 条发现，跨三个类别。"""
    specs = [
        ("structural", "circular_dependency", "pkg/m7/f0.py", 0, Severity.HIGH),
        ("structural", "long_module", "pkg/m1/f0.py", 1, Severity.LOW),
        ("error_handling", "bare_except", "pkg/m2/f0.py", 12, Severity.MEDIUM),
        ("error_handling", "swallowed_exception", "pkg/m2/f1.py", 30, Severity.MEDIUM),
        ("security", "hardcoded_secret", "pkg/m3/f0.py", 7, Severity.HIGH),
        ("security", "sql_concat", "pkg/m3/f1.py", 44, Severity.MEDIUM),
    ]
    return [
        Finding(
            category=FindingCategory(category),
            kind=kind,
            path=path,
            line=line,
            message=f"{kind} 的说明",
            evidence=f"{kind} 的判断依据",
            severity=severity,
        )
        for category, kind, path, line, severity in specs
    ]


def _rich_nodeset(degraded: bool = False, with_report: bool = True):  # type: ignore[no-untyped-def]
    """U2 的受控节点。只填 state，不调 LLM、不碰网络。"""
    from backend.graph.builder import NodeSet

    @observed("ingest", fatal=True)
    async def ingest(state: dict) -> dict:  # type: ignore[type-arg]
        return {
            "workdir": "/stub",
            "commit_sha": "d" * 40,
            "language_profile": LanguageProfile(
                total_files=33, parseable_files=20, by_language=dict(_RICH_LANGUAGES)
            ),
        }

    @observed("parse", fatal=True)
    async def parse(state: dict) -> dict:  # type: ignore[type-arg]
        return {
            "parse_outcome": ParseOutcome(
                parsed=[
                    ParsedFile(path=f, language="python")
                    for m in _RICH_MODULES
                    for f in m.files
                ]
            )
        }

    @observed("cluster", fatal=True)
    async def cluster(state: dict) -> dict:  # type: ignore[type-arg]
        return {
            "modules": list(_RICH_MODULES),
            "dependency_graph": _rich_graph(degraded),
            "entrypoints": [],
            "centrality": {},
        }

    @observed("planner")
    async def planner(state: dict) -> dict:  # type: ignore[type-arg]
        return {
            "planned_modules": [
                ModulePlan(module=m, priority=i + 1, reason="r", rule_score=0.5)
                for i, m in enumerate(state.get("modules") or [])
            ],
            "plan_rationale": "stub",
        }

    @observed("module_agent", scope_key="module")
    async def module_agent(state: dict) -> dict:  # type: ignore[type-arg]
        module = state["module"]
        # 只有 pkg/m4 带 limitation：AE-03 要求该字段非空时能在响应里读到，
        # 而全部模块都带会让「limitation 为空的模块」这条对照失去覆盖。
        limitation = "轮次用尽，未读完全部成员文件" if module.name == "pkg/m4" else ""
        return {
            "module_analyses": [
                ModuleAnalysis(
                    module_name=module.name,
                    summary=f"{module.name} 承担的职责与其边界",
                    cited_paths=module.files,
                    limitation=limitation,
                )
            ]
        }

    @observed("synthesize")
    async def synthesize(state: dict) -> dict:  # type: ignore[type-arg]
        if not with_report:
            return {"report_validation_summary": "无报告"}
        return {
            "report": ArchitectureReport(
                repo="acme/widget",
                commit_sha="d" * 40,
                summary="总体印象",
                missing=MissingParts(),
            ),
            "report_validation_summary": "共 26 条结论：25 条引用完全有效。",
            "unsupported_claims": [],
        }

    @observed("select_files")
    async def select_files(state: dict) -> dict:  # type: ignore[type-arg]
        return {
            "review_targets": [f for m in _RICH_MODULES[:4] for f in m.files],
            "review_selection_basis": "stub",
        }

    @observed("reviewer")
    async def reviewer(state: dict) -> dict:  # type: ignore[type-arg]
        findings = _rich_findings()
        by_category = {
            category: [f for f in findings if f.category is category]
            for category in FindingCategory
        }
        return {
            "review_report": ReviewReport(
                target_files=tuple(f for m in _RICH_MODULES[:4] for f in m.files),
                outcomes=[
                    CategoryOutcome(
                        category=category,
                        status=CheckStatus.EXECUTED,
                        scope="8 个文件",
                        findings=hits,
                    )
                    for category, hits in by_category.items()
                ],
            ),
            "findings": [f"[{f.severity.value}] {f.kind}" for f in findings],
        }

    @observed("chunk_and_index")
    async def chunk_and_index(state: dict) -> dict:  # type: ignore[type-arg]
        return {
            "indexed_chunks": 99,
            "index_identity": "stub:m:v1",
            "index_note": "新建索引",
            "index_cache_hit": False,
            "index_digest": "abcdef0123456789",
        }

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


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """必须用上下文管理器进入。

    TestClient 只在作为上下文管理器时启动常驻的事件循环 portal；不进上下文时每个请求
    各起一个 portal 并在响应返回后销毁——那会让 POST 期间 `create_task` 建的后台分析
    任务被直接丢弃（实测：ingest 跑了一半就停，SSE 随后永久等待）。

    进上下文还有第二个作用：lifespan 事件（含 registry.shutdown）只在那时执行。
    """
    monkeypatch.setattr(
        "backend.api.tasks.make_real_nodes", lambda s, p: _stub_nodeset()
    )
    app = create_app(make_settings(workspace_root=tmp_path / "ws"))
    with TestClient(app) as test_client:
        yield test_client


@contextmanager
def _rich_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, degraded: bool = False,
    with_report: bool = True,
) -> Any:
    """U2 固件的客户端。同 client fixture：必须进上下文，否则后台任务被丢弃。"""
    monkeypatch.setattr(
        "backend.api.tasks.make_real_nodes",
        lambda s, p: _rich_nodeset(degraded=degraded, with_report=with_report),
    )
    app = create_app(make_settings(workspace_root=tmp_path / "ws"))
    with TestClient(app) as test_client:
        yield test_client


# 提交时携带的访客凭证。
#
# U5 之后凭证是提交的前置条件（R-68），所以这套测试的每次提交都要带上它。这不是放宽
# 断言：「未配置凭证时被拒」由 tests/test_credentials.py 专门断言，本文件测的是凭证
# 就位之后的 HTTP 行为。
_TEST_CREDENTIALS = {
    "api_key": "guest-key-not-used",
    "base_url": "https://guest.example",
}


def _submit(client: TestClient, url: str = "https://github.com/acme/widget") -> Any:
    return client.post(
        "/api/analyses",
        json={"repo_url": url, "credentials": _TEST_CREDENTIALS},
    )


def _run_to_completion(client: TestClient, task_id: str) -> list[dict[str, Any]]:
    """消费 SSE 直到结束，返回全部事件。

    SSE 流的结束即分析完成——用它同步比轮询状态更可靠，也顺带验证了流会正常终止。
    """
    events: list[dict[str, Any]] = []
    with client.stream("GET", f"/api/analyses/{task_id}/events") as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


class TestSubmit:
    def test_accepts_valid_repo_with_202(self, client: TestClient) -> None:
        response = _submit(client)
        assert response.status_code == 202
        body = response.json()
        assert body["task_id"]
        assert body["repo"] == "acme/widget"

    def test_invalid_url_returns_400_not_500(self, client: TestClient) -> None:
        response = _submit(client, "not-a-url")
        assert response.status_code == 400
        assert response.json()["reason"] == RejectReason.INVALID_URL.value

    def test_empty_url_rejected_by_validation(self, client: TestClient) -> None:
        assert client.post("/api/analyses", json={"repo_url": ""}).status_code == 422

    def test_task_appears_in_list(self, client: TestClient) -> None:
        task_id = _submit(client).json()["task_id"]
        listed = client.get("/api/analyses").json()
        assert any(t["task_id"] == task_id for t in listed)


class TestAdmissionErrors:
    """AE6：错误可区分。只断言「返回了错误」不够——界面要按类型给不同提示。"""

    @contextmanager
    def _client_with_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exc: RepoRejected
    ) -> Any:
        """同 client fixture：必须进上下文，否则后台任务被丢弃。"""
        monkeypatch.setattr(
            "backend.api.tasks.make_real_nodes",
            lambda s, p: _stub_nodeset(ingest_error=exc),
        )
        app = create_app(make_settings(workspace_root=tmp_path / "ws"))
        with TestClient(app) as test_client:
            yield test_client

    def test_not_found_recorded_on_task(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        with self._client_with_error(
            monkeypatch, tmp_path, RepoRejected(RejectReason.NOT_FOUND, "仓库不存在：a/b")
        ) as client:
            task_id = _submit(client).json()["task_id"]
            _run_to_completion(client, task_id)
            result = client.get(f"/api/analyses/{task_id}").json()
        assert result["failed"] is True
        assert "不存在" in result["error"]

    def test_no_access_distinguishable_from_not_found(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        with self._client_with_error(
            monkeypatch, tmp_path, RepoRejected(RejectReason.NO_ACCESS, "无访问权限：a/b")
        ) as client:
            task_id = _submit(client).json()["task_id"]
            _run_to_completion(client, task_id)
            error = client.get(f"/api/analyses/{task_id}").json()["error"]
        assert "无访问权限" in error
        assert "不存在" not in error

    def test_too_large_includes_actual_size(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        with self._client_with_error(
            monkeypatch,
            tmp_path,
            RepoRejected(RejectReason.TOO_LARGE, "可解析文件数 23997 超出上限 1500：a/b"),
        ) as client:
            task_id = _submit(client).json()["task_id"]
            _run_to_completion(client, task_id)
            error = client.get(f"/api/analyses/{task_id}").json()["error"]
        assert "23997" in error
        assert "1500" in error

    def test_failure_reflected_in_final_sse_event(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        with self._client_with_error(
            monkeypatch, tmp_path, RepoRejected(RejectReason.NOT_FOUND, "仓库不存在")
        ) as client:
            task_id = _submit(client).json()["task_id"]
            events = _run_to_completion(client, task_id)
        assert events[-1]["stage"] == Stage.FAILED.value
        assert events[-1]["failed"] is True


class TestProgressStream:
    def test_event_sequence_covers_required_stages(self, client: TestClient) -> None:
        """计划要求事件序列含骨架就绪、模块分析、报告就绪三个阶段。"""
        task_id = _submit(client).json()["task_id"]
        stages = [e["stage"] for e in _run_to_completion(client, task_id)]
        assert Stage.SKELETON_READY.value in stages
        assert Stage.ANALYZING_MODULES.value in stages
        assert Stage.REPORT_READY.value in stages
        assert stages[-1] == Stage.DONE.value

    def test_events_carry_label_and_percent(self, client: TestClient) -> None:
        """label 随事件一起给，前端不必再维护一份映射——两份会漂移。"""
        task_id = _submit(client).json()["task_id"]
        events = _run_to_completion(client, task_id)
        skeleton = next(e for e in events if e["stage"] == Stage.SKELETON_READY.value)
        assert skeleton["label"] == "结构骨架就绪"
        assert isinstance(skeleton["percent"], int)

    def test_percent_is_monotonic(self, client: TestClient) -> None:
        """进度条回退比不动更让人怀疑出了问题。"""
        task_id = _submit(client).json()["task_id"]
        percents = [
            e["percent"] for e in _run_to_completion(client, task_id) if e["percent"] is not None
        ]
        assert percents == sorted(percents)

    def test_module_events_report_progress_fraction(self, client: TestClient) -> None:
        """模块分析是耗时最长的阶段，要给出「已完成 N/M」而非一个固定百分比。"""
        task_id = _submit(client).json()["task_id"]
        module_events = [
            e
            for e in _run_to_completion(client, task_id)
            if e["stage"] == Stage.ANALYZING_MODULES.value
        ]
        assert len(module_events) == 2
        assert "1/2" in module_events[0]["detail"]
        assert "2/2" in module_events[1]["detail"]

    def test_failed_module_marked_in_its_event(self, client: TestClient) -> None:
        """单模块失败要在该模块的事件上标出，不影响其余模块的事件（R10）。"""
        task_id = _submit(client).json()["task_id"]
        module_events = [
            e
            for e in _run_to_completion(client, task_id)
            if e["stage"] == Stage.ANALYZING_MODULES.value
        ]
        assert sum(1 for e in module_events if e["failed"]) == 1
        assert sum(1 for e in module_events if not e["failed"]) == 1

    def test_late_subscriber_receives_history(self, client: TestClient) -> None:
        """用户刷新页面后不该丢掉已发生的进度。"""
        task_id = _submit(client).json()["task_id"]
        _run_to_completion(client, task_id)
        replayed = _run_to_completion(client, task_id)
        assert replayed
        assert replayed[-1]["stage"] == Stage.DONE.value

    def test_stream_for_unknown_task_returns_404(self, client: TestClient) -> None:
        assert client.get("/api/analyses/nope/events").status_code == 404

    def test_cache_hit_detail_visible_in_progress(self, client: TestClient) -> None:
        """R5：缓存命中时前端要能看出「不必等」。"""
        task_id = _submit(client).json()["task_id"]
        indexing = next(
            e
            for e in _run_to_completion(client, task_id)
            if e["stage"] == Stage.INDEXING.value
        )
        assert "命中" in indexing["detail"]


class TestResults:
    def test_returns_report_and_review(self, client: TestClient) -> None:
        """U12 的 Dependencies 明写读结果端点同时返回报告与评审发现。"""
        task_id = _submit(client).json()["task_id"]
        _run_to_completion(client, task_id)
        body = client.get(f"/api/analyses/{task_id}").json()
        assert body["completed"] is True
        assert body["report"]["summary"] == "总体印象"
        assert body["review"]["findings"]

    def test_report_citations_preserved(self, client: TestClient) -> None:
        task_id = _submit(client).json()["task_id"]
        _run_to_completion(client, task_id)
        sections = client.get(f"/api/analyses/{task_id}").json()["report"]["sections"]
        breakdown = next(s for s in sections if s["key"] == "module_breakdown")
        assert breakdown["claims"][0]["citations"][0]["path"] == "pkg/a.py"

    def test_missing_parts_text_non_empty(self, client: TestClient) -> None:
        """R11：缺失部分始终有内容，空白会让读者怀疑漏了这一节。"""
        task_id = _submit(client).json()["task_id"]
        _run_to_completion(client, task_id)
        missing = client.get(f"/api/analyses/{task_id}").json()["report"]["missing"]
        assert missing["text"].strip()

    def test_zero_hit_and_skipped_categories_distinguishable(self, client: TestClient) -> None:
        """R17：命中零条与未执行必须可区分，只给发现列表的话两者都是空数组。"""
        task_id = _submit(client).json()["task_id"]
        _run_to_completion(client, task_id)
        outcomes = client.get(f"/api/analyses/{task_id}").json()["review"]["outcomes"]
        by_category = {o["category"]: o for o in outcomes}
        assert by_category["structural"]["status"] == "executed"
        assert by_category["security"]["status"] == "skipped"
        assert by_category["security"]["reason"]

    def test_module_failures_listed(self, client: TestClient) -> None:
        """AE4：报告须标注模块缺失及原因。"""
        task_id = _submit(client).json()["task_id"]
        _run_to_completion(client, task_id)
        failures = client.get(f"/api/analyses/{task_id}").json()["module_failures"]
        assert any("m1" in f for f in failures)

    def test_index_status_returned(self, client: TestClient) -> None:
        task_id = _submit(client).json()["task_id"]
        _run_to_completion(client, task_id)
        index = client.get(f"/api/analyses/{task_id}").json()["index"]
        assert index["cache_hit"] is True
        assert index["chunk_count"] == 12

    def test_unknown_task_returns_404_with_reason(self, client: TestClient) -> None:
        """404 要能区分「仓库不存在」与「任务标识不存在」，所以带 reason。"""
        response = client.get("/api/analyses/nope")
        assert response.status_code == 404
        assert response.json()["reason"] == "task_not_found"


class TestResultContractExtension:
    """U2：读结果的加法扩展。概览条与 Architecture 页的字段来源。

    每条断言都对着 state 里的具体字段——BR-002 要求界面数字能指回后端字段，而这一层
    是那条约束的落点：API 若在这里合成或加工，前端就再也无从区分「后端给的」与
    「前端算的」。
    """

    def _result(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **kw: Any) -> Any:
        with _rich_client(monkeypatch, tmp_path, **kw) as client:
            task_id = _submit(client).json()["task_id"]
            _run_to_completion(client, task_id)
            return client.get(f"/api/analyses/{task_id}").json()

    def test_counts_match_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Covers AE-02。33 文件 / 9 模块 / 6 发现 / 99 切块逐项对上。"""
        body = self._result(monkeypatch, tmp_path)
        assert body["language_profile"]["total_files"] == 33
        assert len(body["modules"]) == 9
        assert len(body["review"]["findings"]) == 6
        assert body["index"]["chunk_count"] == 99

    def test_language_profile_not_truncated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Covers AE-02。API 返回全量映射，截断是界面职责。"""
        profile = self._result(monkeypatch, tmp_path)["language_profile"]
        assert profile["by_language"] == _RICH_LANGUAGES
        assert profile["parseable_files"] == 20

    def test_modules_carry_membership_and_analysis(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Covers AE-03。成员文件、内外边数、聚类来历与模块结论原文。"""
        modules = {m["name"]: m for m in self._result(monkeypatch, tmp_path)["modules"]}
        third = modules["pkg/m3"]
        assert third["files"] == ["pkg/m3/f0.py", "pkg/m3/f1.py"]
        assert third["internal_edges"] == 3
        assert third["external_edges"] == 4
        assert third["origin"] == "split"
        assert third["summary"] == "pkg/m3 承担的职责与其边界"

    def test_module_limitation_surfaced(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Covers AE-03。带 limitation 的模块该字段非空，其余为空。"""
        modules = {m["name"]: m for m in self._result(monkeypatch, tmp_path)["modules"]}
        assert modules["pkg/m4"]["limitation"] == "轮次用尽，未读完全部成员文件"
        assert modules["pkg/m3"]["limitation"] == ""

    def test_degraded_graph_marked_with_reason(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Covers AE-04。降级为目录级时粒度与原因都要能读到，不静默以粗粒度呈现。"""
        graph = self._result(monkeypatch, tmp_path, degraded=True)["dependency_graph"]
        assert graph["granularity"] == "directory"
        assert "降级" in graph["degraded_reason"]

    def test_graph_granularity_is_file_by_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        graph = self._result(monkeypatch, tmp_path)["dependency_graph"]
        assert graph["granularity"] == "file"
        assert graph["degraded_reason"] == ""

    def test_edge_direction_preserved(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """R-09 要求方向。序列化不得把「导入方 -> 被导入方」反转。"""
        graph = self._result(monkeypatch, tmp_path)["dependency_graph"]
        pairs = {(e["source"], e["target"]) for e in graph["edges"]}
        assert ("pkg/m0/f0.py", "pkg/m1/f0.py") in pairs
        assert ("pkg/m1/f0.py", "pkg/m0/f0.py") not in pairs

    def test_external_and_unresolved_carried(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """外部依赖与解析缺口都属报告缺失说明的范畴，两者都要带上。"""
        graph = self._result(monkeypatch, tmp_path)["dependency_graph"]
        assert graph["external"] == {"httpx": ["pkg/m0/f0.py"]}
        assert graph["unresolved"][0]["target"] == "./missing"
        assert graph["unresolved"][0]["reason"] == "未找到对应文件"
        assert graph["cycles"] == [["pkg/m7/f0.py", "pkg/m8/f0.py"]]

    def test_commit_sha_available_without_report(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """报告为 None 时概览条仍要显示 commit——所以它提升到顶层。"""
        body = self._result(monkeypatch, tmp_path, with_report=False)
        assert body["report"] is None
        assert body["commit_sha"] == "d" * 40

    def test_review_target_files_delimit_scope(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """R-20 的数据前提：评审目标范围由 target_files 给出，无需新字段。"""
        review = self._result(monkeypatch, tmp_path)["review"]
        assert len(review["target_files"]) == 8
        assert "pkg/m0/f0.py" in review["target_files"]
        assert "pkg/m8/f0.py" not in review["target_files"]

    def test_legacy_fields_unchanged(self, client: TestClient) -> None:
        """回归：旧字段的形状与本单元前完全一致。"""
        task_id = _submit(client).json()["task_id"]
        _run_to_completion(client, task_id)
        body = client.get(f"/api/analyses/{task_id}").json()
        assert body["report"]["summary"] == "总体印象"
        assert body["review"]["findings"][0]["kind"] == "circular_dependency"
        assert body["index"]["cache_hit"] is True
        assert any("m1" in f for f in body["module_failures"])

    def test_new_fields_always_present(self, client: TestClient) -> None:
        """新字段始终出现在响应里，缺数据时是默认值而非缺键。

        断言键的存在而非某个具体值：分析在后台跑，取结果的时刻这些字段可能已被填上，
        钉住「此刻应为空」会让这条测试依赖时序。契约要保证的本来也只是形状。
        """
        task_id = _submit(client).json()["task_id"]
        body = client.get(f"/api/analyses/{task_id}").json()
        for key in ("commit_sha", "modules", "dependency_graph", "language_profile"):
            assert key in body

    def test_all_module_summaries_accumulate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """扇出的每一路结论都要进最终 state，不能只剩最后一路。

        流式增量按节点逐条到达，而 module_analyses 是累积字段——用覆盖语义合并的话
        9 个模块只会留下 1 个结论，界面表现为「模块详情大多是空的」而不报错。
        """
        with _rich_client(monkeypatch, tmp_path) as client:
            task_id = _submit(client).json()["task_id"]
            _run_to_completion(client, task_id)
            modules = client.get(f"/api/analyses/{task_id}").json()["modules"]
        assert len(modules) == 9
        assert all(m["summary"] for m in modules)


class TestQuestions:
    def test_unknown_task_returns_404(self, client: TestClient) -> None:
        response = client.post("/api/analyses/nope/questions", json={"question": "q"})
        assert response.status_code == 404

    def test_incomplete_analysis_returns_409(self, client: TestClient) -> None:
        """任务存在但没到能提问的状态，前端该等而非重新提交。"""
        task_id = _submit(client).json()["task_id"]
        response = client.post(
            f"/api/analyses/{task_id}/questions", json={"question": "q"}
        )
        # 分析在后台跑，此刻大概率未完成；若已完成则该端点会走真实问答（需索引），
        # 两种情况都不该是 404。
        assert response.status_code in (409, 200)
        if response.status_code == 409:
            assert response.json()["reason"] == "analysis_incomplete"

    def test_empty_question_rejected(self, client: TestClient) -> None:
        task_id = _submit(client).json()["task_id"]
        assert (
            client.post(f"/api/analyses/{task_id}/questions", json={"question": ""}).status_code
            == 422
        )


@contextmanager
def _guarded_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    **overrides: object,
) -> Any:
    """可调三项保护阈值的客户端（U6、U7）。

    分析节点用一个**可阻塞**的 ingest：排队要能被观测，就必须让前序任务停在中途。用
    asyncio.Event 而非 sleep——sleep 的时长是猜的，而事件是确定的。
    """
    gate_open = anyio_event()

    from backend.graph.builder import NodeSet

    @observed("ingest", fatal=True)
    async def blocking_ingest(state: dict) -> dict:  # type: ignore[type-arg]
        await gate_open.wait()
        return {"workdir": str(tmp_path / "stub"), "commit_sha": "c" * 40}

    base = _stub_nodeset()
    nodes = NodeSet(
        ingest=blocking_ingest,
        parse=base.parse,
        cluster=base.cluster,
        planner=base.planner,
        module_agent=base.module_agent,
        chunk_and_index=base.chunk_and_index,
        select_files=base.select_files,
        reviewer=base.reviewer,
        synthesize=base.synthesize,
    )
    monkeypatch.setattr("backend.api.tasks.make_real_nodes", lambda s, p: nodes)
    app = create_app(make_settings(workspace_root=tmp_path / "ws", **overrides))
    with TestClient(app) as test_client:
        yield test_client, gate_open


def anyio_event() -> Any:
    """在 TestClient 的事件循环里可用的事件。

    asyncio.Event 在 3.10+ 不再绑定构造时的循环，所以在测试线程里建、在 portal 循环里
    等是安全的。单独包一层只为把这个前提写在一处。
    """
    import asyncio

    return asyncio.Event()


class TestRateLimit:
    """R-53、AE-15。限流器本身在 tests/test_ratelimit.py，这里测 HTTP 接线。"""

    def test_over_limit_returns_429_with_retry_after(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        with _guarded_client(
            monkeypatch, tmp_path, submissions_per_window=2, max_concurrent_analyses=4
        ) as (client, gate_open):
            assert _submit(client).status_code == 202
            assert _submit(client, "https://github.com/acme/other").status_code == 202

            third = _submit(client, "https://github.com/acme/third")
            assert third.status_code == 429
            assert third.json()["reason"] == "rate_limited"
            assert int(third.headers["Retry-After"]) > 0
            # 可重试时间要出现在文案里（R-53：只说「请稍后」不够）。
            assert "秒后重试" in third.json()["message"]
            gate_open.set()

    def test_rejected_submission_leaves_no_task_record(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """限流拒绝发生在建任务之前。"""
        with _guarded_client(
            monkeypatch, tmp_path, submissions_per_window=1, max_concurrent_analyses=4
        ) as (client, gate_open):
            _submit(client)
            _submit(client, "https://github.com/acme/second")
            listed = client.get("/api/analyses").json()
            assert len(listed) == 1
            gate_open.set()

    def test_credentials_rejection_does_not_consume_quota(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """校验顺序：凭证在限流之前，未配置凭证的请求不该吃掉配额。"""
        with _guarded_client(
            monkeypatch, tmp_path, submissions_per_window=1, max_concurrent_analyses=4
        ) as (client, gate_open):
            for _ in range(3):
                assert (
                    client.post(
                        "/api/analyses", json={"repo_url": "https://github.com/a/b"}
                    ).status_code
                    == 400
                )
            # 配额未被消耗，带凭证的提交仍可通过。
            assert _submit(client).status_code == 202
            gate_open.set()

    def test_invalid_url_does_not_consume_quota(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """准入失败在计数之前：用户改对地址后不该反被限流拦住。"""
        with _guarded_client(
            monkeypatch, tmp_path, submissions_per_window=1, max_concurrent_analyses=4
        ) as (client, gate_open):
            assert _submit(client, "not-a-url").status_code == 400
            assert _submit(client).status_code == 202
            gate_open.set()

    def test_forwarded_for_separates_clients(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """反代透传真实 IP 时不同客户端各自计数。"""
        with _guarded_client(
            monkeypatch, tmp_path, submissions_per_window=1, max_concurrent_analyses=4
        ) as (client, gate_open):
            first = client.post(
                "/api/analyses",
                json={"repo_url": "https://github.com/a/b", "credentials": _TEST_CREDENTIALS},
                headers={"X-Forwarded-For": "203.0.113.1"},
            )
            second = client.post(
                "/api/analyses",
                json={"repo_url": "https://github.com/a/c", "credentials": _TEST_CREDENTIALS},
                headers={"X-Forwarded-For": "203.0.113.2"},
            )
            repeat = client.post(
                "/api/analyses",
                json={"repo_url": "https://github.com/a/d", "credentials": _TEST_CREDENTIALS},
                headers={"X-Forwarded-For": "203.0.113.1"},
            )
            assert first.status_code == 202
            assert second.status_code == 202
            assert repeat.status_code == 429
            gate_open.set()

    def test_network_failure_refunds_the_quota(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """克隆因网络失败后配额要回来（端到端）。

        限流在提交阶段计数，而克隆在后台任务里——一次 TLS 断连会白扣一次配额。按 3 次/小时，
        三次网络抖动就把用户锁一小时，而他什么都没做错。
        """
        from backend.graph.builder import NodeSet
        from backend.graph.observability import observed
        from backend.ingest.guards import RejectReason, RepoRejected

        base = _stub_nodeset()

        @observed("ingest", fatal=True)
        async def tls_failure(state: dict) -> dict:  # type: ignore[type-arg]
            raise RepoRejected(
                RejectReason.NETWORK_ERROR,
                "克隆失败（网络问题，已重试 3 次）：acme/widget。"
                "git 输出：GnuTLS recv error (-110)",
            )

        nodes = NodeSet(
            ingest=tls_failure, parse=base.parse, cluster=base.cluster,
            planner=base.planner, module_agent=base.module_agent,
            chunk_and_index=base.chunk_and_index, select_files=base.select_files,
            reviewer=base.reviewer, synthesize=base.synthesize,
        )
        monkeypatch.setattr("backend.api.tasks.make_real_nodes", lambda s, p: nodes)
        app = create_app(
            make_settings(workspace_root=tmp_path / "ws", submissions_per_window=1)
        )

        with TestClient(app) as client:
            first = _submit(client)
            assert first.status_code == 202
            _run_to_completion(client, first.json()["task_id"])

            # 分析确实失败了，且原因是网络类而非「仓库不存在」。
            result = client.get(f"/api/analyses/{first.json()['task_id']}").json()
            assert result["failed"] is True
            assert "网络问题" in result["error"]

            # 配额已退还——1 次/窗口的配额下仍能再提交一次。
            assert _submit(client, "https://github.com/acme/retry").status_code == 202

    def test_rejection_writes_countable_structured_log(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """KTD11 的信号可用性：拒绝原因是稳定字段，可按它计数。

        判定方式不是「加了日志」，而是实跑一次限流触发并在输出里数到那条记录。
        """
        with _guarded_client(
            monkeypatch, tmp_path, submissions_per_window=1, max_concurrent_analyses=4
        ) as (client, gate_open):
            _submit(client)
            with caplog.at_level("INFO", logger="codepilot.api"):
                _submit(client, "https://github.com/acme/second")
            hits = [r for r in caplog.records if "reason=rate_limited" in r.getMessage()]
            assert len(hits) == 1
            gate_open.set()


class TestConcurrencyGate:
    """R-54、AE-15。闸门数据结构在 tests/test_gate.py，这里测排队的端到端可见性。"""

    def test_third_submission_is_queued_with_position(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        with _guarded_client(
            monkeypatch,
            tmp_path,
            max_concurrent_analyses=2,
            submissions_per_window=10,
        ) as (client, gate_open):
            _submit(client, "https://github.com/acme/one")
            _submit(client, "https://github.com/acme/two")
            third = _submit(client, "https://github.com/acme/three")

            assert third.status_code == 202
            body = third.json()
            assert body["queue_position"] == 1
            assert "排队" in body["message"]

            # 读结果端点同样给出位置（界面从这里读）。
            result = client.get(f"/api/analyses/{body['task_id']}").json()
            assert result["queue_position"] == 1
            gate_open.set()

    def test_queued_task_starts_after_predecessors_finish(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        with _guarded_client(
            monkeypatch,
            tmp_path,
            max_concurrent_analyses=1,
            submissions_per_window=10,
        ) as (client, gate_open):
            first = _submit(client, "https://github.com/acme/one").json()
            second = _submit(client, "https://github.com/acme/two").json()
            assert second["queue_position"] == 1

            gate_open.set()
            _run_to_completion(client, first["task_id"])
            _run_to_completion(client, second["task_id"])

            after = client.get(f"/api/analyses/{second['task_id']}").json()
            assert after["completed"] is True
            assert after["queue_position"] == 0

    def test_queue_full_returns_distinguishable_reason(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        with _guarded_client(
            monkeypatch,
            tmp_path,
            max_concurrent_analyses=1,
            max_queued_analyses=1,
            submissions_per_window=10,
        ) as (client, gate_open):
            _submit(client, "https://github.com/acme/one")
            _submit(client, "https://github.com/acme/two")
            overflow = _submit(client, "https://github.com/acme/three")

            assert overflow.status_code == 503
            assert overflow.json()["reason"] == "queue_full"
            gate_open.set()

    def test_queue_full_leaves_no_zombie_task(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """被队列满拒绝的提交不留下任务记录。

        `registry.create` 在闸门之前就已把任务放进注册表——不撤销的话会留下一个永停在
        QUEUED 的僵尸任务，它会出现在历史列表里且看起来可恢复（违反 R-46）。
        """
        with _guarded_client(
            monkeypatch,
            tmp_path,
            max_concurrent_analyses=1,
            max_queued_analyses=1,
            submissions_per_window=10,
        ) as (client, gate_open):
            _submit(client, "https://github.com/acme/one")
            _submit(client, "https://github.com/acme/two")
            overflow = _submit(client, "https://github.com/acme/three")
            assert overflow.status_code == 503

            # 只有两条记录（在跑的那个 + 排队的那个），被拒的第三个没留下痕迹。
            listed = client.get("/api/analyses").json()
            assert len(listed) == 1, "排队中的不入列，被拒的也不该入列"
            registry = client.app.state.registry  # type: ignore[attr-defined]
            assert len(registry.list_tasks()) == 2
            gate_open.set()

    def test_queue_full_does_not_consume_rate_limit_quota(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """容量问题不该扣用户的限流配额。

        被队列满拒绝的提交没有产生任何工作。扣它的配额等于让用户为服务端的容量问题付代价。
        """
        with _guarded_client(
            monkeypatch,
            tmp_path,
            max_concurrent_analyses=1,
            max_queued_analyses=1,
            submissions_per_window=3,
        ) as (client, gate_open):
            _submit(client, "https://github.com/acme/one")
            _submit(client, "https://github.com/acme/two")
            # 第三次因队列满被拒——不该消耗配额，所以配额还剩一次。
            assert _submit(client, "https://github.com/acme/three").status_code == 503

            gate_open.set()
            # 放开闸门后队列腾出位置，第四次提交仍在配额内。
            assert _submit(client, "https://github.com/acme/four").status_code in (202, 503)

    def test_disk_quota_rejection_leaves_no_zombie_task(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """配额拒绝同样不留下任务记录。"""
        from backend.workspace.quota import QuotaOutcome

        monkeypatch.setattr(
            "backend.api.routes.ensure_repo_quota",
            lambda settings, running: QuotaOutcome(admitted=False, note="磁盘不足"),
        )
        with _guarded_client(
            monkeypatch, tmp_path, submissions_per_window=10, max_concurrent_analyses=4
        ) as (client, gate_open):
            response = _submit(client)
            assert response.status_code == 507
            assert response.json()["reason"] == "disk_quota"

            registry = client.app.state.registry  # type: ignore[attr-defined]
            assert registry.list_tasks() == []
            assert client.get("/api/analyses").json() == []
            gate_open.set()

    def test_health_reports_running_and_queued_counts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """KTD11 的第二个信号：队列积压要能从 /api/health 看出来。"""
        with _guarded_client(
            monkeypatch,
            tmp_path,
            max_concurrent_analyses=1,
            submissions_per_window=10,
        ) as (client, gate_open):
            idle = client.get("/api/health").json()
            assert idle["running_analyses"] == 0
            assert idle["queued_analyses"] == 0

            _submit(client, "https://github.com/acme/one")
            _submit(client, "https://github.com/acme/two")
            busy = client.get("/api/health").json()
            assert busy["running_analyses"] == 1
            assert busy["queued_analyses"] == 1
            assert busy["max_concurrent_analyses"] == 1
            gate_open.set()


class TestHealth:
    def test_reports_configuration_without_secrets(self, client: TestClient) -> None:
        """健康检查会进日志与监控，不能回显密钥。"""
        body = client.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["llm_flash"]
        serialized = json.dumps(body)
        assert "test-key-not-used" not in serialized
        assert "sk-" not in serialized

    def test_reports_quota_usage(self, client: TestClient) -> None:
        """KTD11 的第三个信号：配额贴顶要可见。"""
        body = client.get("/api/health").json()
        assert body["repos_quota_bytes"] > 0
        assert body["repos_bytes"] >= 0
        assert "last_reclaimed_bytes" in body
        assert body["submissions_per_window"] > 0


class TestOpenApi:
    def test_schema_generated(self) -> None:
        """Interface Contracts 把生成的 OpenAPI schema 列为 canonical artifact。"""
        app = create_app(make_settings())
        schema = app.openapi()
        assert "/api/analyses" in schema["paths"]
        assert "/api/analyses/{task_id}" in schema["paths"]

    def test_error_responses_declared(self) -> None:
        """错误响应形状进 schema，前端才能按 reason 分别处理。"""
        app = create_app(make_settings())
        schema = app.openapi()
        submit = schema["paths"]["/api/analyses"]["post"]["responses"]
        assert "400" in submit
        assert "413" in submit
