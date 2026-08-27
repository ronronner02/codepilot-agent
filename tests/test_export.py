"""报告导出 Markdown 与 HTML（U19，R-38、R-39、BR-003、AE-14）。

渲染函数是纯函数，所以大部分断言直接调它，不经 HTTP——那让失败信号指向渲染逻辑而非路由。
端点行为（状态码、Content-Disposition、错误 reason）另有一组用例。
"""

from __future__ import annotations

from backend.api.schemas import (
    CategoryOutcomeModel,
    CitationModel,
    ClaimModel,
    FindingModel,
    IndexStatusModel,
    LanguageProfileModel,
    MissingPartsModel,
    ReportModel,
    ReportSectionModel,
    ResultResponse,
    ReviewModel,
)
from backend.report.render import export_filename, render_html, render_markdown


def _result(**overrides: object) -> ResultResponse:
    base: dict[str, object] = {
        "task_id": "t1",
        "repo": "acme/widget",
        "stage": "done",
        "completed": True,
        "failed": False,
        "commit_sha": "abcdef1234567890",
        "report": ReportModel(
            repo="acme/widget",
            commit_sha="abcdef1234567890",
            summary="总体印象文本",
            sections=[
                ReportSectionModel(
                    key="module_breakdown",
                    title="模块划分",
                    claims=[
                        ClaimModel(
                            text="认证逻辑集中在 auth/jwt.py",
                            citations=[
                                CitationModel(path="auth/jwt.py", line=45, end_line=78)
                            ],
                        )
                    ],
                )
            ],
            missing=MissingPartsModel(text="本次分析无缺失部分"),
            validation_summary="共 26 条结论：25 条引用完全有效。",
            unsupported_claims=[],
        ),
        "review": ReviewModel(
            target_files=["auth/jwt.py"],
            outcomes=[
                CategoryOutcomeModel(
                    category="structural",
                    status="executed",
                    scope="12 个文件；检查项：循环依赖",
                    hit_count=1,
                ),
                CategoryOutcomeModel(
                    category="security",
                    status="skipped",
                    scope="12 个文件",
                    hit_count=0,
                    reason="未配置 LLM provider",
                ),
                CategoryOutcomeModel(
                    category="error_handling",
                    status="executed",
                    scope="12 个文件；检查项：裸 except",
                    hit_count=0,
                ),
            ],
            findings=[
                FindingModel(
                    category="structural",
                    kind="circular_dependency",
                    path="auth/jwt.py",
                    line=24,
                    severity="high",
                    message="3 个文件构成循环依赖",
                    evidence="依赖图上的有向环",
                )
            ],
        ),
        "index": IndexStatusModel(cache_hit=False, chunk_count=99),
        "language_profile": LanguageProfileModel(total_files=33, parseable_files=30),
    }
    base.update(overrides)
    return ResultResponse(**base)  # type: ignore[arg-type]


class TestMarkdown:
    def test_contains_report_sections_and_citations(self) -> None:
        text = render_markdown(_result())
        assert "# 代码分析报告：acme/widget" in text
        assert "模块划分" in text
        assert "认证逻辑集中在 auth/jwt.py" in text
        # 引用带路径与行号（R-39：导出要保留可核验性）。
        assert "auth/jwt.py:45-78" in text

    def test_validation_summary_matches_response(self) -> None:
        """BR-003：界面显示 25/26 时导出也是 25/26。取后端字段，不重算。"""
        text = render_markdown(_result())
        assert "共 26 条结论：25 条引用完全有效。" in text

    def test_review_outcomes_distinguish_three_states(self) -> None:
        """NA-09 延伸到导出：未执行、零命中、有命中三者读起来必须不同。"""
        text = render_markdown(_result())
        assert "未执行 —— 未配置 LLM provider" in text
        assert "已执行，命中 0 条" in text
        assert "已执行，命中 1 条" in text

    def test_findings_carry_location_and_evidence(self) -> None:
        text = render_markdown(_result())
        assert "auth/jwt.py:24" in text
        assert "3 个文件构成循环依赖" in text
        assert "依赖图上的有向环" in text

    def test_missing_parts_always_present(self) -> None:
        text = render_markdown(_result())
        assert "分析的缺失部分" in text
        assert "本次分析无缺失部分" in text

    def test_no_scores_anywhere(self) -> None:
        """NA-01 延伸到导出。"""
        text = render_markdown(_result())
        for word in ("架构评分", "代码质量分", "安全分", "星级", "技术债", "/100"):
            assert word not in text

    def test_report_none_still_produces_review(self) -> None:
        text = render_markdown(_result(report=None))
        assert "未产出架构报告" in text
        assert "代码评审" in text
        assert "3 个文件构成循环依赖" in text

    def test_review_none_still_produces_report(self) -> None:
        text = render_markdown(_result(review=None))
        assert "架构报告" in text
        assert "认证逻辑集中在 auth/jwt.py" in text
        assert "未产出评审数据" in text

    def test_zero_findings_explains_the_distinction(self) -> None:
        review = _result().review
        assert review is not None
        text = render_markdown(
            _result(review=ReviewModel(outcomes=review.outcomes, findings=[]))
        )
        assert "未产出发现" in text
        assert "已执行且命中 0 条与未执行" in text

    def test_line_zero_marked_as_whole_file(self) -> None:
        """R-22：0 不是行号。"""
        text = render_markdown(
            _result(
                review=ReviewModel(
                    findings=[
                        FindingModel(
                            category="structural",
                            kind="god_module",
                            path="core/app.py",
                            line=0,
                            severity="medium",
                            message="职责过多",
                            evidence="出度 18",
                        )
                    ]
                )
            )
        )
        assert "该问题属于文件整体" in text
        assert "core/app.py:0" not in text

    def test_unknown_severity_kept_with_original_value(self) -> None:
        text = render_markdown(
            _result(
                review=ReviewModel(
                    findings=[
                        FindingModel(
                            category="structural",
                            kind="x",
                            path="a.py",
                            line=1,
                            severity="critical",
                            message="m",
                            evidence="e",
                        )
                    ]
                )
            )
        )
        assert "原值 critical" in text

    def test_unsupported_claims_listed(self) -> None:
        report = _result().report
        assert report is not None
        text = render_markdown(
            _result(
                report=report.model_copy(
                    update={"unsupported_claims": ["[deps] 某结论 —— 引用路径不存在"]}
                )
            )
        )
        assert "被丢弃的结论（1 条）" in text
        assert "引用路径不存在" in text


class TestHtml:
    def test_is_self_contained_single_file(self) -> None:
        """自包含：不引外部样式或脚本，导出物要能离线打开。"""
        html = render_html(_result())
        assert "<style>" in html
        assert "<link" not in html
        assert "<script" not in html
        assert "http://" not in html.replace("http://www.w3.org", "")

    def test_escapes_content(self) -> None:
        """仓库内容可能含 < 与 &，不转义会让导出物的结构被内容破坏。"""
        html = render_html(
            _result(
                review=ReviewModel(
                    findings=[
                        FindingModel(
                            category="structural",
                            kind="x",
                            path="a.py",
                            line=1,
                            severity="high",
                            message="<script>alert(1)</script> & more",
                            evidence="e",
                        )
                    ]
                )
            )
        )
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html
        assert "&amp; more" in html

    def test_same_validation_summary_as_markdown(self) -> None:
        """BR-003：三格式同源，通过率必须一致。"""
        result = _result()
        assert "25 条引用完全有效" in render_html(result)
        assert "25 条引用完全有效" in render_markdown(result)

    def test_three_states_distinguishable(self) -> None:
        html = render_html(_result())
        assert "未执行 —— 未配置 LLM provider" in html
        assert "已执行，命中 0 条" in html

    def test_no_scores(self) -> None:
        html = render_html(_result())
        for word in ("架构评分", "代码质量分", "安全分", "星级", "技术债"):
            assert word not in html


class TestFilename:
    def test_carries_repo_and_short_sha(self) -> None:
        assert export_filename(_result(), "md") == "acme__widget-abcdef123456.md"

    def test_falls_back_without_sha(self) -> None:
        assert export_filename(_result(commit_sha=""), "html") == "acme__widget.html"
