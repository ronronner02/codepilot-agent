"""架构报告的结构。

**报告是结构化对象而非自由文本。** 这不是形式偏好：R7 要求每个结论可追溯到文件路径，
而自由文本里的「结论」和「引用」无法分离——校验器没法判断某句话有没有依据。把结论拆成
带 citations 的独立项之后，「无依据的结论」变成一个可判定的属性，校验才可能存在。

缺失部分单独成节（R11）。它必须始终存在，即使为空——空节与「没有这一节」在读者看来
不同：前者说明查过没缺失，后者让人怀疑是不是漏了。这与 AE2 对零命中的要求同源。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Citation:
    """一条引用。行号可选——模块级结论未必落在某一行。

    line 与 end_line 都给出时表示行范围；只给 line 表示单行；都不给表示整个文件。
    """

    path: str
    line: int | None = None
    end_line: int | None = None

    def render(self) -> str:
        if self.line is None:
            return self.path
        if self.end_line is None or self.end_line == self.line:
            return f"{self.path}:{self.line}"
        return f"{self.path}:{self.line}-{self.end_line}"


@dataclass(frozen=True)
class Claim:
    """一条结论及其依据。

    citations 为空即视为无效结论（校验会拒绝）。这是 R7 的落点：「本项目采用分层架构、
    代码结构清晰」这类放到任何仓库都成立的表述给不出引用，因此过不了校验。
    """

    text: str
    citations: tuple[Citation, ...] = ()


@dataclass(frozen=True)
class ReportSection:
    """报告的一节。title 是给读者的小标题，claims 是这一节的结论。"""

    key: str
    title: str
    claims: tuple[Claim, ...] = ()


@dataclass(frozen=True)
class SkippedModule:
    """未深挖的模块及原因（R11）。

    区分「Planner 判定不值得挖」与「子 Agent 失败」——两者对读者的含义完全不同：
    前者是有意的范围决定，后者是分析缺失。
    """

    name: str
    reason: str
    kind: str = "not_planned"
    """`not_planned`（Planner 剔除）或 `failed`（子 Agent 失败）。"""


@dataclass
class MissingParts:
    """报告的缺失部分。始终存在，为空时也要明说无缺失（R11）。"""

    unparsed_files: int = 0
    unparsed_reasons: dict[str, int] = field(default_factory=dict)
    """原因到文件数的映射。逐个列出上千个未解析文件没有意义，按原因归类才有。"""
    skipped_modules: tuple[SkippedModule, ...] = ()
    unsupported_languages: tuple[str, ...] = ()
    """未做符号级解析的语言。语言范围限定的直接后果，读者需要知道。"""
    degraded_notes: tuple[str, ...] = ()
    """其他降级说明，如依赖图降级为目录级粒度。"""

    @property
    def is_empty(self) -> bool:
        return not (
            self.unparsed_files
            or self.skipped_modules
            or self.unsupported_languages
            or self.degraded_notes
        )

    def render(self) -> str:
        if self.is_empty:
            # 空节也要有内容：空白会让读者怀疑是不是漏了这一节。
            return "本次分析无缺失部分：所有文件均完成符号级解析，所有模块均已分析。"

        lines: list[str] = []
        if self.unparsed_files:
            detail = "；".join(
                f"{reason}（{count} 个）"
                for reason, count in sorted(
                    self.unparsed_reasons.items(), key=lambda kv: -kv[1]
                )[:5]
            )
            lines.append(f"未做符号级解析的文件 {self.unparsed_files} 个。原因分布：{detail}")
        if self.unsupported_languages:
            lines.append(
                f"以下语言不在符号级解析范围内，仅计入文件树与规模统计："
                f"{', '.join(self.unsupported_languages)}"
            )
        if self.skipped_modules:
            lines.append(f"未深挖的模块 {len(self.skipped_modules)} 个：")
            lines.extend(
                f"  {m.name}（{'分析失败' if m.kind == 'failed' else '未纳入深挖范围'}）：{m.reason}"
                for m in self.skipped_modules
            )
        if self.degraded_notes:
            lines.append("降级说明：")
            lines.extend(f"  {note}" for note in self.degraded_notes)
        return "\n".join(lines)


@dataclass
class ArchitectureReport:
    """一份完整的架构报告。

    各节为独立字段而非一个 sections 列表：字段名固定让校验与渲染都能针对具体节做
    不同处理（例如技术栈的结论允许引用配置文件，而模块划分必须引用源文件）。
    """

    repo: str
    commit_sha: str = ""
    summary: str = ""
    """总体印象。它本身不是结论，不参与 citations 校验，故单独成字段。"""
    module_breakdown: ReportSection = field(
        default_factory=lambda: ReportSection(key="module_breakdown", title="模块划分")
    )
    dependencies: ReportSection = field(
        default_factory=lambda: ReportSection(key="dependencies", title="依赖关系")
    )
    entrypoints: ReportSection = field(
        default_factory=lambda: ReportSection(key="entrypoints", title="入口点")
    )
    key_flows: ReportSection = field(
        default_factory=lambda: ReportSection(key="key_flows", title="关键流程")
    )
    tech_stack: ReportSection = field(
        default_factory=lambda: ReportSection(key="tech_stack", title="技术栈")
    )
    missing: MissingParts = field(default_factory=MissingParts)

    def sections(self) -> tuple[ReportSection, ...]:
        return (
            self.module_breakdown,
            self.dependencies,
            self.entrypoints,
            self.key_flows,
            self.tech_stack,
        )

    def all_claims(self) -> list[tuple[str, Claim]]:
        """全部结论，带所属节的 key。校验遍历它。"""
        return [(section.key, claim) for section in self.sections() for claim in section.claims]
