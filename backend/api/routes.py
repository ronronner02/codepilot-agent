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

from backend.api.export import build_export_router
from backend.api.files import build_files_router
from backend.api.gate import QueueFull
from backend.api.progress import Stage
from backend.api.ratelimit import SubmissionRateLimiter, client_key
from backend.api.search import build_search_router
from backend.api.schemas import (
    AnalysisSummary,
    AnalyzeAccepted,
    AnalyzeRequest,
    AnswerResponse,
    CategoryOutcomeModel,
    CitationModel,
    ClaimModel,
    DependencyEdgeModel,
    DependencyGraphModel,
    ErrorResponse,
    FindingModel,
    IndexStatusModel,
    LanguageProfileModel,
    LlmCredentials,
    MissingPartsModel,
    ModuleModel,
    QaCitationModel,
    QuestionRequest,
    ReportModel,
    ReportSectionModel,
    ResultResponse,
    ReviewModel,
    UnresolvedImportModel,
)
from backend.api.tasks import AnalysisTask, TaskContext, TaskRegistry
from backend.cache.key import build_cache_key
from backend.config import Settings
from backend.history import store as history_store
from backend.history.store import HistoryEntry
from backend.ingest.guards import RejectReason, RepoRejected
from backend.providers.embedding import build_embedding_provider
from backend.providers.llm import GuestCredentials, LLMProvider
from backend.rag.qa import answer_question
from backend.workspace.quota import ensure_repo_quota, quota_usage

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


def _guest_credentials(payload: LlmCredentials | None) -> GuestCredentials | None:
    """把请求里的凭证转成 provider 层的类型。不完整时返回 None。

    完整的判据是 base_url 与 api_key 都非空。只填 key 时用服务端的 base_url 在
    provider 层是合法组合，但在提交端点上不接受——那等于让访客部分借用服务端配置，
    而 BR-004 要的是「访客自带」这件事本身可判定。
    """
    if payload is None:
        return None
    api_key = payload.api_key.strip()
    base_url = payload.base_url.strip()
    if not api_key or not base_url:
        return None
    return GuestCredentials(
        api_key=api_key,
        base_url=base_url,
        model_flash=payload.model_flash.strip(),
        model_pro=payload.model_pro.strip(),
    )


def _rejection_log(reason: str, **extra: object) -> str:
    """一条结构化的拒绝记录（KTD11）。

    reason 是稳定字段，可按它计数；文案不是。四类原因（凭证/限流/准入/闸门）共用这
    一个形态，让「限流是不是在拒真实访客」这类问题有可查询的答案——单机单 worker 的
    规模下日志加 `/api/health` 就是查询路径，不必接指标系统。
    """
    parts = [f"reject reason={reason}"]
    parts.extend(f"{key}={value}" for key, value in extra.items())
    return " ".join(parts)


def _rejection_response(exc: RepoRejected) -> JSONResponse:
    return JSONResponse(
        status_code=_REASON_STATUS.get(exc.reason, status.HTTP_400_BAD_REQUEST),
        content=ErrorResponse(reason=exc.reason.value, message=str(exc)).model_dump(),
    )


def build_router(settings: Settings, registry: TaskRegistry) -> APIRouter:
    router = APIRouter()
    # 限流器与应用同生命周期：它的状态是「哪个 IP 最近提交过几次」，进程内即全局真相
    # （单 worker，KTD6）。每次请求新建一个等于没有限流。
    limiter = SubmissionRateLimiter(
        limit=settings.submissions_per_window,
        window_seconds=settings.submission_window_seconds,
    )

    # 启动时扫描落盘目录重建历史（U21，R-44）。单文件损坏跳过并计数，不拖垮整个列表。
    history = history_store.scan(settings.analyses_dir)

    def persist(task: AnalysisTask) -> None:
        """分析完成时落盘，并把这条并进内存中的历史索引。"""
        result = _result_response(task)
        if history_store.save(settings.analyses_dir, result, created_at=task.created_at):
            history.entries.insert(
                0,
                history_store.HistoryEntry(result=result, created_at=task.created_at),
            )
            # 内存索引有上界。落盘文件不删（KTD10 把清理策略推后了），但这个列表每次分析
            # 都在增长，而它整份进列表端点的响应——长期运行的部署上会同时变成内存增长与
            # 响应体膨胀。取最近 N 条：更早的仍在磁盘上，只是不进这一份索引。
            del history.entries[history_store.MAX_INDEXED_ENTRIES :]

    def _history_entry(task_id: str) -> HistoryEntry | None:
        for entry in history.entries:
            if entry.result.task_id == task_id:
                return entry
        return None

    def resolve_result(task_id: str) -> ResultResponse | None:
        """按 task_id 取结果：先内存，后落盘历史。

        读结果与导出共用它。两处各写一遍的后果是「报告能看但导不出」——同一次分析两种行为，
        而用户无从判断是哪一边坏了。
        """
        task = registry.get(task_id)
        if task is not None:
            return _result_response(task)
        entry = _history_entry(task_id)
        return entry.result if entry is not None else None

    def resolve_context(task_id: str) -> TaskContext | None:
        """按 task_id 取文件与检索端点需要的事实。内存优先，回落到落盘。

        回落存在的理由是 KTD10 的第四态：重启或配额清理之后，分析结果仍可读而工作副本不在。
        没有这条回落，查看器会回答「任务不存在」——而用户此刻正看着那份报告。
        """
        task = registry.get(task_id)
        if task is not None:
            return _context_from_task(task)
        entry = _history_entry(task_id)
        return _context_from_history(entry) if entry is not None else None

    registry.set_persist_hook(persist)
    # 网络类失败退还限流配额：限流器在这个闭包里，注册表只知道「该退了」。
    registry.set_refund_hook(limiter.refund)

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
    async def submit_analysis(payload: AnalyzeRequest, request: Request) -> Any:
        """提交分析。立刻返回 202 与任务标识，分析在后台跑。

        地址非法在这里就返回错误，而不是先接受再让用户拉进度才发现——那会让一个可以
        立即判断的错误延迟几秒才可见。

        **五道门的顺序不可调换**（计划的准入闸门图）：

        1. 凭证——R-68 要求拦在提交阶段，此刻零 clone、零 embedding、零 LLM；
        2. 限流——在建任务之前，被拒不留下任务记录；
        3. 准入（地址解析与规模）——沿用既有拒绝原因；
        4. 配额——克隆之前腾磁盘，AE-22 要求不产生克隆残留；
        5. 闸门——名额满则排队而非拒绝。

        未配置凭证时不消耗限流计数：那一次请求没有产生任何工作，扣配额会让用户配好凭证
        后反被限流拦住。
        """
        # 凭证校验在**建任务之前**（R-68）。此刻 clone 未发生、解析未发生、embedding
        # provider 未构造——AE-23 明确要求 owner 的 embedding 额度消耗为零，而
        # `chunk_and_index` 分支不依赖 LLM，放进去跑就会烧额度。
        credentials = _guest_credentials(payload.credentials)
        if credentials is None:
            logger.info("%s", _rejection_log("credentials_required"))
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=ErrorResponse(
                    reason="credentials_required",
                    message="请先在设置页填写自己的 LLM 凭证（base_url 与 API key）后再提交分析",
                ).model_dump(),
            )

        who = client_key(
            request.headers.get("x-forwarded-for"),
            request.client.host if request.client else None,
        )
        decision = limiter.check(who)
        if not decision.allowed:
            logger.info(
                "%s",
                _rejection_log(
                    "rate_limited",
                    client=who,
                    count=decision.current_count,
                    limit=decision.limit,
                ),
            )
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content=ErrorResponse(
                    reason="rate_limited",
                    message=(
                        f"提交过于频繁：{decision.limit} 次/"
                        f"{settings.submission_window_seconds // 60} 分钟已用尽，"
                        f"请在 {decision.retry_after_seconds} 秒后重试"
                    ),
                ).model_dump(),
                # 标准头。按它重试的客户端不必解析文案。
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )

        try:
            task = registry.create(
                payload.repo_url, credentials=credentials, client_key=who
            )
        except RepoRejected as exc:
            logger.info("%s", _rejection_log(exc.reason.value, client=who))
            return _rejection_response(exc)

        # 配额检查在克隆之前（AE-22：不产生克隆残留）。清理失败不阻止分析——磁盘超一点
        # 比清掉正在分析的副本损失小，那个判断在 quota 模块内。
        quota_outcome = ensure_repo_quota(settings, registry.running_repos())
        if not quota_outcome.admitted:
            # 撤销任务记录：`create` 已经把它放进注册表，而这道门拒绝了它。不撤销会留下
            # 一个永停在 QUEUED 的僵尸任务，它会进历史列表且看起来可恢复（违反 R-46）。
            registry.discard(task)
            logger.warning("%s", _rejection_log("disk_quota", detail=quota_outcome.note))
            return JSONResponse(
                status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                content=ErrorResponse(
                    reason="disk_quota", message=quota_outcome.note
                ).model_dump(),
            )

        try:
            registry.start(task)
        except QueueFull as exc:
            registry.discard(task)
            logger.info("%s", _rejection_log("queue_full", client=who))
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content=ErrorResponse(reason="queue_full", message=str(exc)).model_dump(),
            )

        # 全部门通过、任务已启动或已排队，此刻才计数。
        #
        # 放在最后而非 create 之后：一次因磁盘不足或队列已满而被拒的提交没有产生任何工作，
        # 扣掉它的配额等于让用户为服务端的容量问题付代价。
        limiter.record(who)

        queued = task.queue_position > 0
        return AnalyzeAccepted(
            task_id=task.task_id,
            repo=task.repo,
            stage=task.stage.value,
            queue_position=task.queue_position,
            message=(
                f"已排队，当前第 {task.queue_position} 位，前序分析完成后自动开始"
                if queued
                else "已开始分析，用 /api/analyses/{task_id}/events 拉取进度"
            ),
        )

    @router.get("/api/analyses", response_model=list[AnalysisSummary])
    async def list_analyses() -> Any:
        """最近分析列表（R-45）。

        内存与落盘合并，**内存优先**：同一 task_id 在两处都有时，内存里的是当前进程的实况
        （可能正在跑），落盘的是上一次完成时的快照。origin 的 Source-Of-Truth Resolution 说
        「冲突时以落盘为准」，那针对的是「重启后读到旧内存」的情形——而单进程内不存在那种
        情形，此刻内存才是新的。

        **排队中的任务不进列表**（R-46 的延伸）。它们随进程内存易失，列出来就等于暗示可以
        恢复。已完成与失败的都列——失败也是一次发生过的分析，界面按 failed 区分呈现。
        """
        seen: set[str] = set()
        summaries: list[AnalysisSummary] = []

        for task in registry.list_tasks():
            if task.queued:
                continue
            seen.add(task.task_id)
            summaries.append(_task_summary(task))

        for entry in history.entries:
            if entry.result.task_id in seen:
                continue
            summaries.append(_history_summary(entry))

        summaries.sort(key=lambda s: s.created_at, reverse=True)
        return summaries

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
        result = resolve_result(task_id)
        if result is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=ErrorResponse(
                    reason="task_not_found", message=f"任务不存在：{task_id}"
                ).model_dump(),
            )
        return result

    @router.post(
        "/api/analyses/{task_id}/questions",
        response_model=AnswerResponse,
        responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    )
    async def ask(task_id: str, payload: QuestionRequest) -> Any:
        """对已分析仓库提问（单轮）。

        **重启后落盘的分析仍可提问。** 问答只依赖向量索引，而索引目录不被配额清理、缓存键
        也不含工作副本路径（KTD10）——与检索同理。只读注册表会让一份可读的报告配上一个
        「任务不存在」的问答框。
        """
        context = resolve_context(task_id)
        if context is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=ErrorResponse(
                    reason="task_not_found", message=f"任务不存在：{task_id}"
                ).model_dump(),
            )
        if not context.completed:
            # 409 而非 404：任务存在但还没到能提问的状态，前端该等而非重新提交。
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content=ErrorResponse(
                    reason="analysis_incomplete",
                    message=f"分析尚未完成（当前阶段：{context.stage}），暂不能提问",
                ).model_dump(),
            )

        # 凭证优先取请求体，其次取该任务提交时携带的——刷新页面后再提问仍要能用，而
        # 那时前端可能还没把 localStorage 里的凭证带上。
        #
        # 落盘载入的任务没有 task.credentials（凭证不落盘，NA-02），所以那条路径下凭证
        # 必须来自请求体——前端每次请求都从 localStorage 带上，这是常态而非例外。
        task = registry.get(task_id)
        credentials = _guest_credentials(payload.credentials) or (
            task.credentials if task is not None else None
        )
        if credentials is None or not credentials.api_key:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=ErrorResponse(
                    reason="credentials_required",
                    message="请先在设置页填写自己的 LLM 凭证后再提问",
                ).model_dump(),
            )

        # embedding 用**服务端**配置（R-67、D10）：索引由服务端统一构建，检索侧必须用
        # 同一 provider identity，否则缓存键不匹配。同一请求里 LLM 用访客凭证、
        # embedding 用服务端配置，两者在这里分流。
        embedding = build_embedding_provider(settings)
        key = build_cache_key(
            repo=context.repo_slug,
            commit_sha=context.commit_sha,
            provider_identity=embedding.identity,
        )
        answer = await answer_question(
            question=payload.question,
            cache_key=key,
            index_dir=settings.index_dir,
            embedding=embedding,
            provider=LLMProvider(settings, credentials=credentials),
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
        """健康检查。给出配置摘要但不回显密钥——它会进日志与监控。

        三项资源保护的用量在这里暴露（KTD11）。理由是这三个失败模式**从外部看起来像正常
        运行**：限流把真实访客全拒了、队列积压导致提交后长期无进展、配额贴顶导致反复
        克隆——三者都不产生错误页，健康检查也全绿。没有可见信号，owner 只能等人来反馈。

        不接 Prometheus、不配告警：单机单 worker 的规模下，这个端点加结构化拒绝日志就是
        可查询路径，而 owner 是唯一响应者。
        """
        usage = quota_usage(settings)
        return {
            "status": "ok",
            "llm_flash": settings.llm_model_flash,
            "llm_pro": settings.llm_model_pro,
            "embedding_provider": settings.embedding_provider,
            "llm_configured": bool(settings.deepseek_api_key),
            # 队列是否积压。
            "running_analyses": registry.gate.running_count,
            "queued_analyses": registry.gate.queued_count,
            "max_concurrent_analyses": registry.gate.max_concurrent,
            # 配额是否贴顶。
            "repos_bytes": usage.total_bytes,
            "repos_quota_bytes": usage.quota_bytes,
            "repos_count": usage.repo_count,
            "last_reclaimed_bytes": usage.last_reclaimed_bytes,
            # 限流的阈值。实际拒绝次数按结构化日志的 reason 字段计数。
            "submissions_per_window": settings.submissions_per_window,
            "submission_window_seconds": settings.submission_window_seconds,
        }

    # 文件读取端点独立成模块：它的路径校验与截断语义自成一块，混进本模块会让这里
    # 同时承担「HTTP 编排」与「文件系统边界」两件事。
    router.include_router(build_files_router(resolve_context))
    router.include_router(build_search_router(settings, resolve_context))
    # 导出复用本模块的 _result_response：那是界面消费的同一份数据，传函数进去让
    # 「导出与界面口径一致」（BR-003）不依赖两处实现保持同步。
    router.include_router(build_export_router(registry, resolve_result))

    return router


def _context_from_task(task: AnalysisTask) -> TaskContext:
    return TaskContext(
        task_id=task.task_id,
        repo_slug=task.repo,
        commit_sha=str(task.final_state.get("commit_sha", "")),
        workdir=str(task.final_state.get("workdir", "")).strip(),
        completed=task.completed,
        stage=task.stage.value,
    )


def _context_from_history(entry: HistoryEntry) -> TaskContext:
    """从落盘条目造上下文。

    `workdir` 恒为空——落盘的是 API 形状，其中没有工作副本路径，而重启后那个路径即便记下来
    也可能已经不在了。`from_disk` 让端点把它呈现为「代码副本已清理」而非「还没准备好」。

    `repo_slug` 取 `result.repo`：它就是 `parse_repo_url(...).slug`，与检索侧算缓存键用的
    `_repo_slug(repo_url)` 同值——所以索引仍能按同一个键找到（索引目录不被配额清理，KTD10）。
    """
    result = entry.result
    return TaskContext(
        task_id=result.task_id,
        repo_slug=result.repo,
        commit_sha=result.commit_sha,
        workdir="",
        completed=result.completed,
        stage=result.stage,
        from_disk=True,
    )


def _count_files(state: dict[str, Any]) -> int:
    profile = state.get("language_profile")
    return int(getattr(profile, "total_files", 0)) if profile is not None else 0


def _task_summary(task: AnalysisTask) -> AnalysisSummary:
    state = task.final_state
    review = state.get("review")
    return AnalysisSummary(
        task_id=task.task_id,
        repo=task.repo,
        stage=task.stage.value,
        completed=task.completed,
        failed=task.failed,
        commit_sha=str(state.get("commit_sha", "")),
        created_at=task.created_at,
        file_count=_count_files(state),
        finding_count=len(getattr(review, "findings", []) or []),
    )


def _history_summary(entry: HistoryEntry) -> AnalysisSummary:
    """从落盘条目派生摘要。字段取自 `ResultResponse`，与内存那条同形状。"""
    result = entry.result
    return AnalysisSummary(
        task_id=result.task_id,
        repo=result.repo,
        stage=result.stage,
        completed=result.completed,
        failed=result.failed,
        commit_sha=result.commit_sha,
        created_at=entry.created_at,
        file_count=result.language_profile.total_files if result.language_profile else 0,
        finding_count=len(result.review.findings) if result.review else 0,
    )


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
        queue_position=task.queue_position,
        commit_sha=str(state.get("commit_sha", "")),
        report=_report_model(state, task.repo),
        review=_review_model(state),
        index=_index_model(state),
        modules=_module_models(state),
        dependency_graph=_graph_model(state),
        language_profile=_language_profile_model(state),
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


def _module_models(state: dict[str, Any]) -> list[ModuleModel]:
    """模块清单，并把该模块的分析结论并进来。

    分析可能缺（模块未被 Planner 挑中、子 Agent 失败），那时 summary 为空串而模块本身
    仍要出现——Architecture 页要画出全部模块，缺分析的那些标注为未分析，而不是从图上消失。
    """
    modules = state.get("modules") or []
    analyses = {
        getattr(a, "module_name", ""): a for a in (state.get("module_analyses") or [])
    }
    result: list[ModuleModel] = []
    for module in modules:
        analysis = analyses.get(getattr(module, "name", ""))
        result.append(
            ModuleModel(
                name=module.name,
                files=list(module.files),
                internal_edges=module.internal_edges,
                external_edges=module.external_edges,
                origin=module.origin,
                # 原文直传（R-11）。这里做任何加工，前端就无法声称结论未被改写。
                summary=getattr(analysis, "summary", "") if analysis else "",
                limitation=getattr(analysis, "limitation", "") if analysis else "",
            )
        )
    return result


def _graph_model(state: dict[str, Any]) -> DependencyGraphModel | None:
    graph = state.get("dependency_graph")
    if graph is None:
        return None
    return DependencyGraphModel(
        nodes=list(graph.nodes),
        edges=[
            DependencyEdgeModel(source=source, target=target)
            for source, targets in graph.edges.items()
            for target in sorted(targets)
        ],
        external={
            target: sorted(importers) for target, importers in graph.external.items()
        },
        unresolved=[
            UnresolvedImportModel(
                target=item.target, path=item.path, line=item.line, reason=item.reason
            )
            for item in graph.unresolved
        ],
        cycles=[list(cycle) for cycle in graph.cycles],
        call_time_cycles=[list(cycle) for cycle in graph.call_time_cycles],
        granularity=graph.granularity.value,
        # None 转空串：可选字段在 JSON 里出现两种「没有」（缺键与 null）会让前端多写
        # 一个分支，而这里的语义只有「有原因」与「没原因」。
        degraded_reason=graph.degraded_reason or "",
    )


def _language_profile_model(state: dict[str, Any]) -> LanguageProfileModel | None:
    profile = state.get("language_profile")
    if profile is None:
        return None
    return LanguageProfileModel(
        total_files=profile.total_files,
        parseable_files=profile.parseable_files,
        by_language=dict(profile.by_language),
    )
