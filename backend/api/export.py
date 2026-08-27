"""报告导出端点（U19、U20，R-38~R-40）。

三种格式同源：MD 与 HTML 由 `backend/report/render.py` 从 API 响应模型渲染，PDF 复用 HTML
产物再转换。同源让 BR-003 的口径一致成为结构性事实——三份产物不可能给出不同的通过率。

**PDF 失败不返回损坏文件**（R-40）。weasyprint 抛异常时返回可区分的 reason 并提示改用其它
格式：一个打不开的 PDF 比一条明确的错误更糟，用户要下载、打开、失败三步之后才知道不对。

**weasyprint 是可选依赖**。它需要系统库（libpango、libharfbuzz）与中文字体，镜像体积 +80MB。
未安装时 PDF 分支返回 `pdf_unavailable`，MD 与 HTML 不受影响——这是 P1 的降级路径，不是缺陷。
"""

from __future__ import annotations

import logging
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Query, status
from fastapi.responses import JSONResponse, Response

from backend.api.schemas import ErrorResponse, ResultResponse
from backend.api.tasks import TaskRegistry
from backend.report.render import export_filename, render_html, render_markdown

logger = logging.getLogger("codepilot.api.export")

ExportFormat = Literal["md", "html", "pdf"]

_MEDIA_TYPES: dict[str, str] = {
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "pdf": "application/pdf",
}


def _error(reason: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(reason=reason, message=message).model_dump(),
    )


def _disposition(filename: str) -> str:
    """Content-Disposition 头。

    同时给 `filename` 与 `filename*`：前者是 ASCII 回退（老客户端），后者是 RFC 5987 的
    百分号编码形态（仓库名可能含非 ASCII）。只给前者会让中文文件名变成乱码。
    """
    ascii_fallback = filename.encode("ascii", "replace").decode("ascii")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"


def render_pdf(html: str) -> bytes:
    """HTML 转 PDF。

    在函数内 import weasyprint 而非模块顶层：它是可选依赖，顶层 import 会让整个 API 模块在
    未安装时无法加载——那会连 MD 与 HTML 一起废掉。

    `ImportError` 与渲染异常由调用方区分处理：前者是「这个部署没装 PDF 支持」，后者是「这份
    内容渲染失败」，用户的下一步不同。
    """
    from weasyprint import HTML  # type: ignore[import-untyped]

    return bytes(HTML(string=html).write_pdf())


def build_export_router(registry: TaskRegistry, resolve_result) -> APIRouter:  # type: ignore[no-untyped-def]
    """导出端点。

    `resolve_result` 由 routes 注入：给一个 task_id，返回 `ResultResponse` 或 None。它同时
    覆盖内存与落盘历史，所以重启后仍能导出——**这一点必须与读结果端点一致**，否则同一次
    分析会出现「报告能看但导不出」，而用户无从判断是哪一边坏了。

    传函数而非在这里重写映射：那份映射是导出与界面同源的保证（BR-003），复制它就等于放弃它。
    """
    router = APIRouter()

    @router.get(
        "/api/analyses/{task_id}/export",
        responses={
            400: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            500: {"model": ErrorResponse},
            501: {"model": ErrorResponse},
        },
    )
    async def export(
        task_id: str,
        format: str = Query("md", description="md | html | pdf"),
    ) -> Response:
        """导出当次分析的报告与评审发现。"""
        if format not in _MEDIA_TYPES:
            return _error(
                "unsupported_format",
                f"不支持的导出格式：{format}。可选 md、html、pdf。",
                status.HTTP_400_BAD_REQUEST,
            )

        result: ResultResponse | None = resolve_result(task_id)
        if result is None:
            return _error(
                "task_not_found", f"任务不存在：{task_id}", status.HTTP_404_NOT_FOUND
            )

        filename = export_filename(result, format)

        if format == "md":
            body = render_markdown(result).encode("utf-8")
        elif format == "html":
            body = render_html(result).encode("utf-8")
        else:
            try:
                body = render_pdf(render_html(result))
            except ImportError:
                logger.warning("PDF 导出不可用：weasyprint 未安装")
                return _error(
                    "pdf_unavailable",
                    "本部署未安装 PDF 生成组件（weasyprint 及其系统依赖）。"
                    "请改用 Markdown 或 HTML 格式导出。",
                    status.HTTP_501_NOT_IMPLEMENTED,
                )
            except Exception as exc:  # noqa: BLE001 — 任何渲染失败都不能返回半个文件
                logger.exception("PDF 生成失败：%s", task_id)
                return _error(
                    "pdf_failed",
                    f"PDF 生成失败（{type(exc).__name__}）。"
                    "请改用 Markdown 或 HTML 格式导出——它们的内容与 PDF 完全一致。",
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

        return Response(
            content=body,
            media_type=_MEDIA_TYPES[format],
            headers={"Content-Disposition": _disposition(filename)},
        )

    return router
