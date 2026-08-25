"""报告汇总节点。用 pro 档（KTD3：汇总是低频高判断）。

**校验在这里落地，R7 才从要求变成保证。** 模型按 prompt 给引用，但校验决定哪些结论
真的进报告——没有校验，可追溯性只是 prompt 里的期望。被拒绝的结论数写进报告的缺失
说明，让读者知道有多少结论因无法核验而被丢弃。

按 Risks 的约定，落盘副作用集中在汇聚节点。目前这里不写盘（报告作为 state 字段返回，
由 U12 的 API 层决定怎么持久化），但若日后要落盘，位置在这里而非子 Agent——那边会因
checkpoint 恢复而重跑。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from backend.config import Settings
from backend.graph.prompts.synthesize import SYSTEM_PROMPT, build_synthesis_message
from backend.graph.state import AnalysisState
from backend.providers.llm import LLMProvider
from backend.report.schema import (
    ArchitectureReport,
    Citation,
    Claim,
    MissingParts,
    ReportSection,
    SkippedModule,
)
from backend.report.validate import ValidationReport, apply_validation, validate_report
from backend.static_analysis.models import Granularity

logger = logging.getLogger("codepilot.synthesize")

_SECTION_TITLES = {
    "module_breakdown": "模块划分",
    "dependencies": "依赖关系",
    "entrypoints": "入口点",
    "key_flows": "关键流程",
    "tech_stack": "技术栈",
}


def _build_skeleton(state: AnalysisState) -> dict[str, object]:
    """给模型的结构骨架摘要。

    只给统计与名单，不给完整符号表：模型的任务是综合各模块分析，不是重新发现结构。
    塞进全量数据会挤掉模块分析本身的空间。
    """
    graph = state.get("dependency_graph")
    modules = state.get("modules") or []
    entrypoints = state.get("entrypoints") or []
    profile = state.get("language_profile")

    skeleton: dict[str, object] = {
        "file_count": profile.total_files if profile is not None else 0,
        "languages": dict(profile.by_language) if profile is not None else {},
        "module_count": len(modules),
        "modules": [
            {
                "name": m.name,
                "file_count": len(m.files),
                "internal_edges": m.internal_edges,
                "external_edges": m.external_edges,
            }
            for m in modules[:40]
        ],
        "entrypoints": [
            {"path": e.path, "kind": e.kind.value, "evidence": e.evidence}
            for e in entrypoints[:15]
        ],
    }
    if graph is not None:
        skeleton["edge_count"] = sum(len(t) for t in graph.edges.values())
        skeleton["external_dependencies"] = sorted(graph.external)[:30]
        skeleton["cycles"] = [list(c) for c in graph.cycles[:10]]
    return skeleton


def _parse_citations(raw: object) -> tuple[Citation, ...]:
    if not isinstance(raw, list):
        return ()
    citations: list[Citation] = []
    for item in raw:
        if isinstance(item, str):
            citations.append(Citation(path=item))
            continue
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not isinstance(path, str):
            continue
        line = item.get("line")
        end_line = item.get("end_line")
        citations.append(
            Citation(
                path=path,
                line=line if isinstance(line, int) else None,
                end_line=end_line if isinstance(end_line, int) else None,
            )
        )
    return tuple(citations)


def _parse_section(key: str, raw: object) -> ReportSection:
    claims: list[Claim] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            claims.append(
                Claim(text=text.strip(), citations=_parse_citations(item.get("citations")))
            )
    return ReportSection(key=key, title=_SECTION_TITLES[key], claims=tuple(claims))


def _parse_report(raw: str, repo: str, commit_sha: str) -> ArchitectureReport | None:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    summary = payload.get("summary")
    return ArchitectureReport(
        repo=repo,
        commit_sha=commit_sha,
        summary=summary.strip() if isinstance(summary, str) else "",
        module_breakdown=_parse_section("module_breakdown", payload.get("module_breakdown")),
        dependencies=_parse_section("dependencies", payload.get("dependencies")),
        entrypoints=_parse_section("entrypoints", payload.get("entrypoints")),
        key_flows=_parse_section("key_flows", payload.get("key_flows")),
        tech_stack=_parse_section("tech_stack", payload.get("tech_stack")),
    )


def build_missing_parts(state: AnalysisState, rejected_claims: int) -> MissingParts:
    """汇总缺失部分（R11）。

    三个来源：解析层未处理的文件、Planner 剔除或子 Agent 失败的模块、图降级。
    再加一项校验拒绝数——那也是缺失：那些结论本该在报告里，因无法核验而丢弃。
    """
    outcome = state.get("parse_outcome")
    unparsed = list(outcome.unparsed) if outcome is not None else []
    reasons: dict[str, int] = {}
    for item in unparsed:
        key = item.reason.split("：")[0]
        reasons[key] = reasons.get(key, 0) + 1

    analysed = {a.module_name for a in (state.get("module_analyses") or [])}
    planned = {p.module.name for p in (state.get("planned_modules") or [])}

    skipped: list[SkippedModule] = []
    for failure in state.get("module_failures") or []:
        if failure.node != "module_agent" or not failure.scope:
            continue
        skipped.append(
            SkippedModule(name=failure.scope, reason=failure.error, kind="failed")
        )
    # Planner 挑中但既无分析也无失败记录的模块（例如被上游截断）。
    for name in sorted(planned - analysed - {s.name for s in skipped}):
        skipped.append(
            SkippedModule(name=name, reason="已纳入深挖范围但未产出分析", kind="failed")
        )

    unsupported = sorted(
        {
            item.reason.split("：")[-1].strip()
            for item in unparsed
            if "扩展名" in item.reason
        }
    )

    notes: list[str] = []
    graph = state.get("dependency_graph")
    if graph is not None and graph.granularity is not Granularity.FILE:
        notes.append(graph.degraded_reason or f"依赖图降级为{graph.granularity.value}级粒度")
    if rejected_claims:
        notes.append(
            f"{rejected_claims} 条结论因引用无法核验被丢弃"
            "（引用路径不存在、行号越界或未给出引用）"
        )

    return MissingParts(
        unparsed_files=len(unparsed),
        unparsed_reasons=reasons,
        skipped_modules=tuple(skipped),
        unsupported_languages=tuple(unsupported[:8]),
        degraded_notes=tuple(notes),
    )


async def synthesize_report(
    state: AnalysisState, settings: Settings, provider: LLMProvider | None = None
) -> dict[str, object]:
    """汇总并校验报告。返回可并入 state 的字段。"""
    repo = state.get("repo_url", "")
    commit = state.get("commit_sha", "")
    analyses = state.get("module_analyses") or []
    outcome = state.get("parse_outcome")
    known_paths = frozenset(f.path for f in outcome.parsed) if outcome is not None else frozenset()
    workdir = Path(state.get("workdir") or ".")

    failed = [
        {"name": f.scope, "reason": f.error}
        for f in (state.get("module_failures") or [])
        if f.node == "module_agent" and f.scope
    ]

    if provider is None or not analyses:
        reason = (
            "未配置 LLM provider" if provider is None else "没有任何模块分析结果"
        )
        report = ArchitectureReport(
            repo=repo,
            commit_sha=commit,
            summary=f"未生成架构报告：{reason}。",
            missing=build_missing_parts(state, rejected_claims=0),
        )
        return {
            "report": report,
            "unsupported_claims": [],
            "report_validation_summary": f"未生成报告：{reason}",
        }

    message = build_synthesis_message(
        repo=repo,
        skeleton=_build_skeleton(state),
        module_analyses=[
            {
                "module": a.module_name,
                "analysis": a.summary,
                "limitation": a.limitation,
            }
            for a in analyses
        ],
        failed_modules=failed,
    )

    try:
        response = await provider.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": message},
            ],
            tier="pro",
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001 — 汇总失败不该让整次分析无产出
        logger.warning("报告汇总失败 %s: %s", type(exc).__name__, exc)
        report = ArchitectureReport(
            repo=repo,
            commit_sha=commit,
            summary=f"报告汇总失败：{type(exc).__name__}: {exc}。各模块分析仍可单独查看。",
            missing=build_missing_parts(state, rejected_claims=0),
        )
        return {
            "report": report,
            "unsupported_claims": [],
            "report_validation_summary": f"汇总调用失败：{type(exc).__name__}",
        }

    parsed = _parse_report(content, repo, commit)
    if parsed is None:
        report = ArchitectureReport(
            repo=repo,
            commit_sha=commit,
            summary="报告汇总返回的内容无法解析为结构化报告。各模块分析仍可单独查看。",
            missing=build_missing_parts(state, rejected_claims=0),
        )
        return {
            "report": report,
            "unsupported_claims": [],
            "report_validation_summary": "汇总返回无法解析",
        }

    validation = validate_report(parsed, workdir, known_paths)
    validated = apply_validation(parsed, validation)
    validated.missing = build_missing_parts(state, rejected_claims=len(validation.rejected))

    return {
        "report": validated,
        "unsupported_claims": _unsupported_claim_notes(validation),
        "report_validation_summary": validation.summary(),
    }


def _unsupported_claim_notes(validation: ValidationReport) -> list[str]:
    """被拒绝结论的说明。

    非空即表示报告未达 R7——这个字段存在的意义是让「有多少结论没有依据」可被程序检查，
    而不是埋在日志里。
    """
    notes: list[str] = []
    for result in validation.rejected:
        reasons = "；".join(problem.reason for problem in result.problems[:3])
        notes.append(f"[{result.section_key}] {result.claim.text[:60]} —— {reasons}")
    return notes
