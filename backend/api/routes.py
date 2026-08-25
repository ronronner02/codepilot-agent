"""HTTP 路由。REST 提交与读结果，SSE 推进度（KTD8）。

**错误按 AE6 分类，reason 与 HTTP 状态码并行给出。** 只给状态码不够——404 无法区分「仓库
不存在」与「任务标识不存在」，而界面要按这两种情况给不同的下一步提示。所以响应体带
`reason`（RejectReason 的取值），它是稳定契约；`message` 是文案，会随措辞调整而变。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from backend.api.progress import Stage
from backend.api.schemas import (
    AnalyzeAccepted,
    AnalyzeRequest,
    AnswerResponse,
    CategoryOutcomeModel,
    CitationModel,
    ClaimModel,
    ErrorResponse,
    FindingModel,
    IndexStatusModel,
    MissingPartsModel,
    QaCitationModel,
    QuestionRequest,
    ReportModel,
    ReportSectionModel,
    ResultResponse,
    ReviewModel,
    TaskSummary,
)
from backend.api.tasks import AnalysisTask, TaskRegistry
from backend.cache.key import build_cache_key
from backend.config import Settings
from backend.graph.real_nodes import _repo_slug
from backend.ingest.guards import RejectReason, RepoRejected
from backend.providers.embedding import build_embedding_provider
from backend.providers.llm import LLMProvider
from backend.rag.qa import answer_question

logger = logging.getLogger("codepilot.api")

# RejectReason 到 HTTP 状态码。
#
# 超规模用 413（Payload Too Large）而非 400：它表达的是「请求的对象太大」，与地址格式错
# 是不同的问题类别，而前端的处理也不同（前者建议换仓库，后者建议检查地址）。
_REASON_STATUS: dict[RejectReason, int] = {
    RejectReason.INVALID_URL: status.HTTP_400_BAD_REQUEST,
    RejectReason.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    RejectReason.NO_ACCESS: status.HTTP_403_FORBIDDEN,
    RejectReason.TOO_LARGE: status.HTTP_413_CONTENT_TOO_LARGE,
    RejectReason.NETWORK_ERROR: status.HTTP_502_BAD_GATEWAY,
}


def _rejection_response(exc: RepoRejected) -> JSONResponse:
    return JSONResponse(
        status_code=_REASON_STATUS.get(exc.reason, status.HTTP_400_BAD_REQUEST),
        content=ErrorResponse(reason=exc.reason.value, message=str(exc)).model_dump(),
    )


def build_router(settings: Settings, registry: TaskRegistry) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/api/analyses",
        response_model=AnalyzeAccepted,
        status_code=status.HTTP_202_ACCEPTED,
        responses={
            400: {"model": ErrorResponse},
            403: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            413: {"model": ErrorResponse},
        },
    )
    async def submit_analysis(payload: AnalyzeRequest) -> Any:
        """提交分析。立刻返回 202 与任务标识，分析在后台跑。

        地址非法在这里就返回错误，而不是先接受再让用户拉进度才发现——那会让一个可以
        立即判断的错误延迟几秒才可见。
        """
        try:
            task = registry.create(payload.repo_url)
        except RepoRejected as exc:
            return _rejection_response(exc)

        registry.start(task)
        return AnalyzeAccepted(
            task_id=task.task_id,
            repo=task.repo,
            stage=task.stage.value,
            message="已开始分析，用 /api/analyses/{task_id}/events 拉取进度",
        )

    @router.get("/api/analyses", response_model=list[TaskSummary])
    async def list_analyses() -> Any:
        return [
            TaskSummary(
                task_id=t.task_id,
                repo=t.repo,
                stage=t.stage.value,
                completed=t.completed,
                failed=t.failed,
            )
            for t in registry.list_tasks()
        ]

    @router.get("/api/analyses/{task_id}/events")
    async def stream_events(task_id: str, request: Request) -> Any:
        """SSE 进度流。

        后连接的客户端会先收到历史事件再接实时流——用户刷新页面后不该丢掉已发生的进度。
        """
        task = registry.get(task_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"任务不存在：{task_id}"
            )

        async def event_stream() -> Any:
            """推送事件直到收到结束哨兵。

            **不自己检查 request.is_disconnected()。** sse-starlette 的
            EventSourceResponse 已经监听断连并取消这个生成器，而手写检查在 TestClient
            的流式请求下会永久阻塞（实测：receive 通道在请求体读完后不再有消息）。
            断连时的清理由 finally 承担——它在生成器被取消时同样执行。
            """
            queue = task.subscribe()
            try:
                while True:
                    event = await queue.get()
                    if event is None:
                        break
                    yield {
                        "event": "progress",
                        "data": json.dumps(event.to_payload(), ensure_ascii=False),
                    }
            finally:
                task.unsubscribe(queue)

        return EventSourceResponse(event_stream())

    @router.get(
        "/api/analyses/{task_id}",
        response_model=ResultResponse,
        responses={404: {"model": ErrorResponse}},
    )
    async def get_result(task_id: str) -> Any:
        """读结果。报告与评审发现一并返回。"""
        task = registry.get(task_id)
        if task is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=ErrorResponse(
                    reason="task_not_found", message=f"任务不存在：{task_id}"
                ).model_dump(),
            )
        return _result_response(task)

    @router.post(
        "/api/analyses/{task_id}/questions",
        response_model=AnswerResponse,
        responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    )
    async def ask(task_id: str, payload: QuestionRequest) -> Any:
        """对已分析仓库提问（单轮）。"""
        task = registry.get(task_id)
        if task is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=ErrorResponse(
                    reason="task_not_found", message=f"任务不存在：{task_id}"
                ).model_dump(),
            )
        if not task.completed:
            # 409 而非 404：任务存在但还没到能提问的状态，前端该等而非重新提交。
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content=ErrorResponse(
                    reason="analysis_incomplete",
                    message=f"分析尚未完成（当前阶段：{task.stage.value}），暂不能提问",
                ).model_dump(),
            )

        embedding = build_embedding_provider(settings)
        key = build_cache_key(
            repo=_repo_slug(task.repo_url),
            commit_sha=str(task.final_state.get("commit_sha", "")),
            provider_identity=embedding.identity,
        )
        answer = await answer_question(
            question=payload.question,
            cache_key=key,
            index_dir=settings.index_dir,
            embedding=embedding,
            provider=LLMProvider(settings),
        )
        return AnswerResponse(
            question=answer.question,
            found=answer.found,
            answer=answer.answer,
            citations=[
                QaCitationModel(
                    path=c.path,
                    start_line=c.start_line,
                    end_line=c.end_line,
                    symbol=c.symbol,
                    distance=c.distance,
                )
                for c in answer.citations
            ],
            note=answer.note,
        )

    @router.get("/api/health")
    async def health() -> dict[str, object]:
        """健康检查。给出配置摘要但不回显密钥——它会进日志与监控。"""
        return {
            "status": "ok",
            "llm_flash": settings.llm_model_flash,
            "llm_pro": settings.llm_model_pro,
            "embedding_provider": settings.embedding_provider,
            "llm_configured": bool(settings.deepseek_api_key),
        }

    return router


def _result_response(task: AnalysisTask) -> ResultResponse:
    """把内部 state 转成 API 形状。"""
    state = task.final_state
    return ResultResponse(
        task_id=task.task_id,
        repo=task.repo,
        stage=task.stage.value,
        completed=task.completed,
        failed=task.failed,
        error=task.error,
        report=_report_model(state, task.repo),
        review=_review_model(state),
        index=_index_model(state),
        module_failures=[
            f"{f.scope}：{f.error}"
            for f in (state.get("module_failures") or [])
            if getattr(f, "node", "") == "module_agent" and getattr(f, "scope", "")
        ],
    )


def _report_model(state: dict[str, Any], repo: str) -> ReportModel | None:
    report = state.get("report")
    if report is None:
        return None
    return ReportModel(
        repo=getattr(report, "repo", repo),
        commit_sha=getattr(report, "commit_sha", ""),
        summary=getattr(report, "summary", ""),
        sections=[
            ReportSectionModel(
                key=section.key,
                title=section.title,
                claims=[
                    ClaimModel(
                        text=claim.text,
                        citations=[
                            CitationModel(
                                path=c.path, line=c.line, end_line=c.end_line
                            )
                            for c in claim.citations
                        ],
                    )
                    for claim in section.claims
                ],
            )
            for section in report.sections()
        ],
        missing=MissingPartsModel(
            text=report.missing.render(),
            unparsed_files=report.missing.unparsed_files,
            skipped_modules=len(report.missing.skipped_modules),
        ),
        validation_summary=str(state.get("report_validation_summary", "")),
        unsupported_claims=list(state.get("unsupported_claims") or []),
    )


def _review_model(state: dict[str, Any]) -> ReviewModel | None:
    review = state.get("review_report")
    if review is None:
        return None
    return ReviewModel(
        target_files=list(getattr(review, "target_files", ())),
        outcomes=[
            CategoryOutcomeModel(
                category=outcome.category.value,
                status=outcome.status.value,
                scope=outcome.scope,
                hit_count=outcome.hit_count,
                reason=outcome.reason,
            )
            for outcome in review.outcomes
        ],
        findings=[
            FindingModel(
                category=f.category.value,
                kind=f.kind,
                path=f.path,
                line=f.line,
                severity=f.severity.value,
                message=f.message,
                evidence=f.evidence,
            )
            for f in review.all_findings
        ],
    )


def _index_model(state: dict[str, Any]) -> IndexStatusModel | None:
    if "indexed_chunks" not in state:
        return None
    return IndexStatusModel(
        cache_hit=bool(state.get("index_cache_hit", False)),
        chunk_count=int(state.get("indexed_chunks", 0)),
        identity=str(state.get("index_identity", "")),
        note=str(state.get("index_note", "")),
    )
