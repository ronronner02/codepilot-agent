"""读文件工具。支持行范围。

**截断策略是本单元的成本要害。** 子 Agent 会反复读文件，每次返回的内容都进对话历史
并在后续每轮重新发送。一个 2000 行的文件全量返回，在 8 轮循环里就是 16000 行的累积
输入。所以默认窗口有上限，且截断必须说清楚——Agent 需要知道还有内容没看到，才会决定
是否继续读。

越界范围不报错而是收敛到有效区间（测试场景明确要求）：模型对文件行数只有估计，
`read_file(path, 1, 9999)` 是常见调用，为此报错会让它浪费一轮去试探边界。
"""

from __future__ import annotations

from backend.paths import PathEscapeError, resolve_within
from backend.tools.context import ToolContext
from backend.tools.results import FileSlice, ToolError

# 单次返回的行数上限。
#
# 取 200 的依据：足够看完一个中等函数或一个类的主体，而 8 轮循环累积约 1600 行，
# 在 flash 档的上下文里仍有余量。模型要看更多可以再调一次并给出新的行范围。
DEFAULT_MAX_LINES = 200

# 单行字符上限。压缩过的 JS/CSS 单行可能几万字符，一行就能撑爆上下文。
MAX_LINE_CHARS = 500


def read_file(
    ctx: ToolContext,
    path: str,
    start_line: int = 1,
    end_line: int | None = None,
    max_lines: int = DEFAULT_MAX_LINES,
) -> FileSlice | ToolError:
    """读取文件的指定行范围。行号 1-based 闭区间，与编辑器一致。"""
    try:
        absolute = resolve_within(ctx.workdir, path)
    except PathEscapeError:
        return ToolError(
            message=f"路径校验未通过：{path}",
            hint="路径必须位于仓库工作目录内，且不能经由符号链接或目录联接。",
        )

    if not absolute.exists():
        # 明确报错而非返回空内容——空内容会被模型读成「文件是空的」，继续往下推断。
        return ToolError(
            message=f"文件不存在：{path}",
            hint="用 list_structure 确认路径，注意路径是相对仓库根的。",
        )
    if absolute.is_dir():
        return ToolError(
            message=f"{path} 是目录，不是文件",
            hint="用 list_structure 列出目录内容。",
        )

    try:
        size = absolute.stat().st_size
        if size > ctx.max_file_bytes:
            return ToolError(
                message=f"文件体积 {size} 字节超出上限 {ctx.max_file_bytes}",
                hint="这类文件通常是生成物或数据文件，不适合逐行阅读。",
            )
        text = absolute.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ToolError(message=f"读取失败：{path}（{exc}）")

    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return FileSlice(
            path=path, start_line=0, end_line=0, total_lines=0, content="",
            truncated_note="文件为空",
        )

    # 越界收敛而非报错：模型对行数只有估计，为此报错会浪费一轮试探边界。
    begin = max(1, start_line)
    if begin > total:
        return FileSlice(
            path=path,
            start_line=total,
            end_line=total,
            total_lines=total,
            content=lines[total - 1],
            truncated_note=f"请求的起始行 {start_line} 超出文件末尾，已返回最后一行",
        )

    finish = total if end_line is None else min(end_line, total)
    finish = max(finish, begin)

    notes: list[str] = []
    if finish - begin + 1 > max_lines:
        finish = begin + max_lines - 1
        notes.append(
            f"仅返回 {max_lines} 行；文件共 {total} 行，"
            f"继续读请用 start_line={finish + 1}"
        )
    elif end_line is not None and end_line > total:
        notes.append(f"请求的结束行 {end_line} 超出文件末尾（共 {total} 行）")

    selected = lines[begin - 1 : finish]
    clipped = 0
    rendered: list[str] = []
    for line in selected:
        if len(line) > MAX_LINE_CHARS:
            clipped += 1
            rendered.append(line[:MAX_LINE_CHARS] + " …[本行已截断]")
        else:
            rendered.append(line)
    if clipped:
        notes.append(f"{clipped} 行超过 {MAX_LINE_CHARS} 字符已截断")

    return FileSlice(
        path=path,
        start_line=begin,
        end_line=finish,
        total_lines=total,
        content="\n".join(rendered),
        truncated_note="；".join(notes),
    )
