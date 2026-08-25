"""索引缓存键（KTD4）。

**四个组成部分，缺一个都会导致错误命中。** 计划点名这是本单元最容易埋的坑，具体是漏掉
provider 标识：切换 embedding provider 后向量空间完全不同，而缓存键若不含 provider，
旧索引会被当成有效命中——检索仍然返回结果，只是结果基于另一个向量空间，质量静默变差
而不报错。这类失效极难定位，因为没有任何错误信号。

  仓库标识    owner/name。不同仓库当然不能共用索引。
  commit SHA  代码变了，切块与向量都得重算。
  provider    provider 名 + 模型名 + 预处理版本，由 provider.identity 给出。
  切块版本     切块策略改了（粒度、重叠量、超长函数处理），块边界就变了。

四者拼成一个稳定的短哈希做目录名。用哈希而非拼接原文的理由：commit SHA 加模型名加仓库
名很容易超出文件名长度限制，而模型名里的 `/`（如 `jinaai/jina-embeddings-v2`）会被当成
路径分隔符。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

# 切块策略版本。**改动切块逻辑时必须递增**——粒度、重叠量、超长函数的处理方式、
# 元数据字段，任一变化都会让新旧块不可比。不递增的后果是二次分析命中语义已经不一致的
# 旧索引，而这不会报错。
CHUNK_STRATEGY_VERSION = "v1"

# 哈希取前多少位做目录名。
#
# 取 16 位（64 bit）的依据：本项目的键空间是「仓库 × commit × provider × 策略版本」，
# 量级在数千以内，64 bit 的碰撞概率可忽略。取全长 64 位会让路径过长，在 Windows 上
# 接近 260 字符限制。
_DIGEST_LENGTH = 16


@dataclass(frozen=True)
class CacheKey:
    """索引缓存的标识。

    各组成部分单独保留而非只存哈希：排查「为什么这次没命中」时需要能逐项对比，
    只有哈希的话只能看出「不一样」，看不出是哪一项变了。
    """

    repo: str
    commit_sha: str
    provider_identity: str
    chunk_version: str = CHUNK_STRATEGY_VERSION

    @property
    def digest(self) -> str:
        """稳定的短哈希。跨进程一致——用 sha256 而非内置 hash()。

        内置 `hash()` 对 str 加了进程级随机盐（PYTHONHASHSEED），跨进程不一致。
        用它做缓存键会让每次重启都全量未命中，而这在单进程测试里发现不了。
        """
        material = "\x00".join(
            (self.repo, self.commit_sha, self.provider_identity, self.chunk_version)
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]

    def describe(self) -> str:
        """人可读的键说明。缓存未命中时写进日志，便于对比是哪一项变了。"""
        return (
            f"repo={self.repo} commit={self.commit_sha[:12]} "
            f"provider={self.provider_identity} chunks={self.chunk_version}"
        )

    def differences(self, other: CacheKey) -> list[str]:
        """列出与另一个键的差异项。

        存在的理由：「未命中」本身没有信息量，而「provider 变了」与「commit 变了」对
        用户的含义完全不同——前者意味着要重新付 embedding 成本，后者是正常的代码更新。
        """
        diffs: list[str] = []
        if self.repo != other.repo:
            diffs.append(f"仓库：{other.repo} -> {self.repo}")
        if self.commit_sha != other.commit_sha:
            diffs.append(f"commit：{other.commit_sha[:12]} -> {self.commit_sha[:12]}")
        if self.provider_identity != other.provider_identity:
            diffs.append(
                f"embedding provider：{other.provider_identity} -> {self.provider_identity}"
            )
        if self.chunk_version != other.chunk_version:
            diffs.append(f"切块策略版本：{other.chunk_version} -> {self.chunk_version}")
        return diffs


def build_cache_key(repo: str, commit_sha: str, provider_identity: str) -> CacheKey:
    """构造缓存键。切块版本由模块常量给出，不作为参数——它是代码属性而非运行时输入。"""
    return CacheKey(
        repo=repo, commit_sha=commit_sha, provider_identity=provider_identity
    )
