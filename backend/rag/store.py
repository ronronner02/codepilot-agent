"""向量库封装（KTD5：Chroma 本地持久化）。

Chroma 而非 pgvector/Milvus 的理由见 KTD5：无需独立服务进程，落盘即可复用，与单容器
部署兼容。

**这一层不碰 embedding。** 向量由调用方算好传入，store 只管存取。这样切换 embedding
provider 不影响存储层，而 store 的测试也不需要真实的 embedding 调用。

集合名用缓存键的哈希：Chroma 要求名字为 3-512 个 `[a-zA-Z0-9._-]` 字符（实测确认，
不合规直接抛 InvalidArgumentError），而 16 位十六进制哈希天然符合。这也是缓存键用哈希
而非拼接原文的另一个理由——原文含 `/` 与 `:`，两者都不合规。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.rag.chunker import Chunk

logger = logging.getLogger("codepilot.store")

# 集合名前缀。纯哈希开头可能是数字，而 Chroma 要求首尾字符为 [a-zA-Z0-9]——数字合规，
# 但加前缀让目录内容一眼可辨，也留出日后区分索引类型的余地。
_COLLECTION_PREFIX = "idx_"

# 相似度空间。cosine 而非默认的 l2：代码向量的模长受片段长度影响，而「长度相近」不是
# 语义相关的信号。cosine 只看方向。
_SPACE = "cosine"


@dataclass(frozen=True)
class SearchHit:
    """一条检索结果。

    带完整定位信息（路径 + 行号）：问答链路要给出引用（R7 的可追溯要求延伸到问答），
    只返回文本片段的话调用方无从说明它来自哪里。
    """

    chunk_id: str
    path: str
    start_line: int
    end_line: int
    content: str
    symbol: str
    distance: float

    @property
    def citation(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"


def _chunk_metadata(chunk: Chunk) -> dict[str, Any]:
    """块的元数据。

    Chroma 的元数据值只接受标量（str/int/float/bool），不接受 None 与容器——所以空值
    存空串而非 None，否则 add 时报类型错误。
    """
    return {
        "path": chunk.path,
        "start_line": chunk.start_line,
        "end_line": chunk.end_line,
        "symbol": chunk.symbol,
        "parent": chunk.parent,
        "kind": chunk.kind,
        "part_index": chunk.part_index,
    }


class VectorStore:
    """一个缓存键对应的向量集合。

    Chroma 客户端按目录持久化，多个缓存键共用同一目录下的不同集合——这让「切换 provider
    后旧索引仍在磁盘上」成为可能，而那正是 commit 回退或 provider 切回时能立刻命中的
    前提。清理旧集合是运维决定，不在这一层做。
    """

    def __init__(self, index_dir: Path, digest: str) -> None:
        self._index_dir = index_dir
        self._collection_name = f"{_COLLECTION_PREFIX}{digest}"
        self._client: Any | None = None

    def _ensure_client(self) -> Any:
        """惰性建客户端。

        Chroma 的 PersistentClient 会创建目录并初始化 sqlite，代价不小；而缓存命中检查
        只需要知道集合是否存在，未命中时才真的要写入。惰性化让「检查是否命中」这条最
        频繁的路径不必付出初始化成本。
        """
        if self._client is None:
            import chromadb
            from chromadb.config import Settings as ChromaSettings

            self._index_dir.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(
                path=str(self._index_dir),
                # 关掉匿名遥测。两个理由：
                #
                # 代码分析工具不该外发任何东西——即便只是使用统计，在分析私有仓库的场景下
                # 也是不该有的行为。
                #
                # 实测：遥测在 MCP server 子进程里会让客户端初始化挂住（独立进程 3.5 秒，
                # 子进程内 >120 秒超时）。它在启动时尝试联网上报，而那条路径在受限的
                # 子进程环境下不返回。
                settings=ChromaSettings(anonymized_telemetry=False),
            )
        return self._client

    def exists(self) -> bool:
        """集合是否已存在且非空。

        非空是必要条件：中断的索引过程可能留下空集合，把它当成命中会让检索永远返回空
        结果，而那看起来像「这个仓库没有相关代码」而非「索引没建成」。
        """
        try:
            client = self._ensure_client()
            collection = client.get_collection(self._collection_name)
            return int(collection.count()) > 0
        except Exception:  # noqa: BLE001 — 集合不存在时 Chroma 抛 NotFoundError
            return False

    def count(self) -> int:
        try:
            client = self._ensure_client()
            return int(client.get_collection(self._collection_name).count())
        except Exception:  # noqa: BLE001
            return 0

    def add(self, chunks: list[Chunk], embeddings: list[list[float]]) -> None:
        """写入块与向量。数量必须一致。"""
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"块数 {len(chunks)} 与向量数 {len(embeddings)} 不一致，"
                "写入会让元数据与向量错位"
            )
        if not chunks:
            return

        client = self._ensure_client()
        collection = client.get_or_create_collection(
            name=self._collection_name, metadata={"hnsw:space": _SPACE}
        )
        collection.add(
            ids=[chunk.identifier() for chunk in chunks],
            embeddings=embeddings,
            documents=[chunk.content for chunk in chunks],
            metadatas=[_chunk_metadata(chunk) for chunk in chunks],
        )

    def search(self, query_embedding: list[float], limit: int = 8) -> list[SearchHit]:
        """按向量检索。集合不存在时返回空列表而非抛错。

        返回空与「集合不存在」由调用方通过 exists() 区分——两者对用户的含义不同：
        前者是「没有相关代码」，后者是「索引没建」。
        """
        try:
            client = self._ensure_client()
            collection = client.get_collection(self._collection_name)
        except Exception:  # noqa: BLE001
            return []

        total = int(collection.count())
        if total == 0:
            return []

        result = collection.query(
            query_embeddings=[query_embedding],
            # n_results 超过集合规模时 Chroma 仍返回，但显式收敛更清晰。
            n_results=min(limit, total),
        )

        hits: list[SearchHit] = []
        ids = result.get("ids") or [[]]
        documents = result.get("documents") or [[]]
        metadatas = result.get("metadatas") or [[]]
        distances = result.get("distances") or [[]]

        for index, chunk_id in enumerate(ids[0]):
            meta = metadatas[0][index] if metadatas[0] else {}
            hits.append(
                SearchHit(
                    chunk_id=str(chunk_id),
                    path=str(meta.get("path", "")),
                    start_line=int(meta.get("start_line", 0)),
                    end_line=int(meta.get("end_line", 0)),
                    content=str(documents[0][index]) if documents[0] else "",
                    symbol=str(meta.get("symbol", "")),
                    distance=float(distances[0][index]) if distances[0] else 0.0,
                )
            )
        return hits

    def drop(self) -> None:
        """删除集合。切块策略变更后清理旧索引时用。"""
        try:
            client = self._ensure_client()
            client.delete_collection(self._collection_name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("删除集合 %s 失败（可能本就不存在）：%s", self._collection_name, exc)
