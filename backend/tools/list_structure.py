"""列目录结构工具。

安全要点：条目必须落在仓库工作目录内。`rglob`/`iterdir` 会穿透符号链接与目录联接，
所以每个条目都过 KTD14 的单一校验——测试场景明确要求「输出不逃逸仓库工作目录」，而
这正是 U4 的 manifest 扫描踩过的坑（glob 穿透联接读到了仓库外的文件）。

只列一层而非递归全展：递归在大仓库上会产出上万条目，撑爆上下文且信息密度极低。
Agent 需要往下看时再调一次，这也让它的探索路径在 trace 里可见。
"""

from __future__ import annotations

from pathlib import Path

from backend.paths import PathEscapeError, resolve_within
from backend.tools.context import ToolContext
from backend.tools.results import StructureEntry, StructureResult, ToolError

# 单次返回的条目上限。超出按名字排序截断——稳定顺序让 Agent 能预期第二次调用看到什么。
DEFAULT_MAX_ENTRIES = 80

# 不列出的目录。它们体量大且与架构理解无关。
SKIP_NAMES = frozenset(
    {
        "node_modules",
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".nx",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)


def _count_files(directory: Path) -> int:
    """数目录下的直接子文件。给 Agent 判断值不值得展开的依据。"""
    try:
        return sum(1 for child in directory.iterdir() if child.is_file())
    except OSError:
        return 0


def list_structure(
    ctx: ToolContext,
    path: str = ".",
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> StructureResult | ToolError:
    """列出目录的直接子项。path 为相对仓库根的路径，默认仓库根。"""
    target = path.strip() or "."
    try:
        absolute = resolve_within(ctx.workdir, "" if target == "." else target)
    except PathEscapeError:
        return ToolError(
            message=f"路径校验未通过：{target}",
            hint="路径必须位于仓库工作目录内，且不能经由符号链接或目录联接。",
        )

    # 用解析后的根算相对路径。
    #
    # `iterdir()` 返回的条目继承 absolute 的绝对形态，而 ctx.workdir 可能是相对路径
    # （配置默认 `./.workspace`）——拿相对根去 relative_to 绝对条目会抛 ValueError。
    # 实测教训：单元测试全用 tmp_path（绝对），从未触发；而默认配置恰好是相对路径，
    # 于是这个工具在真实部署里必然失败。
    root = ctx.workdir.resolve()

    if not absolute.exists():
        return ToolError(message=f"目录不存在：{target}")
    if not absolute.is_dir():
        return ToolError(
            message=f"{target} 是文件，不是目录", hint="用 read_file 读取文件内容。"
        )

    try:
        children = sorted(absolute.iterdir(), key=lambda p: (p.is_file(), p.name))
    except OSError as exc:
        return ToolError(message=f"无法列出目录：{target}（{exc}）")

    entries: list[StructureEntry] = []
    skipped_unsafe = 0

    for child in children:
        if child.name in SKIP_NAMES or child.name.startswith("."):
            continue

        relative = child.relative_to(root).as_posix()
        # 逐条校验：iterdir 会穿透联接，仓库内一个指向外部的联接就能让条目落到仓库外。
        try:
            resolve_within(ctx.workdir, relative)
        except PathEscapeError:
            skipped_unsafe += 1
            continue

        if len(entries) >= max_entries:
            break

        entries.append(
            StructureEntry(
                path=relative,
                is_dir=child.is_dir(),
                file_count=_count_files(child) if child.is_dir() else 0,
            )
        )

    notes: list[str] = []
    total_listable = sum(
        1
        for c in children
        if c.name not in SKIP_NAMES and not c.name.startswith(".")
    )
    if total_listable > len(entries) + skipped_unsafe:
        notes.append(f"仅返回前 {len(entries)} 个条目，共 {total_listable} 个")
    if skipped_unsafe:
        notes.append(f"{skipped_unsafe} 个条目因路径校验未通过被跳过（符号链接或目录联接）")

    return StructureResult(
        root=target,
        entries=entries,
        truncated_note="；".join(notes),
    )
