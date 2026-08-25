"""报告校验。R7 从「要求」变成「保证」的地方。

没有这一层，可追溯性只是 prompt 里的期望——模型被要求给引用，但没人检查它给的引用
是否存在、是否指向真实行号。所以这些测试断言的是「无效引用被识别出来」，而不是
「模型给了引用」。

用真实文件校验行号：行号越界只能靠读文件判断，构造假的行数会把要验证的东西替换成
我的假设。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.report.schema import (
    ArchitectureReport,
    Citation,
    Claim,
    MissingParts,
    ReportSection,
    SkippedModule,
)
from backend.report.validate import (
    ClaimVerdict,
    apply_validation,
    validate_report,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "core.py").write_text(
        "\n".join(f"line_{i} = {i}" for i in range(1, 21)), encoding="utf-8"
    )
    (root / "pkg" / "api.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    return root


KNOWN = frozenset({"pkg/core.py", "pkg/api.py"})


def _report(*claims: Claim) -> ArchitectureReport:
    return ArchitectureReport(
        repo="acme/widget",
        module_breakdown=ReportSection(
            key="module_breakdown", title="模块划分", claims=tuple(claims)
        ),
    )


class TestNoCitation:
    def test_claim_without_citation_rejected(self, repo: Path) -> None:
        """「代码结构清晰」这类表述给不出引用，因此过不了校验。"""
        report = _report(Claim(text="本项目采用分层架构，代码结构清晰"))
        validation = validate_report(report, repo, KNOWN)
        assert validation.results[0].verdict is ClaimVerdict.NO_CITATION
        assert validation.rejected

    def test_rejection_reason_cites_the_requirement(self, repo: Path) -> None:
        report = _report(Claim(text="结构清晰"))
        problem = validate_report(report, repo, KNOWN).results[0].problems[0]
        assert "没有任何引用" in problem.reason
        assert "R7" in problem.reason

    def test_no_citation_is_not_salvageable(self, repo: Path) -> None:
        """无引用的结论从一开始就没有依据，不可降级保留。"""
        report = _report(Claim(text="结构清晰"))
        assert not validate_report(report, repo, KNOWN).results[0].is_salvageable


class TestPathValidation:
    def test_nonexistent_path_identified(self, repo: Path) -> None:
        report = _report(
            Claim(text="逻辑在这里", citations=(Citation(path="pkg/imaginary.py"),))
        )
        result = validate_report(report, repo, KNOWN).results[0]
        assert result.verdict is ClaimVerdict.INVALID_CITATION
        assert "不在仓库文件树中" in result.problems[0].reason

    def test_real_path_passes(self, repo: Path) -> None:
        report = _report(Claim(text="核心在这里", citations=(Citation(path="pkg/core.py"),)))
        result = validate_report(report, repo, KNOWN).results[0]
        assert result.is_valid
        assert result.problems == ()

    def test_empty_path_identified(self, repo: Path) -> None:
        report = _report(Claim(text="某处", citations=(Citation(path=""),)))
        result = validate_report(report, repo, KNOWN).results[0]
        assert "未给出文件路径" in result.problems[0].reason

    def test_escaping_path_identified(self, repo: Path) -> None:
        """引用路径逃逸出仓库既是无效引用，也是安全信号。"""
        report = _report(
            Claim(text="外部", citations=(Citation(path="../outside.py", line=1),))
        )
        result = validate_report(report, repo, KNOWN).results[0]
        assert result.verdict is ClaimVerdict.INVALID_CITATION


class TestLineValidation:
    def test_line_within_file_passes(self, repo: Path) -> None:
        report = _report(
            Claim(text="第 5 行", citations=(Citation(path="pkg/core.py", line=5),))
        )
        assert validate_report(report, repo, KNOWN).results[0].is_valid

    def test_line_beyond_file_identified(self, repo: Path) -> None:
        """路径真实但行号越界，比编造路径更隐蔽——只有读文件才能发现。"""
        report = _report(
            Claim(text="第 999 行", citations=(Citation(path="pkg/core.py", line=999),))
        )
        result = validate_report(report, repo, KNOWN).results[0]
        assert result.verdict is ClaimVerdict.INVALID_CITATION
        assert "超出文件实际行数 20" in result.problems[0].reason

    def test_range_end_beyond_file_identified(self, repo: Path) -> None:
        report = _report(
            Claim(
                text="范围",
                citations=(Citation(path="pkg/api.py", line=1, end_line=50),),
            )
        )
        result = validate_report(report, repo, KNOWN).results[0]
        assert "超出文件实际行数 2" in result.problems[0].reason

    def test_valid_range_passes(self, repo: Path) -> None:
        report = _report(
            Claim(
                text="范围",
                citations=(Citation(path="pkg/core.py", line=3, end_line=8),),
            )
        )
        assert validate_report(report, repo, KNOWN).results[0].is_valid

    def test_inverted_range_identified(self, repo: Path) -> None:
        report = _report(
            Claim(
                text="倒置",
                citations=(Citation(path="pkg/core.py", line=10, end_line=3),),
            )
        )
        result = validate_report(report, repo, KNOWN).results[0]
        assert "行范围倒置" in result.problems[0].reason

    def test_non_positive_line_identified(self, repo: Path) -> None:
        report = _report(Claim(text="零行", citations=(Citation(path="pkg/core.py", line=0),)))
        result = validate_report(report, repo, KNOWN).results[0]
        assert "必须为正数" in result.problems[0].reason

    def test_pathonly_citation_skips_line_check(self, repo: Path) -> None:
        """模块级结论未必落在某一行，不给行号是合法的。"""
        report = _report(Claim(text="整个文件", citations=(Citation(path="pkg/core.py"),)))
        assert validate_report(report, repo, KNOWN).results[0].is_valid


class TestPartialValidity:
    def test_mixed_citations_are_salvageable(self, repo: Path) -> None:
        """部分引用有效时可降级保留——剩下的有效引用仍支撑这条结论。"""
        report = _report(
            Claim(
                text="逻辑分布在两处",
                citations=(
                    Citation(path="pkg/core.py", line=5),
                    Citation(path="pkg/ghost.py", line=1),
                ),
            )
        )
        result = validate_report(report, repo, KNOWN).results[0]
        assert result.is_salvageable
        assert len(result.valid_citations) == 1
        assert result.valid_citations[0].path == "pkg/core.py"

    def test_apply_validation_keeps_only_valid_citations(self, repo: Path) -> None:
        report = _report(
            Claim(
                text="两处",
                citations=(
                    Citation(path="pkg/core.py", line=5),
                    Citation(path="pkg/ghost.py"),
                ),
            )
        )
        validation = validate_report(report, repo, KNOWN)
        applied = apply_validation(report, validation)
        kept = applied.module_breakdown.claims
        assert len(kept) == 1
        assert len(kept[0].citations) == 1

    def test_apply_validation_drops_rejected_claims(self, repo: Path) -> None:
        report = _report(
            Claim(text="有依据", citations=(Citation(path="pkg/core.py"),)),
            Claim(text="无依据"),
            Claim(text="全无效", citations=(Citation(path="pkg/ghost.py"),)),
        )
        validation = validate_report(report, repo, KNOWN)
        applied = apply_validation(report, validation)
        assert [c.text for c in applied.module_breakdown.claims] == ["有依据"]

    def test_apply_validation_does_not_mutate_original(self, repo: Path) -> None:
        """校验前后的差异本身是有用信息（多少结论被拒绝了）。"""
        report = _report(Claim(text="无依据"))
        validate_report(report, repo, KNOWN)
        applied = apply_validation(report, validate_report(report, repo, KNOWN))
        assert len(report.module_breakdown.claims) == 1
        assert len(applied.module_breakdown.claims) == 0


class TestMissingParts:
    def test_empty_missing_section_still_states_it(self) -> None:
        """空节与「没有这一节」在读者看来不同：后者让人怀疑是不是漏了（R11、AE2）。"""
        missing = MissingParts()
        assert missing.is_empty
        rendered = missing.render()
        assert rendered.strip()
        assert "无缺失" in rendered

    def test_skipped_modules_listed_with_reason(self) -> None:
        missing = MissingParts(
            skipped_modules=(
                SkippedModule(name="types", reason="全是类型别名", kind="not_planned"),
                SkippedModule(name="auth", reason="子 Agent 超时", kind="failed"),
            )
        )
        rendered = missing.render()
        assert "types" in rendered
        assert "全是类型别名" in rendered
        assert "auth" in rendered

    def test_planner_skip_and_failure_read_differently(self) -> None:
        """有意的范围决定与分析缺失对读者的含义完全不同。"""
        planned_out = MissingParts(
            skipped_modules=(SkippedModule(name="a", reason="r", kind="not_planned"),)
        ).render()
        failed = MissingParts(
            skipped_modules=(SkippedModule(name="a", reason="r", kind="failed"),)
        ).render()
        assert planned_out != failed
        assert "未纳入深挖范围" in planned_out
        assert "分析失败" in failed

    def test_unparsed_reasons_grouped_not_enumerated(self) -> None:
        """逐个列出上千个未解析文件没有意义，按原因归类才有。"""
        missing = MissingParts(
            unparsed_files=2001,
            unparsed_reasons={"未支持符号级解析的扩展名": 2000, "文件体积超出上限": 1},
        )
        rendered = missing.render()
        assert "2001" in rendered
        assert "未支持符号级解析的扩展名（2000 个）" in rendered

    def test_unsupported_languages_disclosed(self) -> None:
        missing = MissingParts(unsupported_languages=("JavaScript", "Go"))
        assert "JavaScript" in missing.render()

    def test_degraded_notes_disclosed(self) -> None:
        missing = MissingParts(degraded_notes=("依赖图降级为目录级粒度",))
        assert "降级说明" in missing.render()


class TestValidationSummary:
    def test_summary_counts_each_category(self, repo: Path) -> None:
        report = _report(
            Claim(text="好", citations=(Citation(path="pkg/core.py"),)),
            Claim(text="无引用"),
            Claim(
                text="部分",
                citations=(Citation(path="pkg/core.py"), Citation(path="pkg/ghost.py")),
            ),
        )
        summary = validate_report(report, repo, KNOWN).summary()
        assert "共 3 条结论" in summary
        assert "1 条引用完全有效" in summary
        assert "1 条部分引用无效" in summary
        assert "1 条被拒绝" in summary

    def test_summary_on_empty_report(self, repo: Path) -> None:
        summary = validate_report(ArchitectureReport(repo="x"), repo, KNOWN).summary()
        assert "没有任何结论项" in summary

    def test_all_sections_validated(self, repo: Path) -> None:
        """校验必须覆盖每一节，漏一节等于那节的引用无人检查。"""
        good = Claim(text="有依据", citations=(Citation(path="pkg/core.py"),))
        report = ArchitectureReport(
            repo="x",
            module_breakdown=ReportSection(key="module_breakdown", title="模块划分", claims=(good,)),
            dependencies=ReportSection(key="dependencies", title="依赖关系", claims=(good,)),
            entrypoints=ReportSection(key="entrypoints", title="入口点", claims=(good,)),
            key_flows=ReportSection(key="key_flows", title="关键流程", claims=(good,)),
            tech_stack=ReportSection(key="tech_stack", title="技术栈", claims=(good,)),
        )
        validation = validate_report(report, repo, KNOWN)
        assert len(validation.results) == 5
        assert {r.section_key for r in validation.results} == {
            "module_breakdown",
            "dependencies",
            "entrypoints",
            "key_flows",
            "tech_stack",
        }


class TestCitationRendering:
    def test_path_only(self) -> None:
        assert Citation(path="a.py").render() == "a.py"

    def test_single_line(self) -> None:
        assert Citation(path="a.py", line=5).render() == "a.py:5"

    def test_range(self) -> None:
        assert Citation(path="a.py", line=5, end_line=9).render() == "a.py:5-9"

    def test_collapsed_range(self) -> None:
        assert Citation(path="a.py", line=5, end_line=5).render() == "a.py:5"
