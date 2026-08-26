"""代码搜索端点（U4：R-24、R-25、R-26）。

**「未建索引」与「未找到」必须是两个 reason。** AE-08 明确两者不得混同：前者的下一步是
「先跑一次分析」，后者是「换个问法」。用空结果表达「没索引」会让界面把两件事说成一件。

embedding 用服务端凭证而非访客凭证（R-67、D10）：索引由服务端统一构建，检索侧必须用
同一 provider identity，否则缓存键不匹配、每次检索都报「未建索引」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.api.progress import Stage
from backend.api.tasks import AnalysisTask
from backend.main import create_app
from backend.rag.store import SearchHit
from tests.support import make_settings

REPO_URL = "https://github.com/acme/widget"


class _FakeEmbedding:
    """受控 embedding。identity 固定，向量化次数可数。"""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self._fail = fail

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self._fail:
            raise RuntimeError("端点拒绝了向量化请求")
        return [[0.1, 0.2, 0.3] for _ in texts]

    @property
    def dimension(self) -> int:
        return 3

    @property
    def identity(self) -> str:
        return "fake:model:v1"


def _hit(index: int = 0) -> SearchHit:
    return SearchHit(
        chunk_id=f"c{index}",
        path="pkg/auth.py",
        start_line=10 + index,
        end_line=18 + index,
        content="def verify_token(raw: str) -> Claims: ...",
        symbol="verify_token",
        distance=0.21 + index / 100,
    )


class _FakeStore:
    """受控向量库。exists 与 search 的行为由构造参数决定。"""

    def __init__(self, exists: bool, hits: list[SearchHit]) -> None:
        self._exists = exists
        self._hits = hits
        self.search_limits: list[int] = []

    def exists(self) -> bool:
        return self._exists

    def search(self, vector: list[float], limit: int = 8) -> list[SearchHit]:
        self.search_limits.append(limit)
        return self._hits[:limit]


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> Any:
    """把 embedding 与向量库换成受控实现。

    打在 `backend.api.search` 的名字上而非源模块：端点是以 `from ... import` 引进来的，
    改源模块不影响已绑定的名字——这类打桩失效的表现是「测试通过但打的桩没生效」。
    """

    class Holder:
        embedding = _FakeEmbedding()
        store = _FakeStore(exists=True, hits=[_hit(i) for i in range(3)])

    holder = Holder()
    monkeypatch.setattr(
        "backend.api.search.build_embedding_provider", lambda s: holder.embedding
    )
    monkeypatch.setattr(
        "backend.api.search.VectorStore", lambda index_dir, digest: holder.store
    )
    return holder


def _app_with_task(tmp_path: Path, *, completed: bool = True) -> Any:
    app = create_app(make_settings(workspace_root=tmp_path / "ws"))
    task = AnalysisTask(
        task_id="t1",
        repo_url=REPO_URL,
        repo="acme/widget",
        stage=Stage.DONE if completed else Stage.PARSING,
        completed=completed,
    )
    task.final_state["commit_sha"] = "c" * 40
    app.state.registry._tasks[task.task_id] = task
    return app


@pytest.fixture
def client(tmp_path: Path, stubs: Any) -> Any:
    with TestClient(_app_with_task(tmp_path)) as test_client:
        yield test_client


def _search(client: TestClient, query: str = "认证逻辑在哪", **params: Any) -> Any:
    return client.post(
        "/api/analyses/t1/search", json={"query": query, **params}
    )


class TestHits:
    def test_returns_structured_hits(self, client: TestClient) -> None:
        """Covers AE-08。命中块带路径、行范围、符号名、片段与距离。"""
        body = _search(client).json()
        assert len(body["hits"]) == 3
        first = body["hits"][0]
        assert first["path"] == "pkg/auth.py"
        assert first["start_line"] == 10
        assert first["end_line"] == 18
        assert first["symbol"] == "verify_token"
        assert "verify_token" in first["content"]
        assert first["distance"] == pytest.approx(0.21)

    def test_found_flag_true_with_hits(self, client: TestClient) -> None:
        assert _search(client).json()["found"] is True

    def test_limit_converges_to_cap(self, client: TestClient, stubs: Any) -> None:
        """limit 超上限时收敛而非报错，沿用 MCP 侧的 1~20 语义。"""
        assert _search(client, limit=500).status_code == 200
        assert stubs.store.search_limits[-1] == 20

    def test_limit_converges_to_floor(self, client: TestClient, stubs: Any) -> None:
        assert _search(client, limit=0).status_code == 200
        assert stubs.store.search_limits[-1] == 1


class TestNotIndexedVersusNotFound:
    """AE-08 的核心：两种「没有结果」不得混同。"""

    def test_missing_index_has_its_own_reason(
        self, tmp_path: Path, stubs: Any
    ) -> None:
        """Covers AE-08。索引不存在时返回 index_missing，不返回空命中列表。"""
        stubs.store = _FakeStore(exists=False, hits=[])
        with TestClient(_app_with_task(tmp_path)) as client:
            response = _search(client)
        assert response.status_code == 409
        assert response.json()["reason"] == "index_missing"

    def test_empty_result_reports_not_found(
        self, tmp_path: Path, stubs: Any
    ) -> None:
        """检索返回空时 found 为 false、命中为空数组，且 reason 与上一条不同。"""
        stubs.store = _FakeStore(exists=True, hits=[])
        with TestClient(_app_with_task(tmp_path)) as client:
            body = _search(client).json()
        assert body["found"] is False
        assert body["hits"] == []
        assert body["reason"] == "no_match"
        assert body["note"]

    def test_identity_mismatch_reads_as_missing_index(
        self, tmp_path: Path, stubs: Any
    ) -> None:
        """Covers AE-08。provider identity 变更导致键不匹配时同样是「未建索引」。

        实现上这与「集合不存在」走同一条路径——`exists()` 按缓存键定位集合，键变了就
        找不到。断言它不退化成空结果：那会让用户以为仓库里没有相关代码。
        """
        stubs.embedding = _FakeEmbedding()
        stubs.store = _FakeStore(exists=False, hits=[_hit()])
        with TestClient(_app_with_task(tmp_path)) as client:
            body = _search(client).json()
        assert body["reason"] == "index_missing"
        assert "hits" not in body

    def test_missing_index_skips_vectorization(
        self, tmp_path: Path, stubs: Any
    ) -> None:
        """没索引时不该先付一次向量化的钱——判定顺序是先查集合再向量化。"""
        stubs.store = _FakeStore(exists=False, hits=[])
        with TestClient(_app_with_task(tmp_path)) as client:
            _search(client)
        assert stubs.embedding.calls == []


class TestRejections:
    def test_empty_query_rejected_without_vectorizing(
        self, client: TestClient, stubs: Any
    ) -> None:
        assert _search(client, query="").status_code == 422
        assert stubs.embedding.calls == []

    def test_whitespace_query_rejected(self, client: TestClient, stubs: Any) -> None:
        """纯空白与空串走同一条路：都不该发起向量化。"""
        response = _search(client, query="   ")
        assert response.status_code == 400
        assert response.json()["reason"] == "empty_query"
        assert stubs.embedding.calls == []

    def test_vectorization_failure_is_not_no_match(
        self, tmp_path: Path, stubs: Any
    ) -> None:
        """向量化失败要说清，不返回空结果冒充「未找到」。"""
        stubs.embedding = _FakeEmbedding(fail=True)
        with TestClient(_app_with_task(tmp_path)) as client:
            response = _search(client)
        assert response.status_code == 502
        body = response.json()
        assert body["reason"] == "embedding_failed"
        assert "RuntimeError" in body["message"] or "向量化" in body["message"]

    def test_unknown_task_returns_task_not_found(self, client: TestClient) -> None:
        response = client.post(
            "/api/analyses/nope/search", json={"query": "x"}
        )
        assert response.status_code == 404
        assert response.json()["reason"] == "task_not_found"

    def test_incomplete_analysis_returns_409(
        self, tmp_path: Path, stubs: Any
    ) -> None:
        """分析未完成时索引还没建好，与「索引不存在」用不同 reason 表达。"""
        with TestClient(_app_with_task(tmp_path, completed=False)) as client:
            response = _search(client)
        assert response.status_code == 409
        assert response.json()["reason"] == "analysis_incomplete"


class TestNoLlm:
    def test_search_never_constructs_llm(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """检索是纯向量操作，路径上不该出现 LLMProvider。"""
        constructed: list[object] = []
        from backend.providers import llm as llm_module

        original = llm_module.LLMProvider.__init__

        def counted(self: Any, *args: Any, **kwargs: Any) -> None:
            constructed.append(self)
            original(self, *args, **kwargs)

        monkeypatch.setattr(llm_module.LLMProvider, "__init__", counted)
        assert _search(client).status_code == 200
        assert constructed == []
