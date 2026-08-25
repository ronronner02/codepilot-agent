"""embedding provider 抽象（KTD4）。本地与 API 两种实现，由配置切换。

`identity` 进入 U9 的缓存键。两家的向量维度不同，切换 provider 后复用旧索引会
静默污染检索结果，所以 identity 的格式一旦定下就不能随意改——格式变动等于全量
缓存失效。
"""

from __future__ import annotations

from typing import Protocol

from openai import AsyncOpenAI

from backend.config import Settings

# 向量化前的预处理版本。改动预处理逻辑（是否加符号名前缀、是否带文件路径上下文
# 等）时手动递增——这类改动会改变向量空间却不改模型名，不递增就会命中语义已经
# 不一致的旧索引。切块策略的版本由 U9 的缓存键单独持有，与此无关。
EMBEDDING_PIPELINE_VERSION = "v1"


class EmbeddingProvider(Protocol):
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量向量化。返回顺序与输入一一对应。"""
        ...

    @property
    def dimension(self) -> int:
        """向量维度。必须与 embed_texts 实际返回的长度一致。"""
        ...

    @property
    def identity(self) -> str:
        """provider 与模型的稳定标识，进入缓存键。跨进程必须一致。

        TODO(U1 核心逻辑)：由你实现 identity 的构成规则。见下方设计说明。
        """
        ...


class LocalEmbeddingProvider:
    """本地模型。首次调用时惰性加载，避免未使用时也付出加载成本。"""

    def __init__(self, settings: Settings) -> None:
        self._model_name = settings.embedding_local_model
        self._model: object | None = None

    def _ensure_model(self) -> object:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_name)
        return self._model

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure_model()
        vectors = model.encode(texts, convert_to_numpy=True)  # type: ignore[attr-defined]
        return [v.tolist() for v in vectors]

    @property
    def dimension(self) -> int:
        model = self._ensure_model()
        return int(model.get_sentence_embedding_dimension())  # type: ignore[attr-defined]

    @property
    def identity(self) -> str:
        # 不读 dimension——那会触发模型加载，而 identity 在缓存键计算时就要用到，
        # 那时还不知道是否真的需要向量化。
        return f"local:{self._model_name}:{EMBEDDING_PIPELINE_VERSION}"


class ApiEmbeddingProvider:
    """OpenAI 兼容的 embedding 端点。"""

    # 维度登记表。端点不提供维度查询接口，故显式登记，新增模型时在此补。
    #
    # **值必须是对目标端点的实测结果，不是模型的标称规格。** 实测教训：
    # `qwen3-embedding-8b` 标称 4096 维，而经由当前网关实际返回 768 维——网关可能换了
    # 底层模型或做了降维。按标称值登记会让 Chroma 集合以错误维度创建，插入时才报错。
    #
    # 登记表的作用是 fail-fast：未登记的模型在配置阶段就报错，好过向量化一千个切块
    # 之后才发现维度不符。校验实测一致性见 verify_dimension。
    _KNOWN_DIMENSIONS = {
        "text-embedding-v3": 1024,
        "text-embedding-v2": 1536,
        "text-embedding-3-small": 1536,
        # 经 api.futureppo.top 实测为 768，与模型标称的 4096 不同。
        "qwen3-embedding-8b": 768,
    }

    def __init__(self, settings: Settings) -> None:
        self._model_name = settings.embedding_api_model
        self._client = AsyncOpenAI(
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_api_base_url,
        )

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        response = await self._client.embeddings.create(
            model=self._model_name, input=texts
        )
        return [item.embedding for item in response.data]

    @property
    def dimension(self) -> int:
        if self._model_name not in self._KNOWN_DIMENSIONS:
            raise ValueError(
                f"未登记的 embedding 模型维度：{self._model_name}。"
                f"在 _KNOWN_DIMENSIONS 中补充后再使用——"
                f"值取对目标端点的实测结果，不要用模型标称规格。"
            )
        return self._KNOWN_DIMENSIONS[self._model_name]

    async def verify_dimension(self) -> int:
        """实测一次并与登记值比对。不一致则抛错，返回实测维度。

        为什么需要它：登记值是手工维护的，而端点行为可能变（网关换底层模型、模型
        升级）。维度不符的后果是索引与查询向量空间不一致——检索结果会静默变差而不
        报错，这类问题极难定位。在建立索引前校验一次，成本是一次 embedding 调用。
        """
        vectors = await self.embed_texts(["dimension probe"])
        actual = len(vectors[0])
        declared = self.dimension
        if actual != declared:
            raise ValueError(
                f"embedding 维度不符：{self._model_name} 登记为 {declared}，"
                f"端点 {self._client.base_url} 实际返回 {actual}。"
                f"更新 _KNOWN_DIMENSIONS 后再建索引——维度不符会让检索静默失准。"
            )
        return actual

    @property
    def identity(self) -> str:
        # 前缀区分本地与 API：同名模型的向量空间不保证一致。
        return f"api:{self._model_name}:{EMBEDDING_PIPELINE_VERSION}"


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    if settings.embedding_provider == "local":
        return LocalEmbeddingProvider(settings)
    return ApiEmbeddingProvider(settings)
