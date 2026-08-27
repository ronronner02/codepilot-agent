"""索引缓存键（KTD4）。

计划点名「缓存键漏掉 provider 标识是本单元最容易埋的坑」，所以本文件的重点是四个组成
部分**每一个**都能让键变化。漏掉任一项的后果都是错误命中，而错误命中不报错——检索仍然
返回结果，只是基于过期或不匹配的索引，质量静默变差。
"""

from __future__ import annotations

import subprocess
import sys

from backend.cache.key import CHUNK_STRATEGY_VERSION, CacheKey, build_cache_key


def _key(**overrides: str) -> CacheKey:
    base = {
        "repo": "acme/widget",
        "commit_sha": "a" * 40,
        "provider_identity": "api:text-embedding-v3:v1",
    }
    base.update(overrides)
    return build_cache_key(**base)  # type: ignore[arg-type]


class TestKeyStability:
    def test_same_inputs_same_digest(self) -> None:
        assert _key().digest == _key().digest

    def test_digest_stable_across_processes(self) -> None:
        """内置 hash() 对 str 加了进程级随机盐，用它做缓存键会让每次重启全量未命中。

        这个缺陷在单进程测试里发现不了，所以这条测试真的起子进程。
        """
        code = (
            "from backend.cache.key import build_cache_key; "
            "print(build_cache_key('acme/widget', 'a'*40, 'api:text-embedding-v3:v1').digest)"
        )
        first = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        ).stdout.strip()
        second = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        ).stdout.strip()
        assert first == second
        assert first == _key().digest

    def test_digest_is_filesystem_safe(self) -> None:
        """模型名里的 `/`（如 jinaai/jina-embeddings-v2）会被当成路径分隔符。"""
        digest = _key(provider_identity="local:jinaai/jina-embeddings-v2-base-code:v1").digest
        assert "/" not in digest
        assert "\\" not in digest
        assert digest.isalnum()

    def test_digest_length_bounded(self) -> None:
        """全长哈希会让路径在 Windows 上接近 260 字符限制。"""
        assert len(_key().digest) == 16


class TestKeyInvalidation:
    def test_different_repo_changes_key(self) -> None:
        assert _key().digest != _key(repo="other/repo").digest

    def test_different_commit_changes_key(self) -> None:
        assert _key().digest != _key(commit_sha="b" * 40).digest

    def test_different_provider_changes_key(self) -> None:
        """切换 embedding provider 后向量空间完全不同，旧索引不可复用。

        这是本单元最容易埋的坑：漏掉 provider 后旧索引会被当成有效命中，检索基于
        另一个向量空间，质量静默变差而无任何错误信号。
        """
        assert _key().digest != _key(provider_identity="local:jina-v2:v1").digest

    def test_same_model_different_provider_changes_key(self) -> None:
        """同名模型的本地与 API 实现不保证同一向量空间。"""
        local = _key(provider_identity="local:model-a:v1")
        api = _key(provider_identity="api:model-a:v1")
        assert local.digest != api.digest

    def test_chunk_version_changes_key(self) -> None:
        """切块策略改了块边界就变了，旧索引与新切块不可比。"""
        current = _key()
        bumped = CacheKey(
            repo=current.repo,
            commit_sha=current.commit_sha,
            provider_identity=current.provider_identity,
            chunk_version="v2",
        )
        assert current.digest != bumped.digest

    def test_component_boundaries_do_not_collide(self) -> None:
        """拼接时必须有分隔符，否则 ('ab','c') 与 ('a','bc') 会产出同一个键。"""
        left = CacheKey(repo="ab", commit_sha="c", provider_identity="p")
        right = CacheKey(repo="a", commit_sha="bc", provider_identity="p")
        assert left.digest != right.digest


class TestIndexScope:
    """索引覆盖范围。

    抽样索引与全量索引在检索时表现相同——都返回结果，只是抽样那份漏内容且不报错。
    所以它必须进键，否则一次抽样分析之后的全量分析会命中残缺索引。
    """

    def test_sampled_scope_changes_key(self) -> None:
        assert _key().digest != _key(index_scope="top200").digest

    def test_different_sample_sizes_differ(self) -> None:
        """抽 200 与抽 500 的索引内容不同，不能互相命中。"""
        assert _key(index_scope="top200").digest != _key(index_scope="top500").digest

    def test_full_index_digest_unchanged_by_new_field(self) -> None:
        """全量索引的 digest 必须与引入该字段之前一致，否则升级即让已建索引全失效。

        写死期望值而非与「四段拼接」的重算对比：重算会跟着实现一起变，钉不住这条约束。
        这个值取自加字段之前的实现输出（四段拼接后取 sha256 前 16 位）。
        """
        assert _key().digest == "f6924a78a1842a65"

    def test_empty_scope_equals_omitted_scope(self) -> None:
        assert _key(index_scope="").digest == _key().digest

    def test_describe_shows_scope_only_when_sampled(self) -> None:
        assert "scope" not in _key().describe()
        assert "scope=top200" in _key(index_scope="top200").describe()

    def test_differences_reports_scope_change(self) -> None:
        diffs = _key(index_scope="top200").differences(_key())
        assert len(diffs) == 1
        assert "索引范围" in diffs[0]
        assert "全量" in diffs[0]


class TestDiagnostics:
    def test_describe_lists_all_components(self) -> None:
        """排查「为什么没命中」时要能逐项对比，只有哈希看不出是哪项变了。"""
        described = _key().describe()
        assert "acme/widget" in described
        assert "api:text-embedding-v3:v1" in described
        assert CHUNK_STRATEGY_VERSION in described

    def test_differences_names_the_changed_component(self) -> None:
        """「provider 变了」与「commit 变了」对用户含义不同：前者要重付 embedding 成本。"""
        old = _key()
        new = _key(provider_identity="local:jina-v2:v1")
        diffs = new.differences(old)
        assert len(diffs) == 1
        assert "embedding provider" in diffs[0]

    def test_differences_reports_commit_change(self) -> None:
        diffs = _key(commit_sha="b" * 40).differences(_key())
        assert len(diffs) == 1
        assert "commit" in diffs[0]

    def test_differences_empty_for_identical_keys(self) -> None:
        assert _key().differences(_key()) == []

    def test_differences_reports_multiple_changes(self) -> None:
        diffs = _key(repo="x/y", commit_sha="c" * 40).differences(_key())
        assert len(diffs) == 2

    def test_chunk_version_is_declared_not_parameterised(self) -> None:
        """切块版本是代码属性而非运行时输入——让它可传会让调用方有机会传错。"""
        assert build_cache_key("r", "c", "p").chunk_version == CHUNK_STRATEGY_VERSION
