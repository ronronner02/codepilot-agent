"""文件内容与文件树端点（U3）。

**这是本期新增的公网可达的任意路径入口，所以路径校验不放宽（BR-007）。** 两条约束
叠在一起：既复用 `resolve_within`（KTD14 的单一校验，逐段拒绝符号链接与 junction），
又在端点层先拒绝绝对路径（R-57）。后者不是防逃逸——`resolve_within` 对绝对路径同样
逐段校验——而是收窄输入形态：公网端点上少接受一种写法，就少一种被构造的可能。

**读取上限与 Agent 侧不同源。** `backend/tools/read_file.py` 的 200 行上限是为了控住
对话历史的累积成本，而界面读文件要给人看完整文件。所以这里另立上限，但截断语义沿用
它：越界收敛不报错、截断必须可见（R-18）。

工作副本的三种缺席状态各有自己的 reason：还没克隆完、被配额清理删掉了（KTD10 的第四
态）、路径非法。三者对用户的下一步动作完全不同，混成一个「文件不存在」会让人以为是
自己路径写错了。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Query, status
from fastapi.responses import JSONResponse

from backend.api.schemas import (
    ErrorResponse,
    FileContentModel,
    FileTreeEntryModel,
    FileTreeModel,
)
from backend.api.tasks import TaskContext
from backend.paths import PathEscapeError, resolve_within
from backend.tools.list_structure import SKIP_NAMES

logger = logging.getLogger("codepilot.api.files")

# 界面侧单次返回的行数上限。
#
# 取 2000 而非 Agent 侧的 200：浏览器消费者要一次看完整个文件，而 2000 行覆盖绝大多数
# 源文件。超出的部分由界面按 start_line 续读，代价是一次额外请求。
UI_MAX_LINES = 2000

# 单行字符上限。压缩过的 JS/CSS 单行可能几万字符，一行就能让界面卡住。
UI_MAX_LINE_CHARS = 2000

# 文件体积上限。
#
# 与行数上限是两道独立的门：行数管「一次给多少」，体积管「这个文件适不适合逐行看」。
# 只有行数门的话，一个 50MB 的单行压缩产物仍会被整体读进内存再截断。
UI_MAX_FILE_BYTES = 524_288

# 单层目录的条目上限。比 Agent 侧的 80 略宽——界面的文件树可以滚动。
MAX_TREE_ENTRIES = 120


class FileRequestError(Exception):
    """一次读取请求的失败。reason 是稳定契约，status 决定 HTTP 状态码。"""

    def __init__(self, reason: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.status_code = status_code


def _error_response(exc: FileRequestError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(reason=exc.reason, message=exc.message).model_dump(),
    )


def resolve_workdir(context: TaskContext) -> Path:
    """定位该任务的仓库工作副本。

    三种缺席状态必须可区分（KTD10）：

    - **从落盘历史载入的任务**（`from_disk`）—— 分析发生过、结果可读，但工作副本的路径不在
      落盘内容里，且重启后那个目录通常也已不在。这是 KTD10 的第四态；
    - state 里还没有 workdir —— 分析尚未克隆完，界面该提示等待；
    - workdir 有值但目录不在了 —— 被磁盘配额 LRU 清理掉了。

    三者的下一步动作不同：等待、换一条路核验、或者接受源码不可看。混成一个「文件不存在」
    会让人以为是自己路径写错了。
    """
    if context.from_disk:
        raise FileRequestError(
            "workspace_cleared",
            "该分析的代码副本已不在本机（服务重启或配额清理），无法查看源文件；"
            "分析结果、报告与评审发现仍可读",
            status.HTTP_410_GONE,
        )
    if not context.workdir:
        raise FileRequestError(
            "workspace_unavailable",
            "该分析的代码副本尚未就绪，请等待克隆与解析完成",
            status.HTTP_409_CONFLICT,
        )
    workdir = Path(context.workdir)
    if not workdir.is_dir():
        raise FileRequestError(
            "workspace_cleared",
            "该分析的代码副本已被清理，无法查看源文件（分析结果仍可读）",
            status.HTTP_410_GONE,
        )
    return workdir


def _safe_target(workdir: Path, requested: str) -> Path:
    """把请求的相对路径解析为工作副本内的真实路径。

    绝对路径在这里就被拒（R-57），不进 `resolve_within`。拒绝的理由见模块 docstring。
    """
    candidate = requested.strip()
    if not candidate or candidate == ".":
        return workdir.resolve()
    if Path(candidate).is_absolute() or candidate.startswith(("/", "\\")):
        raise FileRequestError(
            "invalid_path",
            "路径必须是仓库相对路径，不接受绝对路径",
            status.HTTP_400_BAD_REQUEST,
        )
    try:
        return resolve_within(workdir, candidate)
    except PathEscapeError:
        # 不回显拒绝细节：这个端点在公网上，逐段说明「哪一段是符号链接」等于提供探测反馈。
        raise FileRequestError(
            "invalid_path",
            "无法读取该路径",
            status.HTTP_400_BAD_REQUEST,
        ) from None


def read_file_slice(
    workdir: Path,
    requested: str,
    start_line: int = 1,
    end_line: int | None = None,
) -> FileContentModel:
    """读取文件的指定行范围。行号 1-based 闭区间，与编辑器一致。"""
    target = _safe_target(workdir, requested)

    if not target.exists():
        raise FileRequestError(
            "file_not_found", f"文件不存在：{requested}", status.HTTP_404_NOT_FOUND
        )
    if target.is_dir():
        raise FileRequestError(
            "not_a_file",
            f"{requested} 是目录，不是文件",
            status.HTTP_400_BAD_REQUEST,
        )

    try:
        size = target.stat().st_size
        if size > UI_MAX_FILE_BYTES:
            raise FileRequestError(
                "file_too_large",
                f"文件体积 {size} 字节超出查看上限 {UI_MAX_FILE_BYTES}，"
                f"这类文件通常是生成物或数据文件",
                status.HTTP_413_CONTENT_TOO_LARGE,
            )
        # errors="replace" 而非抛错：仓库里混着非 UTF-8 的文件是常态，为此整个请求失败
        # 会让界面把「编码不同」呈现成「读不到」。
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise FileRequestError(
            "read_failed",
            f"读取失败：{requested}（{exc}）",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        ) from exc

    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return FileContentModel(
            path=requested, start_line=0, end_line=0, total_lines=0, content=""
        )

    # 越界收敛而非报错：界面跳转到某条发现的行号时，行范围可能超出文件末尾（报告依据的
    # 是分析时的快照，而文件可能已经变短）。为此报错会让跳转变成一个错误页。
    begin = min(max(1, start_line), total)
    finish = total if end_line is None else min(max(end_line, begin), total)

    notes: list[str] = []
    truncated = False
    if finish - begin + 1 > UI_MAX_LINES:
        finish = begin + UI_MAX_LINES - 1
        truncated = True
        notes.append(
            f"已截断，仅返回 {UI_MAX_LINES} 行；文件共 {total} 行，"
            f"继续读请用 start_line={finish + 1}"
        )

    clipped = 0
    rendered: list[str] = []
    for line in lines[begin - 1 : finish]:
        if len(line) > UI_MAX_LINE_CHARS:
            clipped += 1
            rendered.append(line[:UI_MAX_LINE_CHARS] + " …[本行已截断]")
        else:
            rendered.append(line)
    if clipped:
        truncated = True
        notes.append(f"{clipped} 行超过 {UI_MAX_LINE_CHARS} 字符已截断")

    return FileContentModel(
        path=requested,
        start_line=begin,
        end_line=finish,
        total_lines=total,
        content="\n".join(rendered),
        truncated=truncated,
        truncated_note="；".join(notes),
    )


def _count_files(directory: Path) -> int:
    try:
        return sum(1 for child in directory.iterdir() if child.is_file())
    except OSError:
        return 0


def list_tree(workdir: Path, requested: str = ".") -> FileTreeModel:
    """列出目录的直接子项，只一层。

    不递归：递归在大仓库上产出上万条目，而文件树是按需展开的交互——用户点开哪个目录
    才需要哪一层。跳过目录集复用 `list_structure.SKIP_NAMES`，同一份判断不写两遍。
    """
    root = workdir.resolve()
    target = _safe_target(workdir, requested)

    if not target.exists():
        raise FileRequestError(
            "directory_not_found",
            f"目录不存在：{requested}",
            status.HTTP_404_NOT_FOUND,
        )
    if not target.is_dir():
        raise FileRequestError(
            "not_a_directory",
            f"{requested} 是文件，不是目录",
            status.HTTP_400_BAD_REQUEST,
        )

    try:
        children = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
    except OSError as exc:
        raise FileRequestError(
            "read_failed",
            f"无法列出目录：{requested}（{exc}）",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        ) from exc

    entries: list[FileTreeEntryModel] = []
    skipped_unsafe = 0
    listable = 0

    for child in children:
        if child.name in SKIP_NAMES or child.name.startswith("."):
            continue
        listable += 1
        relative = child.relative_to(root).as_posix()
        # 逐条校验：iterdir 会穿透 junction，仓库内一个指向外部的 junction 就能让条目
        # 落到仓库外——而那条目随后会被界面当成可点开的文件。
        try:
            resolve_within(workdir, relative)
        except PathEscapeError:
            skipped_unsafe += 1
            continue
        if len(entries) >= MAX_TREE_ENTRIES:
            continue
        entries.append(
            FileTreeEntryModel(
                path=relative,
                is_dir=child.is_dir(),
                file_count=_count_files(child) if child.is_dir() else 0,
            )
        )

    notes: list[str] = []
    if listable > len(entries) + skipped_unsafe:
        notes.append(f"仅返回前 {len(entries)} 个条目，共 {listable} 个")
    if skipped_unsafe:
        notes.append(f"{skipped_unsafe} 个条目因路径校验未通过被跳过")

    return FileTreeModel(
        root=requested, entries=entries, truncated_note="；".join(notes)
    )


def build_files_router(resolve_context) -> APIRouter:  # type: ignore[no-untyped-def]
    """两个读取端点。路由层只做参数接收与错误转换，判断逻辑在上面的纯函数里。

    `resolve_context` 由 routes 注入：它同时覆盖内存与落盘历史，所以「任务不存在」与
    「代码副本已不在」不会混为一谈（KTD10）。
    """
    router = APIRouter()

    def resolve(task_id: str) -> TaskContext:
        context: TaskContext | None = resolve_context(task_id)
        if context is None:
            raise FileRequestError(
                "task_not_found", f"任务不存在：{task_id}", status.HTTP_404_NOT_FOUND
            )
        return context

    _RESPONSES: dict[int | str, dict[str, object]] = {
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        410: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
    }

    @router.get(
        "/api/analyses/{task_id}/file",
        response_model=FileContentModel,
        responses=_RESPONSES,
    )
    async def get_file(
        task_id: str,
        path: str = Query(min_length=1, description="仓库相对路径"),
        start_line: int = Query(1, ge=1),
        end_line: int | None = Query(None, ge=1),
    ) -> object:
        """读取工作副本内某文件的一段内容。"""
        try:
            workdir = resolve_workdir(resolve(task_id))
            return read_file_slice(workdir, path, start_line, end_line)
        except FileRequestError as exc:
            return _error_response(exc)

    @router.get(
        "/api/analyses/{task_id}/tree",
        response_model=FileTreeModel,
        responses=_RESPONSES,
    )
    async def get_tree(
        task_id: str,
        path: str = Query(".", description="仓库相对目录，默认仓库根"),
    ) -> object:
        """列出某目录的直接子项。"""
        try:
            workdir = resolve_workdir(resolve(task_id))
            return list_tree(workdir, path)
        except FileRequestError as exc:
            return _error_response(exc)

    return router
