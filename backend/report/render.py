"""报告导出的渲染（U19，R-38、R-39、BR-003）。

**渲染源是 API 响应模型，不是内部 dataclass。** 界面消费的也是这份模型，所以「导出与界面口径
一致」（BR-003）是结构性的——不靠对照两份实现，而是同一份数据的两种呈现。

**零新依赖。** Markdown 是字符串拼接，HTML 是自包含单文件（内联样式，不引外部资源——导出物
要能离线打开）。引模板引擎会为一件确定的事引入一层可配置性。

**不含评分**（BR-002 延伸到导出）：架构评分、代码质量分、安全分、0-100 的合成分数一律不出现。
**不含凭证**（NA-02）：`ResultResponse` 里本来就没有凭证字段，这是结构性排除。

**未执行与零命中在导出里同样要可区分**（NA-09 延伸到导出）。这是最容易在导出实现里退化的
一处：拼一份「发现列表」比拼「三类各自的执行情况 + 发现列表」省事，而省掉的正是那个区分。
"""

from __future__ import annotations

from html import escape

from backend.api.schemas import (
    CategoryOutcomeModel,
    CitationModel,
    ReportModel,
    ResultResponse,
    ReviewModel,
)

# 类别与状态的中文标签。与前端的映射同源语义，但各自独立收口：
# 让后端 import 前端的常量做不到，而两处同时改错的可能远低于「导出里显示英文枚举名」的代价。
_CATEGORY_LABELS = {
    "structural": "结构类",
    "error_handling": "错误处理",
    "security": "安全可疑模式",
}

_SEVERITY_LABELS = {"high": "高", "medium": "中", "low": "低"}

_STATUS_LABELS = {"executed": "已执行", "skipped": "未执行", "failed": "执行失败"}


def _citation(citation: CitationModel) -> str:
    """引用的文本形态。与 `Citation.render()` 同一套规则。"""
    if citation.line is None or citation.line <= 0:
        return citation.path
    if citation.end_line is None or citation.end_line == citation.line:
        return f"{citation.path}:{citation.line}"
    return f"{citation.path}:{citation.line}-{citation.end_line}"


def _severity(value: str) -> str:
    """未知取值归入「中」并标注原值，与界面一致。丢弃它会让一条真实发现消失。"""
    known = _SEVERITY_LABELS.get(value)
    return known if known else f"中（原值 {value}）"


def _outcome_line(outcome: CategoryOutcomeModel) -> str:
    """一类检查的执行情况。三态的文案必须不同（NA-09）。"""
    label = _CATEGORY_LABELS.get(outcome.category, outcome.category)
    status = _STATUS_LABELS.get(outcome.status, outcome.status)
    if outcome.status == "executed":
        if outcome.hit_count == 0:
            return f"{label}：{status}，命中 0 条（覆盖范围：{outcome.scope}）"
        return f"{label}：{status}，命中 {outcome.hit_count} 条（覆盖范围：{outcome.scope}）"
    return f"{label}：{status} —— {outcome.reason or '未给出原因'}"


def export_filename(result: ResultResponse, extension: str) -> str:
    """导出文件名。带仓库标识与 commit 短 SHA，便于区分多次分析的产物。"""
    slug = result.repo.replace("/", "__") or "analysis"
    sha = (result.commit_sha or "")[:12]
    stem = f"{slug}-{sha}" if sha else slug
    return f"{stem}.{extension}"


def render_markdown(result: ResultResponse) -> str:
    """渲染 Markdown。"""
    lines: list[str] = [f"# 代码分析报告：{result.repo}", ""]
    if result.commit_sha:
        lines.append(f"- commit：`{result.commit_sha}`")
    lines.append(f"- 任务标识：`{result.task_id}`")
    if result.language_profile:
        profile = result.language_profile
        lines.append(
            f"- 文件总数：{profile.total_files}（可解析 {profile.parseable_files}）"
        )
    if result.modules:
        lines.append(f"- 模块数：{len(result.modules)}")
    if result.index:
        lines.append(f"- 索引切块：{result.index.chunk_count}")
    lines.append("")

    lines.extend(_markdown_report(result.report))
    lines.extend(_markdown_review(result.review))
    return "\n".join(lines).rstrip() + "\n"


def _markdown_report(report: ReportModel | None) -> list[str]:
    if report is None:
        # 报告缺失不该让导出变成空文件：评审部分仍有价值，说明为什么没有报告同样有价值。
        return [
            "## 架构报告",
            "",
            "本次分析未产出架构报告。这通常意味着分析中途失败，或所有结论都无法追溯到"
            "具体文件。",
            "",
        ]

    lines = ["## 架构报告", ""]
    if report.summary:
        lines.extend([report.summary, ""])

    for section in report.sections:
        if not section.claims:
            continue
        lines.extend([f"### {section.title}", ""])
        for claim in section.claims:
            lines.append(f"- {claim.text}")
            for citation in claim.citations:
                # 引用带路径与行号，不截断（R-39：导出要保留可核验性）。
                lines.append(f"  - 依据：`{_citation(citation)}`")
        lines.append("")

    if report.unsupported_claims:
        lines.extend(
            [
                f"### 被丢弃的结论（{len(report.unsupported_claims)} 条）",
                "",
                "以下结论因引用无法核验被排除在报告之外：",
                "",
            ]
        )
        lines.extend(f"- {note}" for note in report.unsupported_claims)
        lines.append("")

    lines.extend(["### 分析的缺失部分", "", report.missing.text, ""])
    if report.validation_summary:
        lines.extend(["### 引用校验", "", report.validation_summary, ""])
    return lines


def _markdown_review(review: ReviewModel | None) -> list[str]:
    if review is None:
        return ["## 代码评审", "", "本次分析未产出评审数据。", ""]

    lines = ["## 代码评审", "", "### 检查执行情况", ""]
    if review.outcomes:
        lines.extend(f"- {_outcome_line(outcome)}" for outcome in review.outcomes)
    else:
        lines.append("- 本次分析未产出检查执行记录。")
    lines.append("")

    lines.extend([f"### 发现（{len(review.findings)} 条）", ""])
    if not review.findings:
        lines.extend(
            [
                "本次评审未产出发现。各类检查的执行情况见上——已执行且命中 0 条与未执行"
                "含义不同。",
                "",
            ]
        )
        return lines

    for finding in review.findings:
        label = _CATEGORY_LABELS.get(finding.category, finding.category)
        location = (
            f"{finding.path}:{finding.line}" if finding.line > 0 else finding.path
        )
        lines.append(f"- **[{_severity(finding.severity)}] {label} / {finding.kind}**")
        lines.append(f"  - 位置：`{location}`")
        if finding.line == 0:
            # R-22：0 不是行号，是「属于文件整体」。
            lines.append("  - 该问题属于文件整体")
        lines.append(f"  - 说明：{finding.message}")
        lines.append(f"  - 判断依据：{finding.evidence}")
    lines.append("")
    return lines


# 内联样式。自包含单文件的前提——引外部 CSS 的导出物离线打开就是无样式的纯文本流。
_HTML_STYLE = """
:root { color-scheme: light dark; }
body {
  font: 16px/1.7 -apple-system, "Segoe UI", "Noto Sans SC", sans-serif;
  max-width: 52rem; margin: 0 auto; padding: 2rem 1.25rem; color: #1a1c22;
  background: #fff;
}
h1 { font-size: 1.75rem; margin-bottom: .25rem; }
h2 { font-size: 1.3rem; margin-top: 2.25rem; border-bottom: 1px solid #e3e5ea; padding-bottom: .3rem; }
h3 { font-size: 1.05rem; margin-top: 1.5rem; }
code, pre { font-family: ui-monospace, "Cascadia Code", Consolas, monospace; font-size: .9em; }
code { background: #f2f3f6; padding: .1em .35em; border-radius: 3px; word-break: break-all; }
ul { padding-left: 1.4rem; }
li { margin: .3rem 0; }
.meta { color: #5b6070; font-size: .92rem; }
.meta li { margin: .15rem 0; }
.finding { border-left: 3px solid #c9ccd4; padding-left: .9rem; margin: 1rem 0; }
.finding--high { border-left-color: #c0392b; }
.finding--medium { border-left-color: #d98b16; }
.finding--low { border-left-color: #5b8def; }
.sev { font-weight: 600; }
.note { background: #f7f8fa; padding: .75rem 1rem; border-radius: 4px; }
""".strip()


def render_html(result: ResultResponse) -> str:
    """渲染自包含的单文件 HTML。

    每一处插值都过 `escape`：仓库里的标识符、报告结论与代码片段都可能含 `<` 与 `&`，
    不转义会让导出物的结构被内容破坏——而那在浏览器里表现为「后半页不见了」。
    """
    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>代码分析报告：{escape(result.repo)}</title>",
        f"<style>{_HTML_STYLE}</style>",
        "</head>",
        "<body>",
        f"<h1>代码分析报告：{escape(result.repo)}</h1>",
        '<ul class="meta">',
    ]
    if result.commit_sha:
        parts.append(f"<li>commit：<code>{escape(result.commit_sha)}</code></li>")
    parts.append(f"<li>任务标识：<code>{escape(result.task_id)}</code></li>")
    if result.language_profile:
        profile = result.language_profile
        parts.append(
            f"<li>文件总数：{profile.total_files}（可解析 {profile.parseable_files}）</li>"
        )
    if result.modules:
        parts.append(f"<li>模块数：{len(result.modules)}</li>")
    if result.index:
        parts.append(f"<li>索引切块：{result.index.chunk_count}</li>")
    parts.append("</ul>")

    parts.extend(_html_report(result.report))
    parts.extend(_html_review(result.review))
    parts.extend(["</body>", "</html>"])
    return "\n".join(parts) + "\n"


def _html_report(report: ReportModel | None) -> list[str]:
    if report is None:
        return [
            "<h2>架构报告</h2>",
            '<p class="note">本次分析未产出架构报告。这通常意味着分析中途失败，'
            "或所有结论都无法追溯到具体文件。</p>",
        ]

    parts = ["<h2>架构报告</h2>"]
    if report.summary:
        parts.append(f"<p>{escape(report.summary)}</p>")

    for section in report.sections:
        if not section.claims:
            continue
        parts.append(f"<h3>{escape(section.title)}</h3><ul>")
        for claim in section.claims:
            parts.append(f"<li>{escape(claim.text)}")
            if claim.citations:
                parts.append("<ul>")
                for citation in claim.citations:
                    parts.append(
                        f"<li>依据：<code>{escape(_citation(citation))}</code></li>"
                    )
                parts.append("</ul>")
            parts.append("</li>")
        parts.append("</ul>")

    if report.unsupported_claims:
        parts.append(f"<h3>被丢弃的结论（{len(report.unsupported_claims)} 条）</h3>")
        parts.append("<p>以下结论因引用无法核验被排除在报告之外：</p><ul>")
        parts.extend(f"<li>{escape(note)}</li>" for note in report.unsupported_claims)
        parts.append("</ul>")

    parts.append("<h3>分析的缺失部分</h3>")
    parts.append(f'<p class="note">{escape(report.missing.text)}</p>')
    if report.validation_summary:
        parts.append("<h3>引用校验</h3>")
        parts.append(f"<p>{escape(report.validation_summary)}</p>")
    return parts


def _html_review(review: ReviewModel | None) -> list[str]:
    if review is None:
        return ["<h2>代码评审</h2>", '<p class="note">本次分析未产出评审数据。</p>']

    parts = ["<h2>代码评审</h2>", "<h3>检查执行情况</h3><ul>"]
    if review.outcomes:
        parts.extend(
            f"<li>{escape(_outcome_line(outcome))}</li>" for outcome in review.outcomes
        )
    else:
        parts.append("<li>本次分析未产出检查执行记录。</li>")
    parts.append("</ul>")

    parts.append(f"<h3>发现（{len(review.findings)} 条）</h3>")
    if not review.findings:
        parts.append(
            '<p class="note">本次评审未产出发现。各类检查的执行情况见上——已执行且'
            "命中 0 条与未执行含义不同。</p>"
        )
        return parts

    for finding in review.findings:
        label = _CATEGORY_LABELS.get(finding.category, finding.category)
        location = f"{finding.path}:{finding.line}" if finding.line > 0 else finding.path
        severity_class = (
            finding.severity if finding.severity in _SEVERITY_LABELS else "medium"
        )
        parts.append(f'<div class="finding finding--{escape(severity_class)}">')
        parts.append(
            f'<p><span class="sev">[{escape(_severity(finding.severity))}]</span> '
            f"{escape(label)} / {escape(finding.kind)}</p>"
        )
        parts.append(f"<p>位置：<code>{escape(location)}</code></p>")
        if finding.line == 0:
            parts.append("<p>该问题属于文件整体</p>")
        parts.append(f"<p>说明：{escape(finding.message)}</p>")
        parts.append(f"<p>判断依据：{escape(finding.evidence)}</p>")
        parts.append("</div>")
    return parts
