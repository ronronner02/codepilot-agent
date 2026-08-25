"""集中式配置。在应用启动时构造一次并注入，各模块不重复读环境变量。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    deepseek_api_key: str = Field(min_length=1)
    deepseek_base_url: str = "https://api.deepseek.com"
    llm_model_flash: str = "deepseek-v4-flash"
    llm_model_pro: str = "deepseek-v4-pro"

    embedding_provider: Literal["local", "api"] = "local"
    embedding_local_model: str = "jinaai/jina-embeddings-v2-base-code"
    embedding_api_key: str = ""
    embedding_api_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_api_model: str = "text-embedding-v3"

    workspace_root: Path = Path("./.workspace")

    # 双门限。主门是可解析文件数（Python + TypeScript）——它直接对应解析与
    # embedding 的成本；副门是 git 对象体积，粗略拦住即使浅克隆也过大的仓库。
    # 实测参照：click 79 个可解析文件 / 5MB；fastapi 1138 / 55MB；
    # django 2929 / 282MB。总文件数与可解析文件数差异很大（fastapi 共 3139 个
    # 文件，其余是测试固件、文档、翻译），所以按总数设限会误拒有效基准仓库。
    max_parseable_files: int = 1500
    max_repo_size_kb: int = 300_000

    max_file_bytes: int = 1_048_576
    max_fanout_width: int = 12

    # 依赖图的节点数上限。超出则降级为目录级分组并标注（不静默失败也不耗尽资源）。
    # 取值与 max_parseable_files 对齐留出余量：准入门限是 1500 个可解析文件，
    # 而图的节点是实际解析成功的文件，两者同阶。上限在这里的作用是兜住准入被放宽或
    # 直接调用解析层（MCP 工具、离线脚本）时的病态输入。
    max_graph_nodes: int = 2_000

    # 同时在途的 LLM 请求数上限。
    #
    # KTD16 说「DeepSeek 的并发上限（flash 2500、pro 500）在本项目规模下不是约束」，
    # 那是按直连估的。实测经中转网关时 **3 路并发就触发 Cloudflare 524（上游超时）与
    # `Server disconnected without sending a response`**，四次重试全撞在饱和的网关上，
    # 模块分析随之失败。
    #
    # 取 2 是保守值：扇出宽度上限仍是 12（那是成本约束），但同时在途的请求由这里限住。
    # 换成直连或更稳的端点时可调高——它与扇出宽度是两个独立的旋钮，混为一谈会让「降低
    # 成本」和「避开网关限制」互相干扰。
    llm_max_concurrency: int = 2

    # 克隆深度。设 1 会让提交频次信号恒为空（KTD12）；实测中型仓库设 200 可拿到
    # 上千提交、体积仅增约 2M。
    clone_depth: int = 200

    # 可选。未认证时 GitHub API 速率为 60 次/小时，且私有仓库与不存在的仓库同样
    # 返回 404——带 token 才能区分（403 vs 404），速率也提到 5000 次/小时。
    github_token: str = ""

    # 允许跨域的前端源。开发期 Vite 在 5173，容器部署时前端由 nginx 在 80 提供。
    #
    # 不用通配符：这个 API 能触发克隆仓库与 LLM 调用（有成本），通配会让任意站点都能
    # 消耗这些资源。部署到公网时应改成实际域名。
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost",
    ]

    langsmith_api_key: str = ""
    langsmith_project: str = "codepilot-agent"
    langsmith_tracing: bool = False

    @field_validator("embedding_api_key")
    @classmethod
    def _api_key_required_for_api_provider(cls, v: str, info: object) -> str:
        # provider 为 api 时缺 key 应尽早失败，而不是等到第一次向量化才报错。
        data = getattr(info, "data", {})
        if data.get("embedding_provider") == "api" and not v:
            raise ValueError(
                "EMBEDDING_PROVIDER=api 需要 EMBEDDING_API_KEY，当前为空"
            )
        return v

    @property
    def repos_dir(self) -> Path:
        return self.workspace_root / "repos"

    @property
    def index_dir(self) -> Path:
        return self.workspace_root / "index"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
