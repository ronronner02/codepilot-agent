"""Reviewer 节点。把三类检查接进 LangGraph。

**它读不到报告内容，这由图结构保证而非约定。** Reviewer 分支从 cluster 取骨架，与
synthesize 无数据依赖（KTD9）。若让它看到报告，其发现会被报告叙述锚定，倾向于确认
报告已说过的内容而非独立发现问题。节点内断言 state 里没有 report 字段，是为了让这条
约束在接线出错时立刻暴露，而不是等到评审结果变得可疑时才怀疑。

三类检查的执行方式不同：
  结构类   纯图计算，同步完成，不调 LLM。
  错误处理 AST 定位候选点后交 LLM 取舍。
  安全类   固定模式集匹配后交 LLM 取舍。

各类都产出 CategoryOutcome，零命中与未执行在其中可区分（R17、AE2）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from backend.config import Settings
from backend.graph.state import AnalysisState
from backend.providers.llm import LLMProvider
from backend.review.error_handling import find_error_handling_candidates
from backend.review.judgment import JudgmentOutcome, judge_candidates
from backend.review.models import (
    Candidate,
    CategoryOutcome,
    CheckStatus,
    FindingCategory,
    ReviewReport,
)
from backend.review.security import (
    describe_pattern_set,
    find_hardcoded_secrets,
    find_security_candidates,
    is_secret_shaped_file,
)
from backend.review.structural import run_structural_checks
from backend.static_analysis.parser import ParsedTree, parse_tree

logger = logging.getLogger("codepilot.review")


def _collect_candidates(
    workdir: Path, targets: tuple[str, ...], settings: Settings
) -> tuple[list[Candidate], list[Candidate], list[str]]:
    """在待检文件上跑确定性检测器。返回 (错误处理候选, 安全候选, 读取失败的文件)。

    密钥检测走文本层，所以对不可解析的文件也生效——DoD 要求密钥形态文件仍被扫描，
    而它们（`.env`、`*.pem`）本就不是 Python/TypeScript。
    """
    error_candidates: list[Candidate] = []
    security_candidates: list[Candidate] = []
    unreadable: list[str] = []

    for rel_path in targets:
        parsed = parse_tree(workdir, rel_path, settings.max_file_bytes)

        if isinstance(parsed, ParsedTree):
            error_candidates.extend(find_error_handling_candidates(parsed))
            security_candidates.extend(find_security_candidates(parsed))
            text = parsed.source.decode("utf-8", errors="replace")
        elif is_secret_shaped_file(rel_path):
            # 不可解析但属密钥形态：仍要扫文本层。
            try:
                text = (workdir / rel_path).read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                unreadable.append(f"{rel_path}（{exc}）")
                continue
        else:
            continue

        security_candidates.extend(find_hardcoded_secrets(rel_path, text))

    return error_candidates, security_candidates, unreadable


def _outcome_from_judgment(
    category: FindingCategory,
    candidates: list[Candidate],
    judgment: JudgmentOutcome,
    scope: str,
) -> CategoryOutcome:
    """把判断结果转成 CategoryOutcome。

    全部候选都未判断时记为 FAILED——此时「零发现」不代表没问题，而是判断环节没跑通。
    部分未判断则仍算已执行，但把数量写进 scope，让读者知道产出不完整。
    """
    if candidates and len(judgment.unjudged) == len(candidates):
        return CategoryOutcome(
            category=category,
            status=CheckStatus.FAILED,
            scope=scope,
            reason=(
                f"{len(candidates)} 个候选点全部未完成取舍判断"
                f"（{judgment.failure_reason or '原因未记录'}）；"
                "本类结果不可用，零发现不代表无问题"
            ),
        )

    detail = scope
    if judgment.suppressed:
        detail += f"；模型判定 {judgment.suppressed} 个候选不必报告"
    if judgment.unjudged:
        detail += (
            f"；{len(judgment.unjudged)} 个候选未完成判断"
            f"（{judgment.failure_reason or '原因未记录'}）"
        )

    return CategoryOutcome(
        category=category,
        status=CheckStatus.EXECUTED,
        scope=detail,
        findings=judgment.findings,
    )


async def run_review(
    state: AnalysisState, settings: Settings, provider: LLMProvider | None = None
) -> ReviewReport:
    """执行三类检查，产出评审报告。

    provider 为 None 时跳过需要 LLM 的两类并说明原因，不静默把候选当成零命中——
    「没有 LLM 可用」与「查过没发现」在产出中必须不同（R17）。
    """
    assert "report" not in state, "评审分支不应接收报告内容（KTD9）"

    graph = state.get("dependency_graph")
    modules = state.get("modules") or []
    entrypoints = state.get("entrypoints") or []
    targets = tuple(state.get("review_targets") or ())
    workdir = Path(state.get("workdir") or ".")

    outcomes: list[CategoryOutcome] = []

    # 结构类：纯图计算，与 LLM 可用性无关。
    if graph is None:
        outcomes.append(
            CategoryOutcome(
                category=FindingCategory.STRUCTURAL,
                status=CheckStatus.SKIPPED,
                scope="无",
                reason="state 中无依赖图，结构类检查无输入",
            )
        )
    else:
        outcomes.append(run_structural_checks(graph, modules, entrypoints))

    error_candidates, security_candidates, unreadable = _collect_candidates(
        workdir, targets, settings
    )

    error_scope = f"{len(targets)} 个待检文件；检查项：裸 except、异常被吞、IO 缺错误处理、资源未关闭"
    security_scope = f"{len(targets)} 个待检文件；模式集：{describe_pattern_set()}"
    if unreadable:
        security_scope += f"；{len(unreadable)} 个文件读取失败：{', '.join(unreadable[:3])}"

    if provider is None:
        for category, scope, candidates in (
            (FindingCategory.ERROR_HANDLING, error_scope, error_candidates),
            (FindingCategory.SECURITY, security_scope, security_candidates),
        ):
            outcomes.append(
                CategoryOutcome(
                    category=category,
                    status=CheckStatus.SKIPPED,
                    scope=scope,
                    reason=(
                        f"已定位 {len(candidates)} 个候选点，但未配置 LLM provider，"
                        "取舍判断未执行；候选点未经复核，不作为发现产出"
                    ),
                )
            )
    else:
        for category, scope, candidates in (
            (FindingCategory.ERROR_HANDLING, error_scope, error_candidates),
            (FindingCategory.SECURITY, security_scope, security_candidates),
        ):
            judgment = await judge_candidates(candidates, provider, tier="pro")
            outcomes.append(_outcome_from_judgment(category, candidates, judgment, scope))

    return ReviewReport(target_files=targets, outcomes=outcomes)
