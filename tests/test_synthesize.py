"""报告汇总节点。

重点是校验真的在这一层落地——模型按 prompt 给引用，但只有校验决定哪些结论进报告。
所以测试断言的是「编造引用的结论没能进报告」，而不是「模型给了引用」。

缺失部分的三个来源（未解析文件、未深挖模块、图降级）加校验拒绝数，都要能出现在
产出里（R11、AE4）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.graph.nodes.synthesize import build_missing_parts, synthesize_report
from backend.graph.state import (
    LanguageProfile,
    ModuleAnalysis,
    ModulePlan,
    NodeFailure,
)
from backend.static_analysis.models import (
    DependencyGraph,
    Granularity,
    Module,
    ParsedFile,
    ParseOutcome,
    UnparsedFile,
)
from tests.support import make_settings


class _FakeProvider:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[dict[str, object]] = []

    async def chat(self, messages, tier="flash", tools=None, response_format=None):  # type: ignore[no-untyped-def]
        self.requests.append({"messages": messages, "tier": tier})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome

        class _Message:
            content = outcome

        class _Choice:
            message = _Message()

        class _Response:
            choices = [_Choice()]

        return _Response()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "core.py").write_text(
        "\n".join(f"line_{i} = {i}" for i in range(1, 31)), encoding="utf-8"
    )
    (root / "pkg" / "api.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    return root


def _module(name: str = "pkg") -> Module:
    return Module(
        name=name,
        files=("pkg/api.py", "pkg/core.py"),
        internal_edges=1,
        external_edges=0,
        origin="directory",
    )


def _state(repo: Path, **extra: object) -> dict:
    base: dict = {
        "repo_url": "https://github.com/acme/widget",
        "commit_sha": "abc123",
        "workdir": str(repo),
        "parse_outcome": ParseOutcome(
            parsed=[
                ParsedFile(path="pkg/core.py", language="python"),
                ParsedFile(path="pkg/api.py", language="python"),
            ]
        ),
        "modules": [_module()],
        "entrypoints": [],
        "language_profile": LanguageProfile(
            total_files=2, parseable_files=2, by_language={"Python": 2}
        ),
        "module_analyses": [
            ModuleAnalysis(
                module_name="pkg",
                summary="pkg 的核心逻辑在 pkg/core.py。",
                cited_paths=("pkg/core.py",),
            )
        ],
        "dependency_graph": DependencyGraph(
            nodes=("pkg/api.py", "pkg/core.py"),
            edges={"pkg/api.py": frozenset({"pkg/core.py"}), "pkg/core.py": frozenset()},
        ),
    }
    base.update(extra)
    return base


def _reply(**sections: object) -> str:
    payload: dict[str, object] = {"summary": "总体印象"}
    payload.update(sections)
    return json.dumps(payload, ensure_ascii=False)


def _claim(text: str, *citations: dict[str, object]) -> dict[str, object]:
    return {"text": text, "citations": list(citations)}


class TestValidationEnforced:
    async def test_valid_claim_kept(self, repo: Path) -> None:
        provider = _FakeProvider(
            [_reply(module_breakdown=[_claim("核心在 core", {"path": "pkg/core.py", "line": 5})])]
        )
        result = await synthesize_report(_state(repo), make_settings(), provider)  # type: ignore[arg-type]
        report = result["report"]
        assert len(report.module_breakdown.claims) == 1  # type: ignore[union-attr]

    async def test_claim_without_citation_dropped(self, repo: Path) -> None:
        """没有校验，可追溯性只是 prompt 里的期望。"""
        provider = _FakeProvider(
            [_reply(module_breakdown=[_claim("本项目结构清晰")])]
        )
        result = await synthesize_report(_state(repo), make_settings(), provider)  # type: ignore[arg-type]
        assert result["report"].module_breakdown.claims == ()  # type: ignore[union-attr]
        assert result["unsupported_claims"]

    async def test_fabricated_path_dropped(self, repo: Path) -> None:
        provider = _FakeProvider(
            [_reply(dependencies=[_claim("依赖在这里", {"path": "pkg/ghost.py"})])]
        )
        result = await synthesize_report(_state(repo), make_settings(), provider)  # type: ignore[arg-type]
        assert result["report"].dependencies.claims == ()  # type: ignore[union-attr]
        assert any("不在仓库文件树中" in note for note in result["unsupported_claims"])  # type: ignore[union-attr]

    async def test_out_of_range_line_dropped(self, repo: Path) -> None:
        """路径真实但行号越界，比编造路径更隐蔽。"""
        provider = _FakeProvider(
            [_reply(key_flows=[_claim("流程在此", {"path": "pkg/api.py", "line": 500})])]
        )
        result = await synthesize_report(_state(repo), make_settings(), provider)  # type: ignore[arg-type]
        assert result["report"].key_flows.claims == ()  # type: ignore[union-attr]

    async def test_partially_valid_claim_keeps_valid_citations(self, repo: Path) -> None:
        provider = _FakeProvider(
            [
                _reply(
                    tech_stack=[
                        _claim(
                            "两处证据",
                            {"path": "pkg/core.py", "line": 1},
                            {"path": "pkg/ghost.py"},
                        )
                    ]
                )
            ]
        )
        result = await synthesize_report(_state(repo), make_settings(), provider)  # type: ignore[arg-type]
        claims = result["report"].tech_stack.claims  # type: ignore[union-attr]
        assert len(claims) == 1
        assert len(claims[0].citations) == 1

    async def test_rejected_count_recorded_in_missing(self, repo: Path) -> None:
        """被丢弃的结论也是缺失：它们本该在报告里。"""
        provider = _FakeProvider(
            [_reply(module_breakdown=[_claim("无依据"), _claim("也无依据")])]
        )
        result = await synthesize_report(_state(repo), make_settings(), provider)  # type: ignore[arg-type]
        rendered = result["report"].missing.render()  # type: ignore[union-attr]
        assert "2 条结论因引用无法核验被丢弃" in rendered

    async def test_validation_summary_returned(self, repo: Path) -> None:
        provider = _FakeProvider(
            [
                _reply(
                    module_breakdown=[
                        _claim("好", {"path": "pkg/core.py"}),
                        _claim("坏"),
                    ]
                )
            ]
        )
        result = await synthesize_report(_state(repo), make_settings(), provider)  # type: ignore[arg-type]
        assert "共 2 条结论" in str(result["report_validation_summary"])


class TestMissingParts:
    def test_unparsed_files_grouped_by_reason(self, repo: Path) -> None:
        state = _state(
            repo,
            parse_outcome=ParseOutcome(
                parsed=[ParsedFile(path="pkg/core.py", language="python")],
                unparsed=[
                    UnparsedFile(path="a.md", reason="未支持符号级解析的扩展名：.md"),
                    UnparsedFile(path="b.md", reason="未支持符号级解析的扩展名：.md"),
                    UnparsedFile(path="big.py", reason="文件体积 999 字节超出上限 100"),
                ],
            ),
        )
        missing = build_missing_parts(state, rejected_claims=0)
        assert missing.unparsed_files == 3
        assert missing.unparsed_reasons["未支持符号级解析的扩展名"] == 2

    def test_failed_module_recorded_as_failure(self, repo: Path) -> None:
        """AE4：报告标注该模块缺失及原因。"""
        state = _state(
            repo,
            module_failures=[
                NodeFailure(node="module_agent", scope="auth", error="RuntimeError: 超时")
            ],
        )
        missing = build_missing_parts(state, rejected_claims=0)
        assert len(missing.skipped_modules) == 1
        assert missing.skipped_modules[0].name == "auth"
        assert missing.skipped_modules[0].kind == "failed"
        assert "超时" in missing.skipped_modules[0].reason

    def test_planned_but_unanalysed_module_recorded(self, repo: Path) -> None:
        """挑中却没产出分析的模块不能静默消失。"""
        state = _state(
            repo,
            planned_modules=[
                ModulePlan(module=_module("pkg"), priority=1, reason="r", rule_score=0.5),
                ModulePlan(module=_module("orphan"), priority=2, reason="r", rule_score=0.4),
            ],
        )
        missing = build_missing_parts(state, rejected_claims=0)
        assert any(m.name == "orphan" for m in missing.skipped_modules)

    def test_graph_degradation_noted(self, repo: Path) -> None:
        state = _state(
            repo,
            dependency_graph=DependencyGraph(
                nodes=("pkg",),
                edges={"pkg": frozenset()},
                granularity=Granularity.DIRECTORY,
                degraded_reason="文件数超出上限",
            ),
        )
        missing = build_missing_parts(state, rejected_claims=0)
        assert any("文件数超出上限" in note for note in missing.degraded_notes)

    def test_empty_missing_states_no_gaps(self, repo: Path) -> None:
        """空节也要有内容（R11、AE2 的表达一致性）。"""
        missing = build_missing_parts(_state(repo), rejected_claims=0)
        assert missing.is_empty
        assert "无缺失" in missing.render()


class TestDegradedPaths:
    async def test_no_provider_still_produces_report_with_missing(self, repo: Path) -> None:
        result = await synthesize_report(_state(repo), make_settings(), None)
        report = result["report"]
        assert "未配置 LLM provider" in report.summary  # type: ignore[union-attr]
        assert report.missing is not None  # type: ignore[union-attr]

    async def test_no_analyses_reports_reason(self, repo: Path) -> None:
        provider = _FakeProvider([])
        result = await synthesize_report(
            _state(repo, module_analyses=[]), make_settings(), provider  # type: ignore[arg-type]
        )
        assert "没有任何模块分析结果" in result["report"].summary  # type: ignore[union-attr]

    async def test_call_failure_still_produces_report(self, repo: Path) -> None:
        """汇总失败不该让整次分析无产出——各模块分析仍有价值。"""
        provider = _FakeProvider([RuntimeError("网关 503")])
        result = await synthesize_report(_state(repo), make_settings(), provider)  # type: ignore[arg-type]
        assert "RuntimeError" in result["report"].summary  # type: ignore[union-attr]
        assert "汇总调用失败" in str(result["report_validation_summary"])

    async def test_unparseable_reply_still_produces_report(self, repo: Path) -> None:
        provider = _FakeProvider(["这不是 JSON"])
        result = await synthesize_report(_state(repo), make_settings(), provider)  # type: ignore[arg-type]
        assert "无法解析" in result["report"].summary  # type: ignore[union-attr]

    async def test_code_fenced_json_parsed(self, repo: Path) -> None:
        fenced = "```json\n" + _reply(
            entrypoints=[_claim("入口", {"path": "pkg/api.py", "line": 1})]
        ) + "\n```"
        provider = _FakeProvider([fenced])
        result = await synthesize_report(_state(repo), make_settings(), provider)  # type: ignore[arg-type]
        assert len(result["report"].entrypoints.claims) == 1  # type: ignore[union-attr]


class TestRequestShape:
    async def test_uses_pro_tier(self, repo: Path) -> None:
        """汇总是低频高判断（KTD3）。"""
        provider = _FakeProvider([_reply()])
        await synthesize_report(_state(repo), make_settings(), provider)  # type: ignore[arg-type]
        assert provider.requests[0]["tier"] == "pro"

    async def test_prompt_states_citations_are_validated(self, repo: Path) -> None:
        """告诉模型引用会被校验，它才知道写不出引用就别写。"""
        provider = _FakeProvider([_reply()])
        await synthesize_report(_state(repo), make_settings(), provider)  # type: ignore[arg-type]
        system = provider.requests[0]["messages"][0]["content"]  # type: ignore[index]
        assert "校验" in system
        assert "会被拒绝" in system or "会被程序校验" in system

    async def test_prompt_requires_chinese_output(self, repo: Path) -> None:
        """报告是交付物，输出语言不能靠模型自发跟随输入。"""
        provider = _FakeProvider([_reply()])
        await synthesize_report(_state(repo), make_settings(), provider)  # type: ignore[arg-type]
        system = provider.requests[0]["messages"][0]["content"]  # type: ignore[index]
        assert "用中文" in system
        assert "保留原文" in system

    async def test_failed_modules_listed_in_input(self, repo: Path) -> None:
        """模型要知道哪些模块没有分析，才不会把「没提到」写成「不存在」。"""
        provider = _FakeProvider([_reply()])
        await synthesize_report(
            _state(
                repo,
                module_failures=[
                    NodeFailure(node="module_agent", scope="auth", error="超时")
                ],
            ),
            make_settings(),
            provider,  # type: ignore[arg-type]
        )
        user = provider.requests[0]["messages"][1]["content"]  # type: ignore[index]
        assert "auth" in user
        assert "不要凭空描述" in user

    async def test_skeleton_summarised_not_dumped(self, repo: Path) -> None:
        """只给统计与名单，塞全量数据会挤掉模块分析本身的空间。"""
        provider = _FakeProvider([_reply()])
        await synthesize_report(_state(repo), make_settings(), provider)  # type: ignore[arg-type]
        payload = json.loads(provider.requests[0]["messages"][1]["content"])  # type: ignore[index]
        assert "module_count" in payload["skeleton"]
        assert "symbols" not in payload["skeleton"]
