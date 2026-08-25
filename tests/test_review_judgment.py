"""候选点的取舍判断。

用假 provider 覆盖裁定与失败路径——这里要验证的是「模型返回被怎么解释」，而不是模型
本身的判断质量。后者由基准仓库实跑抽查承担（真实调用有成本且结果不确定，不适合做
断言）。

贯穿本文件的两条约束：
  LLM 不能新增或移动问题位置——path/line 必须原样来自候选点。
  判断失败不等于零命中——必须与「问过且认为不必报」可区分（R17）。
"""

from __future__ import annotations

import json

import pytest

from backend.review.judgment import judge_candidates
from backend.review.models import (
    Candidate,
    Confidence,
    FindingCategory,
    Severity,
)


class _FakeProvider:
    """按预设序列返回内容或抛异常。"""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[dict[str, object]] = []

    async def chat(self, messages, tier="flash", tools=None, response_format=None):  # type: ignore[no-untyped-def]
        self.requests.append(
            {"messages": messages, "tier": tier, "response_format": response_format}
        )
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


def _candidate(
    kind: str = "bare_except",
    line: int = 12,
    path: str = "pkg/mod.py",
    confidence: Confidence = Confidence.CERTAIN,
    category: FindingCategory = FindingCategory.ERROR_HANDLING,
) -> Candidate:
    return Candidate(
        category=category,
        kind=kind,
        path=path,
        line=line,
        snippet="try:\n    risky()\nexcept:\n    pass",
        detector_evidence="except_clause 无 value 字段",
        confidence=confidence,
        enclosing="handler",
    )


def _reply(*judgments: dict[str, object]) -> str:
    return json.dumps({"judgments": list(judgments)}, ensure_ascii=False)


class TestVerdicts:
    async def test_report_becomes_finding(self) -> None:
        provider = _FakeProvider(
            [_reply({"id": 0, "verdict": "report", "severity": "high", "reason": "确实会吞掉中断"})]
        )
        outcome = await judge_candidates([_candidate()], provider)  # type: ignore[arg-type]
        assert len(outcome.findings) == 1
        assert outcome.findings[0].severity is Severity.HIGH
        assert outcome.findings[0].message == "确实会吞掉中断"

    async def test_suppress_is_counted_not_reported(self) -> None:
        provider = _FakeProvider(
            [_reply({"id": 0, "verdict": "suppress", "reason": "刻意的清理兜底"})]
        )
        outcome = await judge_candidates([_candidate()], provider)  # type: ignore[arg-type]
        assert outcome.findings == []
        assert outcome.suppressed == 1
        assert outcome.unjudged == []

    async def test_unknown_verdict_treated_as_unjudged(self) -> None:
        provider = _FakeProvider([_reply({"id": 0, "verdict": "maybe", "reason": "?"})])
        outcome = await judge_candidates([_candidate()], provider)  # type: ignore[arg-type]
        assert outcome.suppressed == 0
        assert len(outcome.unjudged) == 1

    async def test_missing_reason_still_produces_finding(self) -> None:
        provider = _FakeProvider([_reply({"id": 0, "verdict": "report", "severity": "low"})])
        outcome = await judge_candidates([_candidate()], provider)  # type: ignore[arg-type]
        assert outcome.findings[0].message

    async def test_invalid_severity_falls_back_by_category(self) -> None:
        provider = _FakeProvider(
            [_reply({"id": 0, "verdict": "report", "severity": "catastrophic", "reason": "x"})]
        )
        outcome = await judge_candidates(  # type: ignore[arg-type]
            [_candidate(category=FindingCategory.SECURITY)], provider
        )
        assert outcome.findings[0].severity is Severity.HIGH


class TestPositionIntegrity:
    async def test_llm_cannot_move_the_position(self) -> None:
        """path/line 来自 AST，不经 LLM——这是「只做取舍不做发现」的机制保证。

        即使模型在返回里夹带别的路径行号，产出的 Finding 也必须落在原候选位置。
        """
        provider = _FakeProvider(
            [
                json.dumps(
                    {
                        "judgments": [
                            {
                                "id": 0,
                                "verdict": "report",
                                "severity": "high",
                                "reason": "问题在别处",
                                "path": "totally/other.py",
                                "line": 999,
                            }
                        ]
                    }
                )
            ]
        )
        outcome = await judge_candidates(  # type: ignore[arg-type]
            [_candidate(path="pkg/real.py", line=42)], provider
        )
        assert outcome.findings[0].path == "pkg/real.py"
        assert outcome.findings[0].line == 42

    async def test_extra_ids_in_reply_are_ignored(self) -> None:
        """模型编造出候选列表里不存在的 id 时不应产出发现。"""
        provider = _FakeProvider(
            [
                _reply(
                    {"id": 0, "verdict": "report", "severity": "low", "reason": "真"},
                    {"id": 7, "verdict": "report", "severity": "high", "reason": "编造"},
                )
            ]
        )
        outcome = await judge_candidates([_candidate()], provider)  # type: ignore[arg-type]
        assert len(outcome.findings) == 1

    async def test_detector_evidence_preserved_alongside_model_reason(self) -> None:
        """读者要能区分「机器凭什么定位到这里」与「模型凭什么认为值得管」。"""
        provider = _FakeProvider(
            [_reply({"id": 0, "verdict": "report", "severity": "medium", "reason": "模型理由"})]
        )
        outcome = await judge_candidates([_candidate()], provider)  # type: ignore[arg-type]
        finding = outcome.findings[0]
        assert finding.message == "模型理由"
        assert "except_clause 无 value 字段" in finding.evidence
        assert "复核" in finding.evidence


class TestFailureHandling:
    async def test_call_failure_marks_unjudged_not_suppressed(self) -> None:
        """判断失败不等于零命中——否则模型故障会伪装成「代码没问题」。"""
        provider = _FakeProvider([RuntimeError("网关 503")])
        outcome = await judge_candidates([_candidate()], provider)  # type: ignore[arg-type]
        assert outcome.findings == []
        assert outcome.suppressed == 0
        assert len(outcome.unjudged) == 1
        assert "RuntimeError" in outcome.failure_reason

    async def test_unparseable_reply_marks_unjudged(self) -> None:
        provider = _FakeProvider(["这不是 JSON"])
        outcome = await judge_candidates([_candidate()], provider)  # type: ignore[arg-type]
        assert len(outcome.unjudged) == 1
        assert "无法解析" in outcome.failure_reason

    async def test_omitted_candidate_marked_unjudged(self) -> None:
        """批量的代价是模型可能漏答。漏答不能静默变成「这里没问题」。"""
        provider = _FakeProvider(
            [_reply({"id": 0, "verdict": "report", "severity": "low", "reason": "答了第一条"})]
        )
        outcome = await judge_candidates(  # type: ignore[arg-type]
            [_candidate(line=1), _candidate(line=2)], provider
        )
        assert len(outcome.findings) == 1
        assert len(outcome.unjudged) == 1
        assert outcome.unjudged[0].line == 2

    async def test_one_failing_batch_does_not_lose_others(self) -> None:
        provider = _FakeProvider(
            [
                RuntimeError("第一批失败"),
                _reply({"id": 0, "verdict": "report", "severity": "low", "reason": "第二批成功"}),
            ]
        )
        candidates = [_candidate(line=i) for i in range(1, 3)]
        outcome = await judge_candidates(candidates, provider, batch_size=1)  # type: ignore[arg-type]
        assert len(outcome.findings) == 1
        assert len(outcome.unjudged) == 1

    async def test_empty_candidates_makes_no_call(self) -> None:
        provider = _FakeProvider([])
        outcome = await judge_candidates([], provider)  # type: ignore[arg-type]
        assert outcome.findings == []
        assert provider.requests == []


class TestRequestShape:
    async def test_uses_pro_tier_by_default(self) -> None:
        """取舍是低频高判断，正是 pro 档的定位（KTD3）。"""
        provider = _FakeProvider([_reply({"id": 0, "verdict": "suppress", "reason": "x"})])
        await judge_candidates([_candidate()], provider)  # type: ignore[arg-type]
        assert provider.requests[0]["tier"] == "pro"

    async def test_requests_json_response_format(self) -> None:
        provider = _FakeProvider([_reply({"id": 0, "verdict": "suppress", "reason": "x"})])
        await judge_candidates([_candidate()], provider)  # type: ignore[arg-type]
        assert provider.requests[0]["response_format"] == {"type": "json_object"}

    async def test_prompt_forbids_inventing_candidates(self) -> None:
        """「不能新增候选点」必须写进 prompt，这是压缩幻觉空间的前提。"""
        provider = _FakeProvider([_reply({"id": 0, "verdict": "suppress", "reason": "x"})])
        await judge_candidates([_candidate()], provider)  # type: ignore[arg-type]
        system = provider.requests[0]["messages"][0]["content"]  # type: ignore[index]
        assert "不能新增候选点" in system

    async def test_confidence_passed_to_model(self) -> None:
        """置信度决定模型的默认立场，必须随候选一起传。"""
        provider = _FakeProvider([_reply({"id": 0, "verdict": "suppress", "reason": "x"})])
        await judge_candidates(  # type: ignore[arg-type]
            [_candidate(confidence=Confidence.CONTEXTUAL)], provider
        )
        user = provider.requests[0]["messages"][1]["content"]  # type: ignore[index]
        assert "contextual" in user

    async def test_batches_respect_size(self) -> None:
        provider = _FakeProvider(
            [_reply({"id": 0, "verdict": "suppress", "reason": "x"}) for _ in range(3)]
        )
        await judge_candidates(  # type: ignore[arg-type]
            [_candidate(line=i) for i in range(3)], provider, batch_size=1
        )
        assert len(provider.requests) == 3

    async def test_snippet_included_for_judgment(self) -> None:
        """模型要看代码才能取舍，定位时取好的片段随请求发出。"""
        provider = _FakeProvider([_reply({"id": 0, "verdict": "suppress", "reason": "x"})])
        await judge_candidates([_candidate()], provider)  # type: ignore[arg-type]
        user = provider.requests[0]["messages"][1]["content"]  # type: ignore[index]
        assert "risky()" in user


class TestReplyTolerance:
    async def test_code_fenced_json_parsed(self) -> None:
        """有些模型即使要求了 JSON 模式仍会用 ``` 包裹。"""
        provider = _FakeProvider(
            ['```json\n{"judgments": [{"id": 0, "verdict": "suppress", "reason": "x"}]}\n```']
        )
        outcome = await judge_candidates([_candidate()], provider)  # type: ignore[arg-type]
        assert outcome.suppressed == 1

    async def test_bare_list_reply_parsed(self) -> None:
        provider = _FakeProvider(['[{"id": 0, "verdict": "suppress", "reason": "x"}]'])
        outcome = await judge_candidates([_candidate()], provider)  # type: ignore[arg-type]
        assert outcome.suppressed == 1

    async def test_string_id_coerced(self) -> None:
        provider = _FakeProvider(['{"judgments": [{"id": "0", "verdict": "suppress", "reason": "x"}]}'])
        outcome = await judge_candidates([_candidate()], provider)  # type: ignore[arg-type]
        assert outcome.suppressed == 1
