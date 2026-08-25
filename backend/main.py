"""应用入口。

CORS 对前端开放：R24 要求前后端分离，而分离部署下前端的源与后端不同（开发期 Vite 在
5173，后端在 8000）。允许的源从配置读而非写死通配——通配会让任意站点能调用这个 API，
而它能触发克隆仓库与 LLM 调用（有成本）。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.routes import build_router
from backend.api.tasks import TaskRegistry
from backend.config import Settings, get_settings
from backend.graph.observability import configure_tracing


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造应用。settings 可注入，让测试不依赖 .env。"""
    resolved = settings or get_settings()
    registry = TaskRegistry(resolved)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    traced = configure_tracing(resolved)
    logging.getLogger("codepilot").info(
        "启动：flash=%s pro=%s embedding=%s trace=%s",
        resolved.llm_model_flash,
        resolved.llm_model_pro,
        resolved.embedding_provider,
        "on" if traced else "off",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        # 关闭时取消未完成的分析，避免留下悬挂协程与半写的索引。
        await registry.shutdown()

    app = FastAPI(
        title="CodePilot-Agent API",
        description="多 Agent 代码分析：架构报告、单轮问答、三类检查评审",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    app.include_router(build_router(resolved, registry))
    # 暴露给测试与运维：能拿到 registry 才能断言任务状态。
    app.state.registry = registry
    app.state.settings = resolved
    return app


_app: FastAPI | None = None


def get_app() -> FastAPI:
    """uvicorn 的入口。惰性构造，避免导入模块时就读配置——那会让任何 import 都要求
    .env 存在，而测试与工具脚本不该有这个前提。
    """
    global _app
    if _app is None:
        _app = create_app()
    return _app
