"""候选点的取舍判断。唯一调用 LLM 的评审环节。

**LLM 只做取舍，不做发现。** 它收到的是确定性检测器定位好的候选点，只能对每个候选
回答「报」或「不报」，无法新增位置——候选点的 path 与 line 来自 AST，不经 LLM。这样
幻觉空间被压缩到一个维度：它可能误判该不该报，但不可能凭空造出一个不存在的问题位置。

判断结果如何回到 Finding：LLM 给的是 verdict 与理由，而 Finding 的 path/line/kind
全部沿用候选点的值。LLM 的理由进 message，检测器的依据进 evidence——两者分开是有意的，
读者要能区分「机器凭什么定位到这里」与「模型凭什么认为值得管」。

置信度决定默认立场（见 models.Confidence）：CERTAIN 的候选除非有明确反证否则报出，
CONTEXTUAL 的候选默认不报。没有这个区分，模型会把「裸 except」与「这次 IO 没包 try」
当成同等可疑，而后者在多数代码里是正常的。

**判断失败不等于零命中。** LLM 不可用或返回不可解析时，按 R17 记为未完成并说明原因，
不静默把候选丢掉——那会让「查了但模型没答上」与「查了确实没问题」在产出中不可区分。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from backend.providers.llm import LLMProvider, Tier
from backend.review.models import (
    Candidate,
    Confidence,
    Finding,
    FindingCategory,
    Severity,
)

logger = logging.getLogger("codepilot.review")

# 单次请求里塞多少候选。
#
# 批量而非逐个：逐个调用在 100 个候选上就是 100 次请求，限流与耗时都不可接受。
# 批量的代价是模型可能漏答某几条——所以要按 id 对齐结果，漏答的按「未判断」处理
# 而不是当成「不报」。
#
# 取 8 的依据：每个候选带 5 行片段，8 个约 1500-2500 token，远低于上下文上限，
# 又让模型能逐条给出理由而不敷衍。
DEFAULT_BATCH_SIZE = 8

_SEVERITY_BY_NAME = {s.value: s for s in Severity}

_SYSTEM_PROMPT = """你是代码评审的取舍环节。上游的静态分析已定位好候选点，你的唯一任务是判断每个候选是否值得报告给开发者。

你不能新增候选点，也不能修改候选点的位置。只对给定的候选逐一裁定。

裁定标准：
- 只报真实问题。宁可只报 5 条真问题，不要 50 条泛泛之谈。
- 候选带有 confidence 字段，它决定你的默认立场：
  - certain：判据本身已足够定性，除非代码上下文有明确反证（例如刻意的兜底清理路径），否则应当报出。
  - contextual：需要看上下文才能判断，默认不报；只有当上下文显示确实会导致问题时才报。
- 判断时考虑所在函数的职责。异常有意向上传给调用方处理是正常设计，不是缺陷。
- 测试代码、示例代码、脚本中的问题严重度通常低于生产代码。

对每个候选返回：
- id：候选的 id，原样返回
- verdict："report" 或 "suppress"
- severity："high" / "medium" / "low"，仅在 verdict 为 report 时有意义
- reason：一句话说明裁定依据。report 时说明它为什么是真问题；suppress 时说明为什么不必管。

只输出 JSON，形如 {"judgments": [{"id": 1, "verdict": "report", "severity": "medium", "reason": "..."}]}

reason 用中文。文件路径、符号名与代码片段保留原文，不要翻译。"""


@dataclass(frozen=True)
class JudgmentOutcome:
    """判断环节的结果。

    unjudged 单独记录而非混入 suppressed：前者是「没问过或没答上」，后者是「问过且
    认为不必报」。两者在产出中必须可区分（R17），否则模型故障会伪装成「代码没问题」。
    """

    findings: list[Finding]
    suppressed: int
    unjudged: list[Candidate]
    failure_reason: str = ""


def _candidate_payload(index: int, candidate: Candidate) -> dict[str, object]:
    return {
        "id": index,
        "kind": candidate.kind,
        "path": candidate.path,
        "line": candidate.line,
        "confidence": candidate.confidence.value,
        "enclosing_function": candidate.enclosing or "(模块级)",
        "detector_evidence": candidate.detector_evidence,
        "code": candidate.snippet,
    }


def _to_finding(candidate: Candidate, reason: str, severity: Severity) -> Finding:
    """把裁定为 report 的候选转成 Finding。

    path/line/kind 全部沿用候选点——它们来自 AST，不经 LLM，所以「位置」这一维不可能
    是幻觉。message 用模型的理由，evidence 保留检测器的依据并标注模型已复核，让读者
    能区分两种依据。
    """
    return Finding(
        category=candidate.category,
        kind=candidate.kind,
        path=candidate.path,
        line=candidate.line,
        message=reason,
        evidence=f"{candidate.detector_evidence}（{candidate.confidence.value} 级候选，已经模型复核）",
        severity=severity,
        related_paths=(),
    )


def _parse_judgments(raw: str) -> dict[int, dict[str, str]]:
    """解析模型返回。解析不出返回空字典，由调用方按「未判断」处理。"""
    text = raw.strip()
    # 有些模型会用 ```json 包裹，即使要求了 JSON 模式。
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}

    items = payload.get("judgments") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return {}

    parsed: dict[int, dict[str, str]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            key = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        parsed[key] = {
            "verdict": str(item.get("verdict", "")).lower(),
            "severity": str(item.get("severity", "")).lower(),
            "reason": str(item.get("reason", "")).strip(),
        }
    return parsed


def _default_severity(candidate: Candidate) -> Severity:
    if candidate.category is FindingCategory.SECURITY:
        return Severity.HIGH if candidate.confidence is Confidence.CERTAIN else Severity.MEDIUM
    return Severity.MEDIUM


async def judge_candidates(
    candidates: list[Candidate],
    provider: LLMProvider,
    tier: Tier = "pro",
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> JudgmentOutcome:
    """对候选点逐批取舍。

    用 pro 档：取舍是低频高判断，正是 KTD3 里 pro 档的定位。评审的价值全在这一步的
    准确度上，用 flash 省下的成本换不来同等质量。
    """
    if not candidates:
        return JudgmentOutcome(findings=[], suppressed=0, unjudged=[])

    findings: list[Finding] = []
    suppressed = 0
    unjudged: list[Candidate] = []
    failures: list[str] = []

    for start in range(0, len(candidates), batch_size):
        batch = candidates[start : start + batch_size]
        payload = [_candidate_payload(i, c) for i, c in enumerate(batch)]

        try:
            response = await provider.chat(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"candidates": payload}, ensure_ascii=False, indent=1
                        ),
                    },
                ],
                tier=tier,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001 — 判断失败要转成数据，不能中断评审
            logger.warning("判断批次失败 %s: %s", type(exc).__name__, exc)
            failures.append(f"{type(exc).__name__}: {exc}")
            unjudged.extend(batch)
            continue

        verdicts = _parse_judgments(content)
        if not verdicts:
            failures.append("模型返回无法解析为裁定列表")
            unjudged.extend(batch)
            continue

        for index, candidate in enumerate(batch):
            verdict = verdicts.get(index)
            if verdict is None:
                # 批量的代价：模型可能漏答。漏答按未判断处理，不当成「不报」——
                # 后者会让漏答静默变成「这里没问题」。
                unjudged.append(candidate)
                continue

            if verdict["verdict"] == "report":
                severity = _SEVERITY_BY_NAME.get(
                    verdict["severity"], _default_severity(candidate)
                )
                reason = verdict["reason"] or "模型判定为真实问题但未给出理由"
                findings.append(_to_finding(candidate, reason, severity))
            elif verdict["verdict"] == "suppress":
                suppressed += 1
            else:
                unjudged.append(candidate)

    findings.sort(key=lambda f: (f.path, f.line, f.kind))
    return JudgmentOutcome(
        findings=findings,
        suppressed=suppressed,
        unjudged=unjudged,
        failure_reason="；".join(dict.fromkeys(failures)),
    )
