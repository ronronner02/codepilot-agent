"""索引构建。切块 -> 向量化 -> 入库。

**缓存命中时一次 embedding 调用都不发（R4、AE3）。** 这是本单元的验收点，也是唯一能
证明缓存真的省了成本的判据——「秒级返回」可能只是因为切块快，而 embedding 调用次数为 0
是确定的。所以命中检查在最前面，且检查本身不需要 embedding。

向量化分批：一次传上千个片段会撞 API 的请求体上限，也让单次失败的代价过大。批内失败
不丢整批——已成功的批次已经落盘，重跑只需补未完成的部分。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from backend.cache.key import CacheKey
from backend.providers.embedding import EmbeddingProvider
from backend.rag.chunker import Chunk, chunk_repo
from backend.rag.store import VectorStore

logger = logging.getLogger("codepilot.indexer")

# 单批向量化的片段数。
#
# 取 64 的依据：多数 embedding API 的单请求上限在 100-2048 个输入之间，64 留足余量；
# 而批次太小会让 HTTP 往返次数成为瓶颈（千个片段就是十几次请求 vs 上百次）。
DEFAULT_BATCH_SIZE = 64


@dataclass
class IndexResult:
    """索引结果。

    cache_hit 与 embedding_calls 都保留：前者是结论，后者是证据。AE3 要求「不重复产生
    embedding 成本」，而只报 cache_hit 无法证明这一点——调用次数才是。
    """

    cache_hit: bool
    chunk_count: int
    embedding_calls: int = 0
    identity: str = ""
    skipped_files: int = 0
    failed_batches: tuple[str, ...] = ()
    note: str = ""

    @property
    def is_complete(self) -> bool:
        """索引是否完整。有失败批次即不完整——检索会漏内容而不报错。"""
        return not self.failed_batches


async def build_index(
    repo_root: Path,
    rel_paths: list[str],
    cache_key: CacheKey,
    provider: EmbeddingProvider,
    index_dir: Path,
    max_file_bytes: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
    force: bool = False,
) -> IndexResult:
    """建立或复用索引。

    force 为真时忽略已有索引重建——切块逻辑改了但忘了递增版本号时的补救手段，
    不应是常规路径。
    """
    store = VectorStore(index_dir, cache_key.digest)

    if not force and store.exists():
        # 命中路径不调 embedding，也不切块——这两项是未命中时才付的成本。
        count = store.count()
        logger.info("索引缓存命中 %s（%d 个片段）", cache_key.describe(), count)
        return IndexResult(
            cache_hit=True,
            chunk_count=count,
            embedding_calls=0,
            identity=cache_key.provider_identity,
            note=f"复用已有索引（{cache_key.describe()}）",
        )

    chunks = chunk_repo(repo_root, rel_paths, max_file_bytes)
    if not chunks:
        return IndexResult(
            cache_hit=False,
            chunk_count=0,
            embedding_calls=0,
            identity=cache_key.provider_identity,
            skipped_files=len(rel_paths),
            note="没有可索引的片段（文件均不可解析、为空或属密钥形态）",
        )

    calls = 0
    failures: list[str] = []
    indexed = 0

    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        try:
            vectors = await provider.embed_texts([_embedding_text(c) for c in batch])
            calls += 1
        except Exception as exc:  # noqa: BLE001 — 单批失败不该丢弃已完成的批次
            failures.append(f"片段 {start}-{start + len(batch) - 1}：{type(exc).__name__}: {exc}")
            logger.warning("向量化批次失败（%d-%d）：%s", start, start + len(batch) - 1, exc)
            continue

        if len(vectors) != len(batch):
            failures.append(
                f"片段 {start}-{start + len(batch) - 1}：返回向量数 {len(vectors)} "
                f"与片段数 {len(batch)} 不符"
            )
            continue

        store.add(batch, vectors)
        indexed += len(batch)

    note = f"新建索引（{cache_key.describe()}）"
    if failures:
        # 不完整的索引会让检索漏内容而不报错，必须在产出里说清。
        note += f"；{len(failures)} 个批次失败，索引不完整"

    return IndexResult(
        cache_hit=False,
        chunk_count=indexed,
        embedding_calls=calls,
        identity=cache_key.provider_identity,
        failed_batches=tuple(failures),
        note=note,
    )


def _embedding_text(chunk: Chunk) -> str:
    """送去向量化的文本。

    在代码前加一行定位信息（路径与符号名），理由是查询往往带这类词：「auth 模块怎么
    校验 token」里的 `auth` 可能只出现在路径上而不在代码里。不加的话这类查询只能靠代码
    内容匹配，命中率明显更低。

    改动这里的拼接方式必须递增 EMBEDDING_PIPELINE_VERSION——它改变了向量空间却不改模型
    名，不递增就会命中语义已不一致的旧索引。
    """
    header = chunk.path
    if chunk.symbol:
        header += f" · {chunk.symbol}"
    return f"{header}\n{chunk.content}"
