"""报告校验。R7 从「要求」变成「保证」的地方。

没有这一层，可追溯性只是 prompt 里的期望——模型被要求给引用，但没人检查它给的引用
是否存在、是否指向真实行号。计划把这一点点明为本单元的核心逻辑。

四条校验规则，对应四种失效：

  无引用           结论没有 citations。「代码结构清晰」这类表述给不出引用。
  路径不存在       引用了仓库里没有的文件。模型编造路径的典型形态。
  行号越界         路径真实但行号超出文件实际行数。比编造路径更隐蔽。
  行范围倒置       end_line 小于 line。通常是模型笔误，但会让引用无法定位。

**校验产出的是判定而非异常。** 一条结论不合格不该让整份报告失败：其余结论仍有价值，
而读者需要知道哪些结论被拒绝以及为什么。所以返回结构化结果，由调用方决定是拒绝该项
还是降级保留。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from backend.paths import PathEscapeError, resolve_within
from backend.report.schema import ArchitectureReport, Citation, Claim


class ClaimVerdict(str, Enum):
    VALID = "valid"
    """全部引用有效。"""
    NO_CITATION = "no_citation"
    """没有引用。R7 的硬底线，不可降级保留。"""
    INVALID_CITATION = "invalid_citation"
    """有引用但部分或全部无效。"""


@dataclass(frozen=True)
class CitationProblem:
    citation: Citation
    reason: str


@dataclass(frozen=True)
class ClaimResult:
    section_key: str
    claim: Claim
    verdict: ClaimVerdict
    problems: tuple[CitationProblem, ...] = ()
    valid_citations: tuple[Citation, ...] = ()

    @property
    def is_valid(self) -> bool:
        return self.verdict is ClaimVerdict.VALID

    @property
    def is_salvageable(self) -> bool:
        """部分引用有效时可降级保留——剩下的有效引用仍支撑这条结论。

        完全无引用则不可救：那条结论从一开始就没有依据。
        """
        return bool(self.valid_citations) and self.verdict is not ClaimVerdict.NO_CITATION


@dataclass
class ValidationReport:
    results: list[ClaimResult] = field(default_factory=list)

    @property
    def valid(self) -> list[ClaimResult]:
        return [r for r in self.results if r.is_valid]

    @property
    def rejected(self) -> list[ClaimResult]:
        """不可救的结论：无引用，或全部引用无效。"""
        return [r for r in self.results if not r.is_valid and not r.is_salvageable]

    @property
    def salvageable(self) -> list[ClaimResult]:
        return [r for r in self.results if not r.is_valid and r.is_salvageable]

    def summary(self) -> str:
        total = len(self.results)
        if total == 0:
            return "报告没有任何结论项，校验无对象。"
        parts = [f"共 {total} 条结论：{len(self.valid)} 条引用完全有效"]
        if self.salvageable:
            parts.append(f"{len(self.salvageable)} 条部分引用无效（保留有效引用）")
        if self.rejected:
            no_citation = sum(
                1 for r in self.rejected if r.verdict is ClaimVerdict.NO_CITATION
            )
            parts.append(
                f"{len(self.rejected)} 条被拒绝"
                f"（其中 {no_citation} 条无任何引用）"
            )
        return "；".join(parts) + "。"


class _LineCounter:
    """按需读取文件行数并缓存。

    校验行号必须知道文件实际有多少行，而这要读文件。缓存是必需的：一份报告里同一个
    文件常被多条结论引用，重复读几十次没有必要。
    """

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir
        self._cache: dict[str, int | None] = {}

    def count(self, rel_path: str) -> int | None:
        """返回行数，路径无效或不可读时返回 None。"""
        if rel_path in self._cache:
            return self._cache[rel_path]

        result: int | None = None
        try:
            absolute = resolve_within(self._workdir, rel_path)
        except PathEscapeError:
            # 引用路径逃逸出仓库：既是无效引用，也是安全信号。
            self._cache[rel_path] = None
            return None

        if absolute.is_file():
            try:
                with absolute.open("rb") as handle:
                    result = sum(1 for _ in handle)
            except OSError:
                result = None

        self._cache[rel_path] = result
        return result


def _check_citation(
    citation: Citation, known_paths: frozenset[str], counter: _LineCounter
) -> str:
    """校验单条引用。返回空串表示有效，否则为失效原因。"""
    if not citation.path:
        return "引用未给出文件路径"

    if citation.path not in known_paths:
        return f"引用的路径不在仓库文件树中：{citation.path}"

    if citation.line is None:
        return ""

    if citation.line < 1:
        return f"行号必须为正数，实际为 {citation.line}"

    if citation.end_line is not None and citation.end_line < citation.line:
        return f"行范围倒置：{citation.line}-{citation.end_line}"

    total = counter.count(citation.path)
    if total is None:
        return f"无法读取被引用文件以校验行号：{citation.path}"

    upper = citation.end_line if citation.end_line is not None else citation.line
    if upper > total:
        return f"行号 {upper} 超出文件实际行数 {total}：{citation.path}"

    return ""


def validate_claim(
    section_key: str,
    claim: Claim,
    known_paths: frozenset[str],
    counter: _LineCounter,
) -> ClaimResult:
    if not claim.citations:
        return ClaimResult(
            section_key=section_key,
            claim=claim,
            verdict=ClaimVerdict.NO_CITATION,
            problems=(
                CitationProblem(
                    citation=Citation(path=""),
                    reason="结论没有任何引用，无法核验（R7 要求每个结论可追溯到文件路径）",
                ),
            ),
        )

    problems: list[CitationProblem] = []
    valid: list[Citation] = []
    for citation in claim.citations:
        reason = _check_citation(citation, known_paths, counter)
        if reason:
            problems.append(CitationProblem(citation=citation, reason=reason))
        else:
            valid.append(citation)

    if not problems:
        return ClaimResult(
            section_key=section_key,
            claim=claim,
            verdict=ClaimVerdict.VALID,
            valid_citations=tuple(valid),
        )

    return ClaimResult(
        section_key=section_key,
        claim=claim,
        verdict=ClaimVerdict.INVALID_CITATION,
        problems=tuple(problems),
        valid_citations=tuple(valid),
    )


def validate_report(
    report: ArchitectureReport, workdir: Path, known_paths: frozenset[str]
) -> ValidationReport:
    """校验整份报告的全部结论。

    known_paths 由调用方从文件树给出，而非在这里遍历目录：调用方已经有文件清单
    （解析层的产出），重新遍历既慢也可能与报告依据的清单不一致。
    """
    counter = _LineCounter(workdir)
    validation = ValidationReport()
    for section_key, claim in report.all_claims():
        validation.results.append(
            validate_claim(section_key, claim, known_paths, counter)
        )
    return validation


def apply_validation(
    report: ArchitectureReport, validation: ValidationReport
) -> ArchitectureReport:
    """按校验结果重建报告：拒绝不可救的结论，可救的只保留有效引用。

    不原地修改：报告是 dataclass，原地改会让「校验前」与「校验后」无法对比，而两者
    的差异本身是有用信息（多少结论被拒绝了）。
    """
    from dataclasses import replace

    kept: dict[str, list[Claim]] = {section.key: [] for section in report.sections()}

    for result in validation.results:
        if result.is_valid:
            kept[result.section_key].append(result.claim)
        elif result.is_salvageable:
            kept[result.section_key].append(
                replace(result.claim, citations=result.valid_citations)
            )

    return replace(
        report,
        module_breakdown=replace(
            report.module_breakdown, claims=tuple(kept["module_breakdown"])
        ),
        dependencies=replace(report.dependencies, claims=tuple(kept["dependencies"])),
        entrypoints=replace(report.entrypoints, claims=tuple(kept["entrypoints"])),
        key_flows=replace(report.key_flows, claims=tuple(kept["key_flows"])),
        tech_stack=replace(report.tech_stack, claims=tuple(kept["tech_stack"])),
    )
