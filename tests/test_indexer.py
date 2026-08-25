"""索引构建与缓存命中（R4、AE3）。

**核心断言是 embedding 调用次数为 0。** AE3 要求二次提交同一仓库不重复产生 embedding
成本，而「秒级返回」不足以证明这一点——那可能只是因为切块快。调用次数是确定的证据。

用假 provider 记录调用次数，用真 Chroma 落盘：前者是要断言的对象，后者的持久化行为
是要验证的（跨进程复用是缓存的全部意义）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.cache.key import build_cache_key
from backend.cache.manager import check_cache, find_previous_keys, write_manifest
from backend.rag.indexer import build_index
from backend.rag.store import VectorStore


class _CountingProvider:
    """记录调用次数与收到的文本。维度固定，向量按内容哈希生成——同内容同向量，
    让检索结果可预期。
    """

    def __init__(self, dimension: int = 8) -> None:
        self.calls = 0
        self.texts: list[str] = []
        self._dimension = dimension

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.texts.extend(texts)
        return [self._vector(t) for t in texts]

    def _vector(self, text: str) -> list[float]:
        # 简单确定性映射：按字符位置累加，保证同文本同向量。
        vector = [0.0] * self._dimension
        for index, char in enumerate(text):
            vector[index % self._dimension] += (ord(char) % 17) / 17.0
        norm = sum(v * v for v in vector) ** 0.5 or 1.0
        return [v / norm for v in vector]

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def identity(self) -> str:
        return "fake:test-model:v1"


class _FailingProvider(_CountingProvider):
    def __init__(self, fail_on_call: int = 1) -> None:
        super().__init__()
        self._fail_on_call = fail_on_call

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls == self._fail_on_call:
            raise RuntimeError("向量化服务不可用")
        self.texts.extend(texts)
        return [self._vector(t) for t in texts]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "auth.py").write_text(
        "def verify_token(token):\n"
        "    if not token:\n"
        "        raise ValueError('missing token')\n"
        "    return decode(token)\n",
        encoding="utf-8",
    )
    (root / "pkg" / "billing.py").write_text(
        "def charge(amount):\n    return gateway.post(amount)\n", encoding="utf-8"
    )
    (root / ".env").write_text("API_KEY=xQ7fL2mZ9pR4tK8wB3nH\n", encoding="utf-8")
    return root


def _paths() -> list[str]:
    return ["pkg/auth.py", "pkg/billing.py", ".env"]


def _key(commit: str = "a" * 40, identity: str = "fake:test-model:v1"):  # type: ignore[no-untyped-def]
    return build_cache_key("acme/widget", commit, identity)


class TestFirstIndex:
    async def test_builds_and_reports_counts(self, repo: Path, tmp_path: Path) -> None:
        provider = _CountingProvider()
        result = await build_index(
            repo, _paths(), _key(), provider, tmp_path / "index", 1_048_576
        )
        assert result.cache_hit is False
        assert result.chunk_count > 0
        assert result.embedding_calls >= 1
        assert result.is_complete

    async def test_secret_file_not_indexed(self, repo: Path, tmp_path: Path) -> None:
        """KTD17：索引是落盘的，密钥写进去就可能被问答链路检索出来。"""
        provider = _CountingProvider()
        await build_index(repo, _paths(), _key(), provider, tmp_path / "index", 1_048_576)
        assert not any("xQ7fL2mZ9pR4tK8wB3nH" in t for t in provider.texts)
        assert not any(".env" in t.splitlines()[0] for t in provider.texts)

    async def test_embedding_text_includes_path_and_symbol(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """查询常带路径词（「auth 模块怎么校验 token」里的 auth 只在路径上）。

        不加定位信息的话这类查询只能靠代码内容匹配，命中率明显更低。
        """
        provider = _CountingProvider()
        await build_index(repo, _paths(), _key(), provider, tmp_path / "index", 1_048_576)
        assert any(t.startswith("pkg/auth.py") for t in provider.texts)
        assert any("verify_token" in t.splitlines()[0] for t in provider.texts)

    async def test_batching_respected(self, repo: Path, tmp_path: Path) -> None:
        provider = _CountingProvider()
        result = await build_index(
            repo, _paths(), _key(), provider, tmp_path / "index", 1_048_576, batch_size=1
        )
        assert provider.calls == result.chunk_count

    async def test_no_chunks_reports_reason(self, tmp_path: Path) -> None:
        root = tmp_path / "empty"
        root.mkdir()
        (root / "README.md").write_text("# hi\n", encoding="utf-8")
        provider = _CountingProvider()
        result = await build_index(
            root, ["README.md"], _key(), provider, tmp_path / "index", 1_048_576
        )
        assert result.chunk_count == 0
        assert provider.calls == 0
        assert "没有可索引的片段" in result.note


class TestCacheHit:
    async def test_second_run_makes_zero_embedding_calls(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """AE3 的核心断言。「秒级返回」不足以证明省了成本——调用次数才是。"""
        index_dir = tmp_path / "index"
        first_provider = _CountingProvider()
        first = await build_index(
            repo, _paths(), _key(), first_provider, index_dir, 1_048_576
        )
        assert first_provider.calls > 0

        second_provider = _CountingProvider()
        second = await build_index(
            repo, _paths(), _key(), second_provider, index_dir, 1_048_576
        )
        assert second.cache_hit is True
        assert second_provider.calls == 0, "缓存命中时不该有任何 embedding 调用"
        assert second.chunk_count == first.chunk_count

    async def test_hit_does_not_rechunk(self, repo: Path, tmp_path: Path) -> None:
        """命中路径连切块都不做——切块要读全部文件，那也是未命中才付的成本。"""
        index_dir = tmp_path / "index"
        await build_index(repo, _paths(), _key(), _CountingProvider(), index_dir, 1_048_576)

        # 删掉源文件后仍应命中：证明命中路径没有读文件。
        (repo / "pkg" / "auth.py").unlink()
        provider = _CountingProvider()
        result = await build_index(repo, _paths(), _key(), provider, index_dir, 1_048_576)
        assert result.cache_hit is True
        assert provider.calls == 0

    async def test_force_rebuilds_despite_existing(self, repo: Path, tmp_path: Path) -> None:
        index_dir = tmp_path / "index"
        await build_index(repo, _paths(), _key(), _CountingProvider(), index_dir, 1_048_576)
        provider = _CountingProvider()
        result = await build_index(
            repo, _paths(), _key(), provider, index_dir, 1_048_576, force=True
        )
        assert result.cache_hit is False
        assert provider.calls > 0


class TestCacheInvalidation:
    async def test_provider_change_misses(self, repo: Path, tmp_path: Path) -> None:
        """切换 provider 后向量空间不同，复用旧索引会让检索质量静默变差。"""
        index_dir = tmp_path / "index"
        await build_index(repo, _paths(), _key(), _CountingProvider(), index_dir, 1_048_576)

        provider = _CountingProvider()
        result = await build_index(
            repo,
            _paths(),
            _key(identity="other:model-b:v1"),
            provider,
            index_dir,
            1_048_576,
        )
        assert result.cache_hit is False
        assert provider.calls > 0

    async def test_commit_change_misses(self, repo: Path, tmp_path: Path) -> None:
        index_dir = tmp_path / "index"
        await build_index(repo, _paths(), _key(), _CountingProvider(), index_dir, 1_048_576)
        result = await build_index(
            repo, _paths(), _key(commit="b" * 40), _CountingProvider(), index_dir, 1_048_576
        )
        assert result.cache_hit is False

    async def test_old_index_still_on_disk_after_switch(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """旧索引不删：commit 回退或 provider 切回时能立刻命中。"""
        index_dir = tmp_path / "index"
        original = _key()
        await build_index(repo, _paths(), original, _CountingProvider(), index_dir, 1_048_576)
        await build_index(
            repo, _paths(), _key(commit="b" * 40), _CountingProvider(), index_dir, 1_048_576
        )

        provider = _CountingProvider()
        back = await build_index(repo, _paths(), original, provider, index_dir, 1_048_576)
        assert back.cache_hit is True
        assert provider.calls == 0


class TestPartialFailure:
    async def test_failed_batch_recorded_and_index_marked_incomplete(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """不完整的索引会让检索漏内容而不报错，必须在产出里说清。"""
        provider = _FailingProvider(fail_on_call=1)
        result = await build_index(
            repo, _paths(), _key(), provider, tmp_path / "index", 1_048_576, batch_size=1
        )
        assert result.failed_batches
        assert result.is_complete is False
        assert "索引不完整" in result.note

    async def test_successful_batches_still_stored(self, repo: Path, tmp_path: Path) -> None:
        """单批失败不该丢弃已完成的批次——重跑只需补未完成的部分。"""
        provider = _FailingProvider(fail_on_call=1)
        result = await build_index(
            repo, _paths(), _key(), provider, tmp_path / "index", 1_048_576, batch_size=1
        )
        assert result.chunk_count > 0

    async def test_vector_count_mismatch_recorded(self, repo: Path, tmp_path: Path) -> None:
        """返回向量数与片段数不符时不能写入——错位会让元数据指向错误的代码。"""

        class _Mismatched(_CountingProvider):
            async def embed_texts(self, texts: list[str]) -> list[list[float]]:
                self.calls += 1
                return [self._vector(texts[0])]  # 只返回一个

        result = await build_index(
            repo, _paths(), _key(), _Mismatched(), tmp_path / "index", 1_048_576, batch_size=4
        )
        assert any("不符" in f for f in result.failed_batches)


class TestSearch:
    async def test_search_returns_relevant_chunk(self, repo: Path, tmp_path: Path) -> None:
        index_dir = tmp_path / "index"
        provider = _CountingProvider()
        key = _key()
        await build_index(repo, _paths(), key, provider, index_dir, 1_048_576)

        store = VectorStore(index_dir, key.digest)
        query = (await provider.embed_texts(["pkg/auth.py · verify_token"]))[0]
        hits = store.search(query, limit=3)
        assert hits
        assert hits[0].path == "pkg/auth.py"

    async def test_hit_carries_citation(self, repo: Path, tmp_path: Path) -> None:
        """问答要给出引用（R7 的可追溯延伸到问答链路）。"""
        index_dir = tmp_path / "index"
        provider = _CountingProvider()
        key = _key()
        await build_index(repo, _paths(), key, provider, index_dir, 1_048_576)

        store = VectorStore(index_dir, key.digest)
        hits = store.search((await provider.embed_texts(["verify_token"]))[0], limit=1)
        assert hits[0].citation.startswith("pkg/")
        assert ":" in hits[0].citation

    def test_search_on_missing_collection_returns_empty(self, tmp_path: Path) -> None:
        store = VectorStore(tmp_path / "index", "deadbeefdeadbeef")
        assert store.search([0.1] * 8) == []

    def test_exists_false_for_empty_collection(self, tmp_path: Path) -> None:
        """中断的索引可能留下空集合。把它当命中会让检索永远返回空，
        而那看起来像「没有相关代码」而非「索引没建成」。
        """
        store = VectorStore(tmp_path / "index", "cafebabecafebabe")
        assert store.exists() is False


class TestCacheManager:
    def test_first_time_reports_first_index(self, tmp_path: Path) -> None:
        status = check_cache(tmp_path / "index", _key(), collection_exists=False)
        assert status.hit is False
        assert "首次索引" in status.reason
        assert status.needs_embedding is True

    def test_hit_reports_skip(self, tmp_path: Path) -> None:
        status = check_cache(tmp_path / "index", _key(), collection_exists=True, chunk_count=42)
        assert status.hit is True
        assert "跳过解析与向量化" in status.reason
        assert status.needs_embedding is False
        assert status.chunk_count == 42

    def test_miss_explains_provider_change(self, tmp_path: Path) -> None:
        """「未命中，因为 provider 变了」比「未命中」有用得多——前者告诉用户要重付成本。"""
        index_dir = tmp_path / "index"
        old = _key()
        write_manifest(index_dir, old, chunk_count=10)

        status = check_cache(
            index_dir, _key(identity="other:model-b:v1"), collection_exists=False
        )
        assert status.hit is False
        assert "embedding provider" in status.reason
        assert status.previous_key == old

    def test_miss_explains_commit_change(self, tmp_path: Path) -> None:
        index_dir = tmp_path / "index"
        write_manifest(index_dir, _key(), chunk_count=10)
        status = check_cache(index_dir, _key(commit="b" * 40), collection_exists=False)
        assert "commit" in status.reason

    def test_manifest_roundtrip(self, tmp_path: Path) -> None:
        index_dir = tmp_path / "index"
        key = _key()
        write_manifest(index_dir, key, chunk_count=7)
        assert find_previous_keys(index_dir, "acme/widget") == [key]

    def test_other_repo_manifests_ignored(self, tmp_path: Path) -> None:
        index_dir = tmp_path / "index"
        write_manifest(index_dir, build_cache_key("other/repo", "c" * 40, "x:y:v1"), 3)
        assert find_previous_keys(index_dir, "acme/widget") == []

    def test_corrupt_manifest_tolerated(self, tmp_path: Path) -> None:
        index_dir = tmp_path / "index"
        index_dir.mkdir(parents=True)
        (index_dir / "bad-index-manifest.json").write_text("{不是 JSON", encoding="utf-8")
        assert find_previous_keys(index_dir, "acme/widget") == []

    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        assert find_previous_keys(tmp_path / "nope", "acme/widget") == []
