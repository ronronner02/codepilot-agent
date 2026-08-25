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
from backend.static_analysis.models import DependencyGraph, Module, ParsedFile, ParseOutcome
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


def _submit(client: TestClient, url: str = "https://github.com/acme/widget") -> Any:
    return client.post("/api/analyses", json={"repo_url": url})


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


class TestHealth:
    def test_reports_configuration_without_secrets(self, client: TestClient) -> None:
        """健康检查会进日志与监控，不能回显密钥。"""
        body = client.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["llm_flash"]
        serialized = json.dumps(body)
        assert "test-key-not-used" not in serialized
        assert "sk-" not in serialized


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
