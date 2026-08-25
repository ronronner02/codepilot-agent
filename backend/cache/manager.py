"""缓存状态查询与说明（R4、R5、AE3）。

这一层的职责是回答「这次会不会命中，为什么」，并把答案说成人能读的话。

**为什么需要它而不是让调用方直接看 store.exists()：** R5 要求缓存未命中时界面呈现进度，
而「要不要显示进度条」取决于是否命中；AE3 要求二次提交秒级返回。两者都需要在开工前就
知道命中状态，且需要能向用户解释——「未命中，因为 embedding provider 从 X 变成了 Y」
比「未命中」有用得多，前者告诉用户这次要重新付 embedding 成本。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from backend.cache.key import CacheKey

logger = logging.getLogger("codepilot.cache")

# 记录上次索引所用键的文件名。放在索引目录下，与向量库同生共死。
_MANIFEST_NAME = "index-manifest.json"


@dataclass(frozen=True)
class CacheStatus:
    """缓存命中判断及其依据。"""

    hit: bool
    key: CacheKey
    reason: str
    previous_key: CacheKey | None = None
    chunk_count: int = 0

    @property
    def needs_embedding(self) -> bool:
        """本次是否要付 embedding 成本。界面据此决定是否显示进度（R5）。"""
        return not self.hit


def _manifest_path(index_dir: Path, digest: str) -> Path:
    return index_dir / f"{digest}-{_MANIFEST_NAME}"


def write_manifest(index_dir: Path, key: CacheKey, chunk_count: int) -> None:
    """记录本次索引所用的键。

    为什么要单独存一份而不只依赖集合是否存在：集合存在只能回答「命中吗」，回答不了
    「上次用的是什么 provider」。而后者是解释未命中原因的唯一依据——没有它，用户看到的
    就只是「未命中」三个字。
    """
    index_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "repo": key.repo,
        "commit_sha": key.commit_sha,
        "provider_identity": key.provider_identity,
        "chunk_version": key.chunk_version,
        "chunk_count": chunk_count,
    }
    try:
        _manifest_path(index_dir, key.digest).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError as exc:
        # 写不成不影响索引可用性，只影响未命中原因的可解释性。
        logger.warning("写索引清单失败：%s", exc)


def read_manifest(index_dir: Path, digest: str) -> CacheKey | None:
    path = _manifest_path(index_dir, digest)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return CacheKey(
            repo=str(payload["repo"]),
            commit_sha=str(payload["commit_sha"]),
            provider_identity=str(payload["provider_identity"]),
            chunk_version=str(payload["chunk_version"]),
        )
    except KeyError:
        return None


def find_previous_keys(index_dir: Path, repo: str) -> list[CacheKey]:
    """找同一仓库此前用过的键。

    用于解释未命中原因：当前键不命中时，同仓库的历史键能告出是哪一项变了。按 commit
    与 provider 都可能变，逐个对比比只看最近一次更可靠。
    """
    if not index_dir.is_dir():
        return []

    found: list[CacheKey] = []
    for path in sorted(index_dir.glob(f"*-{_MANIFEST_NAME}")):
        digest = path.name.removesuffix(f"-{_MANIFEST_NAME}")
        key = read_manifest(index_dir, digest)
        if key is not None and key.repo == repo:
            found.append(key)
    return found


def check_cache(
    index_dir: Path, key: CacheKey, collection_exists: bool, chunk_count: int = 0
) -> CacheStatus:
    """判断命中并给出可读的原因。

    collection_exists 由调用方从 VectorStore 传入，而不是在这里建 Chroma 客户端——
    那会让缓存检查依赖向量库的初始化成本，而这条路径是最频繁的。
    """
    if collection_exists:
        return CacheStatus(
            hit=True,
            key=key,
            reason=f"命中已有索引（{key.describe()}），跳过解析与向量化",
            chunk_count=chunk_count,
        )

    previous = find_previous_keys(index_dir, key.repo)
    if not previous:
        return CacheStatus(
            hit=False,
            key=key,
            reason=f"该仓库首次索引（{key.describe()}），需完整解析与向量化",
        )

    # 取差异最少的历史键作对比对象——它最可能是「上一次」，差异也最能说明问题。
    closest = min(previous, key=lambda p: len(key.differences(p)))
    diffs = key.differences(closest)
    return CacheStatus(
        hit=False,
        key=key,
        previous_key=closest,
        reason=(
            "缓存未命中，因以下变化：" + "；".join(diffs)
            if diffs
            else "缓存未命中：索引清单存在但向量集合缺失（上次索引可能中断）"
        ),
    )
