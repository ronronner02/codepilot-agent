"""访客凭证的边界（U5：BR-004、NA-02、NA-03、R-49、R-68）。

**这些是边界断言而非功能断言。** 功能正常时它们也可能已经失守——凭证回落到服务端配置
的表现是「分析跑得很顺」，凭证进日志的表现是「日志很详细」。所以它们各自独立成条，
每条都钉住一个具体的不应发生。

拦截必须在提交阶段（R-68）：`chunk_and_index` 分支不依赖 LLM，放进去跑就会消耗 owner
的 embedding 额度，而 AE-23 要求那个数字是零。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.providers import llm as llm_module
from tests.support import make_settings

REPO_URL = "https://github.com/acme/widget"

GUEST = {
    "api_key": "sk-guest-secret-9f3a",
    "base_url": "https://guest-gateway.example",
    "model_flash": "guest-flash",
    "model_pro": "guest-pro",
}


class _Spy:
    """记录三个花钱动作的调用次数。"""

    def __init__(self) -> None:
        self.clone = 0
        self.embedding = 0
        self.llm = 0

    def snapshot(self) -> tuple[int, int, int]:
        return (self.clone, self.embedding, self.llm)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> Any:
    """把 clone、embedding、LLM 三处构造点都换成计数器。

    打在 `backend.graph.real_nodes` 的名字上：那是生产节点实际调用的绑定。改源模块不影响
    已 import 的名字——那类打桩失效的表现是「测试通过但桩没生效」，恰好会让本文件的
    负面断言全部变成假绿灯。
    """
    counter = _Spy()

    async def fake_clone(*args: object, **kwargs: object) -> object:
        counter.clone += 1
        raise AssertionError("未配置凭证时不该发生克隆")

    def fake_embedding(*args: object, **kwargs: object) -> object:
        counter.embedding += 1
        raise AssertionError("未配置凭证时不该构造 embedding provider")

    original_init = llm_module.LLMProvider.__init__

    def counted_init(self: Any, *args: Any, **kwargs: Any) -> None:
        counter.llm += 1
        original_init(self, *args, **kwargs)

    monkeypatch.setattr("backend.graph.real_nodes.clone_repo", fake_clone)
    monkeypatch.setattr(
        "backend.graph.real_nodes.build_embedding_provider", fake_embedding
    )
    monkeypatch.setattr(llm_module.LLMProvider, "__init__", counted_init)
    return counter


@pytest.fixture
def client(tmp_path: Path, spy: Any) -> Any:
    """真实的应用与真实的生产节点集。

    不换 make_real_nodes：本文件要验证的正是「拒绝发生在真实调用点之前」，而换掉节点集
    等于把要验证的那段路径拿掉。三个 spy 在被调用时直接抛断言，所以一旦拒绝失守，
    失败信息会直接指向哪一步不该跑却跑了。
    """
    app = create_app(
        make_settings(
            workspace_root=tmp_path / "ws", deepseek_api_key="server-side-key-xyz"
        )
    )
    with TestClient(app) as test_client:
        yield test_client


def _submit(client: TestClient, credentials: object | None) -> Any:
    payload: dict[str, object] = {"repo_url": REPO_URL}
    if credentials is not None:
        payload["credentials"] = credentials
    return client.post("/api/analyses", json=payload)


class TestRejectionWithoutCredentials:
    """R-49、R-68：未配置凭证时在提交阶段被拒。"""

    def test_missing_credentials_rejected_with_guiding_reason(
        self, client: TestClient
    ) -> None:
        """Covers AE-13。reason 稳定，文案指向设置页。"""
        response = _submit(client, None)
        assert response.status_code == 400
        body = response.json()
        assert body["reason"] == "credentials_required"
        assert "设置页" in body["message"]

    def test_rejection_costs_nothing(self, client: TestClient, spy: Any) -> None:
        """Covers AE-23。拒绝不产生克隆、不构造 embedding、不构造 LLM。

        断言的是**增量**而非绝对值：同进程内的其它用例可能合法地跑过后台分析，把它们
        的计数算进来会让这条测试的成败取决于执行顺序。
        """
        before = spy.snapshot()
        assert _submit(client, None).status_code == 400
        assert spy.snapshot() == before

    def test_no_task_created_on_rejection(self, client: TestClient) -> None:
        """拒绝发生在建任务之前，不留下已建但被拒的任务记录。"""
        before = len(client.get("/api/analyses").json())
        _submit(client, None)
        assert len(client.get("/api/analyses").json()) == before

    def test_server_credentials_do_not_substitute(
        self, client: TestClient, spy: Any
    ) -> None:
        """Covers AE-13, NA-03。服务端 .env 配了 key，访客没配 —— 仍然拒绝。

        这是本单元最容易失守的一条：回落到服务端凭证会让分析「正常跑起来」，功能测试
        全绿，只有 owner 的账单能发现。client fixture 特意配了服务端 key。
        """
        before = spy.snapshot()
        response = _submit(client, None)
        assert response.status_code == 400
        assert response.json()["reason"] == "credentials_required"
        assert spy.snapshot() == before, "服务端凭证被当成了访客凭证的替代品"

    def test_partial_credentials_rejected(self, client: TestClient) -> None:
        """只填 key 未填 base_url 时按未完整配置处理。"""
        response = _submit(client, {"api_key": "sk-only-key"})
        assert response.status_code == 400
        assert response.json()["reason"] == "credentials_required"

    def test_blank_api_key_rejected(self, client: TestClient) -> None:
        """纯空白的 key 与空串同等对待——它长度非零但语义为空。"""
        response = _submit(
            client, {"api_key": "   ", "base_url": "https://x.example"}
        )
        assert response.status_code == 400
        assert response.json()["reason"] == "credentials_required"

    def test_missing_base_url_rejected(self, client: TestClient) -> None:
        response = _submit(client, {"api_key": "sk-k", "base_url": "  "})
        assert response.status_code == 400


class TestNoLeak:
    """NA-02：凭证不进日志、不进响应。"""

    def test_health_never_echoes_credentials(self, client: TestClient) -> None:
        """`/api/health` 会进日志与监控，不能回显任何 key。"""
        import json

        body = client.get("/api/health").json()
        serialized = json.dumps(body)
        assert GUEST["api_key"] not in serialized
        assert "server-side-key-xyz" not in serialized
        assert "sk-" not in serialized

    def test_rejection_log_carries_reason_not_secret(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """KTD11：拒绝记一条带稳定原因字段的结构化日志，且不含凭证。"""
        with caplog.at_level(logging.INFO, logger="codepilot.api"):
            _submit(client, None)
        assert any("reason=credentials_required" in r.message for r in caplog.records)

    def test_credentials_absent_from_logs_on_accepted_submit(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Covers AE-13, NA-02。带凭证提交后，日志输出中不含 api_key 的任何片段。

        断言整个 caplog 的文本而非逐条：泄露可能出现在任何一条记录里，包括异常回溯。
        """
        with caplog.at_level(logging.DEBUG):
            _submit(client, GUEST)
        assert GUEST["api_key"] not in caplog.text
        assert "guest-secret" not in caplog.text

    def test_task_repr_hides_credentials(self, client: TestClient) -> None:
        """任务对象的 repr 不含凭证。

        它会出现在异常回溯与调试输出里——那些路径不经过 logger，审查 `logger.*` 调用
        发现不了它们，所以字段上加了 `repr=False`。
        """
        _submit(client, GUEST)
        registry = client.app.state.registry  # type: ignore[attr-defined]
        tasks = registry.list_tasks()
        assert tasks, "带凭证的提交应当建出任务"
        for task in tasks:
            assert GUEST["api_key"] not in repr(task)
