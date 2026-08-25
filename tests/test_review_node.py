"""Reviewer 节点：三类检查的编排与产出契约。

重点覆盖三条需求级约束：
  R18  节点收不到报告内容（KTD9：否则发现会被报告叙述锚定）。
  R17  各类都说明执行范围与命中数，零命中与未执行可区分。
  DoD  密钥形态文件即使不可解析也要被扫。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.graph.nodes.reviewer import run_review
from backend.review.models import CheckStatus, FindingCategory
from backend.static_analysis.models import DependencyGraph, Module
from tests.support import make_settings


class _FakeProvider:
    """所有候选一律裁定为 report，便于验证编排而非模型质量。"""

    def __init__(self, verdict: str = "report", fail: bool = False) -> None:
        self.verdict = verdict
        self.fail = fail
        self.calls = 0

    async def chat(self, messages, tier="flash", tools=None, response_format=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.fail:
            raise RuntimeError("网关不可用")

        payload = json.loads(messages[1]["content"])
        judgments = [
            {
                "id": c["id"],
                "verdict": self.verdict,
                "severity": "medium",
                "reason": f"裁定 {c['kind']}",
            }
            for c in payload["candidates"]
        ]

        class _Message:
            content = json.dumps({"judgments": judgments})

        class _Choice:
            message = _Message()

        class _Response:
            choices = [_Choice()]

        return _Response()


def _graph(edges: dict[str, list[str]]) -> DependencyGraph:
    nodes = sorted({*edges, *(t for ts in edges.values() for t in ts)})
    return DependencyGraph(
        nodes=tuple(nodes), edges={n: frozenset(edges.get(n, ())) for n in nodes}
    )


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "bad.py").write_text(
        "def go():\n    try:\n        risky()\n    except:\n        pass\n", encoding="utf-8"
    )
    (root / "pkg" / "clean.py").write_text(
        "def go(uid):\n    cur.execute('SELECT 1 FROM t WHERE id = %s', (uid,))\n",
        encoding="utf-8",
    )
    return root


def _state(workdir: Path, targets: tuple[str, ...], **extra: object) -> dict:
    base: dict = {
        "workdir": str(workdir),
        "review_targets": list(targets),
        "dependency_graph": _graph({"pkg/bad.py": ["pkg/clean.py"], "pkg/clean.py": []}),
        "modules": [
            Module(
                name="pkg",
                files=("pkg/bad.py", "pkg/clean.py"),
                internal_edges=1,
                external_edges=0,
                origin="directory",
            )
        ],
        "entrypoints": [],
    }
    base.update(extra)
    return base


class TestReportIsolation:
    async def test_node_refuses_state_carrying_report(self, workdir: Path) -> None:
        """KTD9：若让 Reviewer 看到报告，其发现会被报告叙述锚定。

        断言放在节点里，是为了让接线出错时立刻暴露，而不是等评审结果变得可疑才怀疑。
        """
        state = _state(workdir, ("pkg/bad.py",), report="架构报告正文")
        with pytest.raises(AssertionError, match="KTD9"):
            await run_review(state, make_settings(), _FakeProvider())  # type: ignore[arg-type]

    async def test_runs_without_report_field(self, workdir: Path) -> None:
        report = await run_review(
            _state(workdir, ("pkg/bad.py",)), make_settings(), _FakeProvider()  # type: ignore[arg-type]
        )
        assert report.target_files == ("pkg/bad.py",)


class TestCategoryCoverage:
    async def test_all_three_categories_present(self, workdir: Path) -> None:
        """R17：逐类说明执行情况，缺一类等于那类的结果无从判断。"""
        report = await run_review(
            _state(workdir, ("pkg/bad.py", "pkg/clean.py")),
            make_settings(),
            _FakeProvider(),  # type: ignore[arg-type]
        )
        assert {o.category for o in report.outcomes} == {
            FindingCategory.STRUCTURAL,
            FindingCategory.ERROR_HANDLING,
            FindingCategory.SECURITY,
        }

    async def test_error_handling_findings_produced(self, workdir: Path) -> None:
        report = await run_review(
            _state(workdir, ("pkg/bad.py",)), make_settings(), _FakeProvider()  # type: ignore[arg-type]
        )
        outcome = next(
            o for o in report.outcomes if o.category is FindingCategory.ERROR_HANDLING
        )
        assert outcome.status is CheckStatus.EXECUTED
        assert any(f.kind == "bare_except" for f in outcome.findings)

    async def test_security_scope_lists_pattern_set(self, workdir: Path) -> None:
        """AE2：零命中时要说明已检查的模式集合，所以 scope 必须含它。"""
        report = await run_review(
            _state(workdir, ("pkg/clean.py",)), make_settings(), _FakeProvider()  # type: ignore[arg-type]
        )
        outcome = next(o for o in report.outcomes if o.category is FindingCategory.SECURITY)
        assert "硬编码密钥" in outcome.scope
        assert "SQL" in outcome.scope

    async def test_suppressed_count_disclosed(self, workdir: Path) -> None:
        """模型压掉的候选数要可见——否则读者无法判断产出是「没问题」还是「被压了」。"""
        report = await run_review(
            _state(workdir, ("pkg/bad.py",)),
            make_settings(),
            _FakeProvider(verdict="suppress"),  # type: ignore[arg-type]
        )
        outcome = next(
            o for o in report.outcomes if o.category is FindingCategory.ERROR_HANDLING
        )
        assert outcome.findings == []
        assert "不必报告" in outcome.scope


class TestDegradedPaths:
    async def test_no_provider_skips_llm_categories_with_reason(self, workdir: Path) -> None:
        """没有 LLM 时不能把候选当成零命中——两者在产出中必须不同（R17）。"""
        report = await run_review(_state(workdir, ("pkg/bad.py",)), make_settings(), None)
        for category in (FindingCategory.ERROR_HANDLING, FindingCategory.SECURITY):
            outcome = next(o for o in report.outcomes if o.category is category)
            assert outcome.status is CheckStatus.SKIPPED
            assert "未配置 LLM" in outcome.reason
            assert "候选点" in outcome.reason

    async def test_structural_still_runs_without_provider(self, workdir: Path) -> None:
        """结构类是纯图计算，不该因 LLM 不可用而跳过。"""
        report = await run_review(_state(workdir, ("pkg/bad.py",)), make_settings(), None)
        outcome = next(o for o in report.outcomes if o.category is FindingCategory.STRUCTURAL)
        assert outcome.status is CheckStatus.EXECUTED

    async def test_all_candidates_unjudged_marks_category_failed(self, workdir: Path) -> None:
        """判断全失败时零发现不代表没问题，必须记为 FAILED 并说明。"""
        report = await run_review(
            _state(workdir, ("pkg/bad.py",)),
            make_settings(),
            _FakeProvider(fail=True),  # type: ignore[arg-type]
        )
        outcome = next(
            o for o in report.outcomes if o.category is FindingCategory.ERROR_HANDLING
        )
        assert outcome.status is CheckStatus.FAILED
        assert "零发现不代表无问题" in outcome.reason

    async def test_missing_graph_skips_structural_with_reason(self, workdir: Path) -> None:
        state = _state(workdir, ("pkg/bad.py",))
        del state["dependency_graph"]
        report = await run_review(state, make_settings(), None)
        outcome = next(o for o in report.outcomes if o.category is FindingCategory.STRUCTURAL)
        assert outcome.status is CheckStatus.SKIPPED
        assert "无依赖图" in outcome.reason


class TestSecretShapedFiles:
    async def test_unparseable_secret_file_still_scanned(self, workdir: Path) -> None:
        """DoD：密钥形态文件不产生切块，但 Reviewer 仍扫描它们。

        `.env` 不是 Python/TypeScript，tree-sitter 解析不了，而它最可能含密钥。
        """
        (workdir / ".env").write_text(
            "DB_PASSWORD=xQ7fL2mZ9pR4tK8wB3nH\n", encoding="utf-8"
        )
        report = await run_review(
            _state(workdir, (".env",)), make_settings(), _FakeProvider()  # type: ignore[arg-type]
        )
        outcome = next(o for o in report.outcomes if o.category is FindingCategory.SECURITY)
        assert any(f.kind == "hardcoded_secret" for f in outcome.findings)

    async def test_ordinary_unparseable_file_skipped_quietly(self, workdir: Path) -> None:
        """非密钥形态的不可解析文件（README 之类）不必扫，也不该报错。"""
        (workdir / "README.md").write_text("# hi\n", encoding="utf-8")
        report = await run_review(
            _state(workdir, ("README.md",)), make_settings(), _FakeProvider()  # type: ignore[arg-type]
        )
        outcome = next(o for o in report.outcomes if o.category is FindingCategory.SECURITY)
        assert outcome.findings == []
        assert outcome.status is CheckStatus.EXECUTED


class TestReportSummary:
    async def test_summary_distinguishes_zero_hit_from_skipped(self, workdir: Path) -> None:
        """同一份产出里两种状态必须读起来不同（AE2 明文要求）。"""
        with_llm = await run_review(
            _state(workdir, ("pkg/clean.py",)), make_settings(), _FakeProvider()  # type: ignore[arg-type]
        )
        without_llm = await run_review(_state(workdir, ("pkg/clean.py",)), make_settings(), None)

        zero_hit = next(
            o for o in with_llm.outcomes if o.category is FindingCategory.ERROR_HANDLING
        )
        skipped = next(
            o for o in without_llm.outcomes if o.category is FindingCategory.ERROR_HANDLING
        )
        assert zero_hit.hit_count == skipped.hit_count == 0
        assert zero_hit.describe() != skipped.describe()
        assert "已检查" in zero_hit.describe()
        assert "未执行" in skipped.describe()

    async def test_all_findings_aggregated(self, workdir: Path) -> None:
        report = await run_review(
            _state(workdir, ("pkg/bad.py",)), make_settings(), _FakeProvider()  # type: ignore[arg-type]
        )
        assert len(report.all_findings) == sum(o.hit_count for o in report.outcomes)

    async def test_summary_has_one_line_per_category(self, workdir: Path) -> None:
        report = await run_review(
            _state(workdir, ("pkg/bad.py",)), make_settings(), _FakeProvider()  # type: ignore[arg-type]
        )
        assert len(report.summary_lines()) == 3
