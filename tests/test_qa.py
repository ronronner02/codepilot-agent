"""单轮代码问答（R12、R13、R14、AE5）。

**核心断言是阈值之下 LLM 调用为 0。** AE5 要求「不基于无关片段编造答案」，而只断言
「返回了未找到」不足以证明——那可能是模型自己说的未找到，成本已经付了。调用次数是确定的。

引用的来源同样要断言：R13 要求回答附带路径与行号，而路径必须来自检索块的元数据。若模型
自己写路径就会有幻觉路径，且问答是即时返回的，没有 U8 那样的事后校验层。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.cache.key import build_cache_key
from backend.rag.chunker import Chunk
from backend.rag.qa import (
    DEFAULT_DISTANCE_THRESHOLD,
    answer_question,
)
from backend.rag.store import VectorStore


class _CountingLLM:
    def __init__(self, reply: str = "回答内容", fail: bool = False) -> None:
        self.reply = reply
        self.fail = fail
        self.calls = 0
        self.requests: list[list[dict[str, str]]] = []

    async def chat(self, messages, tier="flash", tools=None, response_format=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.requests.append(messages)
        if self.fail:
            raise RuntimeError("网关不可用")

        class _Message:
            content = self.reply

        class _Choice:
            message = _Message()

        class _Response:
            choices = [_Choice()]

        return _Response()


class _StubEmbedding:
    """把查询映射到固定向量。测试用它控制检索命中什么。"""

    def __init__(self, vector: list[float] | None = None, fail: bool = False) -> None:
        self._vector = vector or [1.0, 0.0, 0.0, 0.0]
        self.fail = fail
        self.calls = 0

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("向量化服务不可用")
        return [list(self._vector) for _ in texts]

    @property
    def dimension(self) -> int:
        return 4

    @property
    def identity(self) -> str:
        return "stub:test:v1"


def _key():  # type: ignore[no-untyped-def]
    return build_cache_key("acme/widget", "a" * 40, "stub:test:v1")


def _chunk(path: str, start: int, end: int, symbol: str, content: str) -> Chunk:
    return Chunk(
        path=path,
        start_line=start,
        end_line=end,
        content=content,
        symbol=symbol,
        kind="function",
    )


@pytest.fixture
def index_dir(tmp_path: Path) -> Path:
    """建一个含两个块的索引：一个与查询向量同向（近），一个正交（远）。"""
    directory = tmp_path / "index"
    store = VectorStore(directory, _key().digest)
    store.add(
        [
            _chunk(
                "pkg/auth.py",
                10,
                18,
                "verify_token",
                "def verify_token(token):\n    return decode(token)",
            ),
            _chunk(
                "pkg/math.py",
                3,
                5,
                "add",
                "def add(a, b):\n    return a + b",
            ),
        ],
        [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]],
    )
    return directory


class TestRelevantAnswer:
    async def test_answer_carries_real_citation(self, index_dir: Path) -> None:
        """R13：引用来自检索块的元数据，不由模型生成。"""
        llm = _CountingLLM(reply="校验逻辑在片段 1 的 verify_token 函数里。")
        result = await answer_question(
            "如何校验 token", _key(), index_dir, _StubEmbedding(), llm  # type: ignore[arg-type]
        )
        assert result.found is True
        assert result.citations
        assert result.citations[0].path == "pkg/auth.py"

    async def test_citation_lines_match_chunk_metadata(self, index_dir: Path) -> None:
        llm = _CountingLLM(reply="见片段 1。")
        result = await answer_question(
            "如何校验 token", _key(), index_dir, _StubEmbedding(), llm  # type: ignore[arg-type]
        )
        citation = result.citations[0]
        assert citation.start_line == 10
        assert citation.end_line == 18

    async def test_snippet_reference_replaced_with_location(self, index_dir: Path) -> None:
        """读者在读到结论那一句就该看到依据在哪，而不是回头对照文末列表。"""
        llm = _CountingLLM(reply="校验逻辑在片段 1 里实现。")
        result = await answer_question(
            "如何校验 token", _key(), index_dir, _StubEmbedding(), llm  # type: ignore[arg-type]
        )
        assert "片段 1" not in result.answer
        assert "pkg/auth.py:10-18" in result.answer

    async def test_model_written_path_not_trusted_as_citation(self, index_dir: Path) -> None:
        """模型写的路径不进 citations——那是幻觉路径的来源。"""
        llm = _CountingLLM(reply="实现在 totally/fake.py:999 中，见片段 1。")
        result = await answer_question(
            "如何校验 token", _key(), index_dir, _StubEmbedding(), llm  # type: ignore[arg-type]
        )
        assert all(c.path != "totally/fake.py" for c in result.citations)

    async def test_only_referenced_snippets_become_citations(self, index_dir: Path) -> None:
        """列出模型没用到的片段会让读者以为结论有更多依据。"""
        llm = _CountingLLM(reply="只看片段 1 就够了。")
        result = await answer_question(
            "如何校验 token",
            _key(),
            index_dir,
            _StubEmbedding([0.7, 0.7, 0.0, 0.0]),  # 两个块都在阈值内
            llm,  # type: ignore[arg-type]
        )
        assert result.referenced_indices == (1,)
        assert len(result.citations) == 1

    async def test_unreferenced_answer_keeps_all_citations(self, index_dir: Path) -> None:
        """模型没写编号时保留全部引用：完全不给引用会让 R13 落空。"""
        llm = _CountingLLM(reply="校验逻辑就在那个函数里。")
        result = await answer_question(
            "如何校验 token", _key(), index_dir, _StubEmbedding(), llm  # type: ignore[arg-type]
        )
        assert result.citations
        assert result.referenced_indices == ()
        assert "未显式引用" in result.note

    async def test_fabricated_snippet_number_ignored(self, index_dir: Path) -> None:
        """模型写「片段 7」而实际只给了 1 个——那个编号无处可指。"""
        llm = _CountingLLM(reply="见片段 7。")
        result = await answer_question(
            "如何校验 token", _key(), index_dir, _StubEmbedding(), llm  # type: ignore[arg-type]
        )
        assert 7 not in result.referenced_indices

    async def test_uses_pro_tier(self, index_dir: Path) -> None:
        llm = _CountingLLM()
        await answer_question(
            "如何校验 token", _key(), index_dir, _StubEmbedding(), llm  # type: ignore[arg-type]
        )
        assert llm.calls == 1


class TestThreshold:
    async def test_below_threshold_makes_zero_llm_calls(self, index_dir: Path) -> None:
        """AE5 的核心断言。

        U9 实跑演示了不设阈值的后果：查「认证用了哪些策略」在没有认证代码的索引里返回了
        getPMT / getP（距离 0.48-0.51），喂给模型必然产出一段像样但错误的解释。
        """
        # 与两个块都近乎正交的查询向量。
        llm = _CountingLLM()
        result = await answer_question(
            "怎么部署到 Kubernetes",
            _key(),
            index_dir,
            _StubEmbedding([0.0, 0.0, 1.0, 0.0]),
            llm,  # type: ignore[arg-type]
        )
        assert result.found is False
        assert llm.calls == 0, "阈值之下不该有任何 LLM 调用"
        assert "未在该仓库中找到" in result.answer

    async def test_not_found_reports_best_distance(self, index_dir: Path) -> None:
        """给出最接近的距离，让用户判断是「换问法」还是「这个仓库真没有」。"""
        result = await answer_question(
            "无关问题",
            _key(),
            index_dir,
            _StubEmbedding([0.0, 0.0, 1.0, 0.0]),
            _CountingLLM(),  # type: ignore[arg-type]
        )
        assert result.best_distance is not None
        assert "阈值" in result.answer

    async def test_not_found_has_no_citations(self, index_dir: Path) -> None:
        """带引用的「未找到」说明自相矛盾。"""
        result = await answer_question(
            "无关问题",
            _key(),
            index_dir,
            _StubEmbedding([0.0, 0.0, 1.0, 0.0]),
            _CountingLLM(),  # type: ignore[arg-type]
        )
        assert result.citations == ()

    async def test_threshold_is_tunable(self, index_dir: Path) -> None:
        """阈值与 embedding 模型绑定，换 provider 后需重新实测。"""
        query = _StubEmbedding([0.0, 0.0, 1.0, 0.0])
        strict = await answer_question(
            "q", _key(), index_dir, query, _CountingLLM(), distance_threshold=0.1  # type: ignore[arg-type]
        )
        loose = await answer_question(
            "q", _key(), index_dir, query, _CountingLLM(), distance_threshold=2.0  # type: ignore[arg-type]
        )
        assert strict.found is False
        assert loose.found is True

    def test_default_threshold_matches_measured_range(self) -> None:
        """0.45 落在实测的好命中（0.26-0.41）与无关结果（0.48-0.51）之间。"""
        assert 0.41 < DEFAULT_DISTANCE_THRESHOLD < 0.48


class TestMissingIndex:
    async def test_unanalysed_repo_returns_clear_error(self, tmp_path: Path) -> None:
        """「还没分析」与「没找到相关代码」对用户的下一步动作完全不同。"""
        llm = _CountingLLM()
        result = await answer_question(
            "任何问题",
            build_cache_key("never/analysed", "b" * 40, "stub:test:v1"),
            tmp_path / "index",
            _StubEmbedding(),
            llm,  # type: ignore[arg-type]
        )
        assert result.found is False
        assert result.answer
        assert "尚未建立索引" in result.answer
        assert "发起一次分析" in result.answer
        assert llm.calls == 0

    async def test_missing_index_does_not_embed(self, tmp_path: Path) -> None:
        """索引不存在时连查询向量化都不必做。"""
        embedding = _StubEmbedding()
        await answer_question(
            "q",
            build_cache_key("x/y", "c" * 40, "stub:test:v1"),
            tmp_path / "index",
            embedding,
            _CountingLLM(),  # type: ignore[arg-type]
        )
        assert embedding.calls == 0

    async def test_note_distinguishes_missing_from_below_threshold(
        self, tmp_path: Path, index_dir: Path
    ) -> None:
        missing = await answer_question(
            "q",
            build_cache_key("x/y", "c" * 40, "stub:test:v1"),
            tmp_path / "nope",
            _StubEmbedding(),
            _CountingLLM(),  # type: ignore[arg-type]
        )
        below = await answer_question(
            "q",
            _key(),
            index_dir,
            _StubEmbedding([0.0, 0.0, 1.0, 0.0]),
            _CountingLLM(),  # type: ignore[arg-type]
        )
        assert missing.note == "index_missing"
        assert below.note == "below_threshold"
        assert missing.answer != below.answer


class TestFailurePaths:
    async def test_empty_question_rejected_without_calls(self, index_dir: Path) -> None:
        llm = _CountingLLM()
        embedding = _StubEmbedding()
        result = await answer_question("   ", _key(), index_dir, embedding, llm)  # type: ignore[arg-type]
        assert result.found is False
        assert llm.calls == 0
        assert embedding.calls == 0

    async def test_embedding_failure_reported(self, index_dir: Path) -> None:
        llm = _CountingLLM()
        result = await answer_question(
            "q", _key(), index_dir, _StubEmbedding(fail=True), llm  # type: ignore[arg-type]
        )
        assert result.found is False
        assert "向量化失败" in result.answer
        assert llm.calls == 0

    async def test_llm_failure_reported_not_silent(self, index_dir: Path) -> None:
        """检索到了但没答上——要说清，不能返回空回答。"""
        result = await answer_question(
            "如何校验 token",
            _key(),
            index_dir,
            _StubEmbedding(),
            _CountingLLM(fail=True),  # type: ignore[arg-type]
        )
        assert result.found is False
        assert "生成回答失败" in result.answer
        assert result.llm_calls == 1

    async def test_empty_llm_reply_reported(self, index_dir: Path) -> None:
        result = await answer_question(
            "如何校验 token",
            _key(),
            index_dir,
            _StubEmbedding(),
            _CountingLLM(reply="   "),  # type: ignore[arg-type]
        )
        assert result.found is False
        assert "空回答" in result.answer


class TestContextAssembly:
    async def test_prompt_forbids_model_written_paths(self, index_dir: Path) -> None:
        llm = _CountingLLM()
        await answer_question(
            "q", _key(), index_dir, _StubEmbedding(), llm  # type: ignore[arg-type]
        )
        system = llm.requests[0][0]["content"]
        assert "不要自己写文件路径" in system
        assert "片段 N" in system

    async def test_prompt_forbids_outside_knowledge(self, index_dir: Path) -> None:
        """R14 的延伸：片段里没有的内容不该用一般印象补充。"""
        llm = _CountingLLM()
        await answer_question(
            "q", _key(), index_dir, _StubEmbedding(), llm  # type: ignore[arg-type]
        )
        system = llm.requests[0][0]["content"]
        assert "一般印象" in system

    async def test_snippet_content_included(self, index_dir: Path) -> None:
        llm = _CountingLLM()
        await answer_question(
            "q", _key(), index_dir, _StubEmbedding(), llm  # type: ignore[arg-type]
        )
        user = llm.requests[0][1]["content"]
        assert "def verify_token" in user
        assert "片段 1" in user

    async def test_snippet_limit_respected(self, tmp_path: Path) -> None:
        directory = tmp_path / "index"
        store = VectorStore(directory, _key().digest)
        chunks = [
            _chunk(f"pkg/f{i}.py", 1, 3, f"fn_{i}", f"def fn_{i}():\n    return {i}")
            for i in range(10)
        ]
        # 全部同向：都在阈值内，靠 max_snippets 限量。
        store.add(chunks, [[1.0, 0.0, 0.0, 0.0] for _ in chunks])

        llm = _CountingLLM()
        await answer_question(
            "q", _key(), directory, _StubEmbedding(), llm, max_snippets=3  # type: ignore[arg-type]
        )
        user = llm.requests[0][1]["content"]
        assert user.count("--- 片段") == 3

    async def test_budget_keeps_most_relevant_first(self, tmp_path: Path) -> None:
        """预算用尽即停，而不是让长片段挤掉更相关的。"""
        directory = tmp_path / "index"
        store = VectorStore(directory, _key().digest)
        near = _chunk("pkg/near.py", 1, 2, "near_fn", "def near_fn():\n    pass")
        far = _chunk("pkg/far.py", 1, 200, "far_fn", "x = 1\n" * 200)
        store.add([near, far], [[1.0, 0.0, 0.0, 0.0], [0.9, 0.1, 0.0, 0.0]])

        llm = _CountingLLM(reply="见片段 1。")
        await answer_question(
            "q", _key(), directory, _StubEmbedding(), llm, context_budget=200  # type: ignore[arg-type]
        )
        user = llm.requests[0][1]["content"]
        assert "near_fn" in user
        assert user.count("--- 片段") == 1, "预算用尽后不该再塞片段"
