"""代码搜索端点（U4）。

**与 MCP 的 `search_code` 各自独立收口，不互相调用。** 两侧的判定顺序相同（查询非空 →
算缓存键 → 判断集合存在 → 检索），但产出形态不同：MCP 返回给模型看的文本，HTTP 返回
结构化字段（路径、行范围、符号名、片段、距离）。让 HTTP 端点去调 MCP 工具函数就得把
文本再解析回结构，比各写一遍更绕且更脆。

**判定顺序里有一处是成本约束而非风格：集合存在性检查必须在向量化之前。** 反过来的话，
没建索引的仓库每次检索都先付一次 embedding 的钱，而结果注定是「未建索引」。

embedding 用服务端配置而非访客凭证（R-67、D10）。索引由服务端统一构建，检索侧必须用
同一 provider identity——否则缓存键不匹配，每次检索都报「未建索引」。这也是设置页不
暴露 embedding 配置项的原因：暴露它会让索引键分裂。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from backend.api.schemas import (
    ErrorResponse,
    SearchHitModel,
    SearchRequest,
    SearchResponse,
)
from backend.cache.key import build_cache_key
from backend.config import Settings
from backend.providers.embedding import build_embedding_provider
from backend.rag.store import VectorStore

logger = logging.getLogger("codepilot.api.search")

# 返回条数的上下限。与 MCP 侧同值：同一份检索能力开在两个边界上，条数语义不该不同。
MIN_LIMIT = 1
MAX_LIMIT = 20


def _error(reason: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(reason=reason, message=message).model_dump(),
    )


def build_search_router(settings: Settings, resolve_context) -> APIRouter:  # type: ignore[no-untyped-def]
    router = APIRouter()

    @router.post(
        "/api/analyses/{task_id}/search",
        response_model=SearchResponse,
        responses={
            400: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
        },
    )
    async def search_code(task_id: str, payload: SearchRequest) -> object:
        """按语义检索该仓库的代码片段。

        **从落盘历史载入的任务同样可检索。** 配额清理只删仓库工作副本，索引目录不动
        （KTD10），而缓存键是 `repo slug + commit_sha + provider identity`，不含工作副本
        路径——所以重启后索引仍能按同一个键找到。这与查看器不同：查看器要读源文件，检索
        只要索引。
        """
        context = resolve_context(task_id)
        if context is None:
            return _error(
                "task_not_found", f"任务不存在：{task_id}", status.HTTP_404_NOT_FOUND
            )
        if not context.completed:
            # 409 而非 404：任务存在但索引还没建好，前端该等而非重新提交。
            return _error(
                "analysis_incomplete",
                f"分析尚未完成（当前阶段：{context.stage}），索引未就绪",
                status.HTTP_409_CONFLICT,
            )

        cleaned = payload.query.strip()
        if not cleaned:
            # pydantic 的 min_length 挡住空串，纯空白要在这里挡——它长度非零但语义为空。
            return _error(
                "empty_query", "查询为空", status.HTTP_400_BAD_REQUEST
            )

        embedding = build_embedding_provider(settings)
        # repo_slug 已是 parse_repo_url(...).slug，与 _repo_slug(repo_url) 同值——
        # 内存与落盘两条来源因此算出同一个键。
        key = build_cache_key(
            repo=context.repo_slug,
            commit_sha=context.commit_sha,
            provider_identity=embedding.identity,
        )
        store = VectorStore(settings.index_dir, key.digest)
        if not store.exists():
            # 「没索引」与「没找到」在这里分道（AE-08）。provider identity 变更导致的键
            # 不匹配也落在这条路上——集合按键定位，键变了就找不到。
            return _error(
                "index_missing",
                f"{context.repo_slug} 尚未建立向量索引（{key.describe()}），无法语义检索。"
                f"若已分析过，可能是 embedding provider 变更导致索引键不匹配",
                status.HTTP_409_CONFLICT,
            )

        limit = max(MIN_LIMIT, min(payload.limit, MAX_LIMIT))
        try:
            vector = (await embedding.embed_texts([cleaned]))[0]
        except Exception as exc:  # noqa: BLE001 — 失败要说清，不返回空结果冒充「未找到」
            logger.warning("检索向量化失败：%s: %s", type(exc).__name__, exc)
            return _error(
                "embedding_failed",
                f"查询向量化失败（{type(exc).__name__}: {exc}）",
                status.HTTP_502_BAD_GATEWAY,
            )

        hits = store.search(vector, limit=limit)
        if not hits:
            return SearchResponse(
                query=cleaned,
                found=False,
                reason="no_match",
                note="未检索到相关代码片段。索引已建立，可以换个更具体的问法。",
            )

        return SearchResponse(
            query=cleaned,
            found=True,
            hits=[
                SearchHitModel(
                    path=hit.path,
                    start_line=hit.start_line,
                    end_line=hit.end_line,
                    symbol=hit.symbol,
                    content=hit.content,
                    distance=hit.distance,
                )
                for hit in hits
            ],
        )

    return router
