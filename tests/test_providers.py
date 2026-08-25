"""provider 抽象的契约测试。

identity 的跨进程稳定性是重点：它进入 U9 的缓存键，不稳定就会让缓存在无变化时
反复失效，或更糟——在 provider 变了之后仍然命中。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from openai import BadRequestError, PermissionDeniedError, RateLimitError

from backend.config import Settings
from backend.providers.embedding import (
    ApiEmbeddingProvider,
    LocalEmbeddingProvider,
    build_embedding_provider,
)
from backend.providers.llm import LLMProvider


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "deepseek_api_key": "test-key",
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_llm_tier_maps_to_configured_model() -> None:
    provider = LLMProvider(
        _settings(llm_model_flash="flash-model", llm_model_pro="pro-model")
    )
    assert provider.model_for("flash") == "flash-model"
    assert provider.model_for("pro") == "pro-model"


class _FakeCompletions:
    """按预设序列抛异常或返回结果，用来控制重试路径。"""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _install_fake(provider: LLMProvider, outcomes: list[object]) -> _FakeCompletions:
    fake = _FakeCompletions(outcomes)

    class _Chat:
        completions = fake

    provider._client = type("_Client", (), {"chat": _Chat()})()  # type: ignore[assignment]
    return fake


def _rate_limit_error() -> RateLimitError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(429, request=request, json={"error": {"code": "rpm_limit"}})
    return RateLimitError("rate limited", response=response, body=None)


class TestLLMRetry:
    """重试是必需而非健壮性修饰：实测网关会返回 429，而 flash 档要承担并行扇出，
    不处理限流会让部分模块分析随机失败——表现为「报告少了一节」，比报错更难察觉。
    """

    async def test_retries_rate_limit_then_succeeds(self) -> None:
        provider = LLMProvider(_settings(), base_delay=0.0)
        fake = _install_fake(provider, [_rate_limit_error(), _rate_limit_error(), "ok"])
        assert await provider.chat([{"role": "user", "content": "hi"}]) == "ok"
        assert len(fake.calls) == 3

    async def test_gives_up_after_max_attempts(self) -> None:
        provider = LLMProvider(_settings(), max_attempts=2, base_delay=0.0)
        fake = _install_fake(provider, [_rate_limit_error(), _rate_limit_error()])
        with pytest.raises(RateLimitError):
            await provider.chat([{"role": "user", "content": "hi"}])
        assert len(fake.calls) == 2

    async def test_non_retryable_error_fails_immediately(self) -> None:
        """认证失败、模型不存在这类错误重试无意义——再试一次不会变好，
        只会浪费时间并掩盖配置错误。
        """
        provider = LLMProvider(_settings(), base_delay=0.0)
        fake = _install_fake(provider, [ValueError("模型名写错了")])
        with pytest.raises(ValueError):
            await provider.chat([{"role": "user", "content": "hi"}])
        assert len(fake.calls) == 1, "不可重试的错误不应重试"

    async def test_tools_and_response_format_forwarded(self) -> None:
        provider = LLMProvider(_settings(), base_delay=0.0)
        tools = [{"type": "function", "function": {"name": "judge"}}]
        fake = _install_fake(provider, ["ok"])
        await provider.chat(
            [{"role": "user", "content": "hi"}],
            tier="pro",
            tools=tools,
            response_format={"type": "json_object"},
        )
        assert fake.calls[0]["tools"] == tools
        assert fake.calls[0]["response_format"] == {"type": "json_object"}

    async def test_omits_optional_params_when_absent(self) -> None:
        """不传的参数不应出现在请求里——有些端点对 null 值报错。"""
        provider = LLMProvider(_settings(), base_delay=0.0)
        fake = _install_fake(provider, ["ok"])
        await provider.chat([{"role": "user", "content": "hi"}])
        assert "tools" not in fake.calls[0]
        assert "response_format" not in fake.calls[0]


def _bad_request_error() -> BadRequestError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(400, request=request, json={"error": {"code": "bad_param"}})
    return BadRequestError("bad request", response=response, body=None)


def _permission_error(body: dict[str, object]) -> PermissionDeniedError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(403, request=request, json=body)
    return PermissionDeniedError("forbidden", response=response, body=body)


class TestConcurrencyGate:
    """实测经中转网关时 3 路并发触发 Cloudflare 524 与 Server disconnected，
    四次重试全撞在饱和的网关上。闸门放在 provider——它是所有 LLM 调用的唯一收口点。
    """

    async def test_concurrency_capped(self) -> None:
        provider = LLMProvider(_settings(), base_delay=0.0, max_concurrency=2)
        in_flight = 0
        peak = 0

        class _Tracking:
            async def create(self, **kwargs: object) -> str:
                nonlocal in_flight, peak
                in_flight += 1
                peak = max(peak, in_flight)
                await asyncio.sleep(0.02)
                in_flight -= 1
                return "ok"

        tracker = _Tracking()

        class _Chat:
            completions = tracker

        provider._client = type("_Client", (), {"chat": _Chat()})()  # type: ignore[assignment]

        await asyncio.gather(
            *[provider.chat([{"role": "user", "content": "hi"}]) for _ in range(6)]
        )
        assert peak <= 2, f"同时在途请求达到 {peak}，超过闸门上限"

    async def test_gate_does_not_hold_slot_during_backoff(self) -> None:
        """退避等待不占名额——否则重试时占着位置睡觉，其他调用被白白挡住。"""
        provider = LLMProvider(_settings(), base_delay=0.05, max_concurrency=1)
        outcomes: list[object] = [_rate_limit_error(), "ok", "ok"]
        fake = _install_fake(provider, outcomes)

        results = await asyncio.gather(
            provider.chat([{"role": "user", "content": "a"}]),
            provider.chat([{"role": "user", "content": "b"}]),
        )
        assert results == ["ok", "ok"]
        assert len(fake.calls) == 3

    async def test_config_default_applies(self) -> None:
        provider = LLMProvider(_settings(llm_max_concurrency=3))
        assert provider._gate._value == 3  # type: ignore[attr-defined]


class TestTransientUpstreamError:
    """网关把上游瞬时故障报成 403。实测：body 为 upstream_error，随后同样请求立即成功。

    但不能把所有 403 都重试——真正的 key 配错也是 403，重试四次才报出会让排查绕远。
    """

    async def test_upstream_marker_triggers_retry(self) -> None:
        provider = LLMProvider(_settings(), base_delay=0.0)
        error = _permission_error(
            {"error": {"code": "bad_response_status_code", "type": "upstream_error"}}
        )
        fake = _install_fake(provider, [error, "ok"])
        assert await provider.chat([{"role": "user", "content": "hi"}]) == "ok"
        assert len(fake.calls) == 2

    async def test_genuine_permission_error_raises_immediately(self) -> None:
        """真正的鉴权拒绝立刻抛出，不重试。"""
        provider = LLMProvider(_settings(), base_delay=0.0)
        error = _permission_error({"error": {"message": "invalid api key", "type": "auth"}})
        fake = _install_fake(provider, [error])
        with pytest.raises(PermissionDeniedError):
            await provider.chat([{"role": "user", "content": "hi"}])
        assert len(fake.calls) == 1, "鉴权失败重试只会掩盖配置错误"

    async def test_gives_up_after_max_attempts(self) -> None:
        provider = LLMProvider(_settings(), max_attempts=2, base_delay=0.0)
        error = _permission_error({"error": {"type": "upstream_error"}})
        fake = _install_fake(provider, [error, error])
        with pytest.raises(PermissionDeniedError):
            await provider.chat([{"role": "user", "content": "hi"}])
        assert len(fake.calls) == 2


class TestResponseFormatFallback:
    """并非所有端点都支持 response_format。实测所用网关对 json_object 直接返回 400，
    而不传它时模型本就返回干净 JSON。降级重试而非让整类检查记为失败。
    """

    async def test_drops_response_format_and_retries(self) -> None:
        provider = LLMProvider(_settings(), base_delay=0.0)
        fake = _install_fake(provider, [_bad_request_error(), "ok"])
        result = await provider.chat(
            [{"role": "user", "content": "hi"}], response_format={"type": "json_object"}
        )
        assert result == "ok"
        assert fake.calls[0]["response_format"] == {"type": "json_object"}
        assert "response_format" not in fake.calls[1], "重试时应去掉该参数"

    async def test_bad_request_without_response_format_propagates(self) -> None:
        """请求本身有问题时不该被降级逻辑掩盖——那会让真实的参数错误难以定位。"""
        provider = LLMProvider(_settings(), base_delay=0.0)
        fake = _install_fake(provider, [_bad_request_error()])
        with pytest.raises(BadRequestError):
            await provider.chat([{"role": "user", "content": "hi"}])
        assert len(fake.calls) == 1


def test_build_selects_provider_by_config() -> None:
    assert isinstance(
        build_embedding_provider(_settings(embedding_provider="local")),
        LocalEmbeddingProvider,
    )
    assert isinstance(
        build_embedding_provider(
            _settings(embedding_provider="api", embedding_api_key="k")
        ),
        ApiEmbeddingProvider,
    )


def test_api_provider_rejects_unregistered_model_dimension() -> None:
    provider = ApiEmbeddingProvider(
        _settings(
            embedding_provider="api",
            embedding_api_key="k",
            embedding_api_model="some-future-model",
        )
    )
    with pytest.raises(ValueError, match="未登记"):
        _ = provider.dimension


def test_unregistered_model_error_warns_against_nominal_spec() -> None:
    """登记值必须是实测结果——实测教训：qwen3-embedding-8b 标称 4096，
    经当前网关实际返回 768。按标称值登记会让向量库以错误维度建集合。
    """
    provider = ApiEmbeddingProvider(
        _settings(embedding_provider="api", embedding_api_key="k", embedding_api_model="x")
    )
    with pytest.raises(ValueError, match="实测"):
        _ = provider.dimension


class TestDimensionVerification:
    """维度不符的后果是索引与查询向量空间不一致——检索静默变差而不报错，极难定位。
    所以建索引前实测校验一次，成本是一次 embedding 调用。
    """

    async def test_matching_dimension_returns_actual(self) -> None:
        provider = ApiEmbeddingProvider(
            _settings(
                embedding_provider="api",
                embedding_api_key="k",
                embedding_api_model="text-embedding-v3",
            )
        )

        async def fake_embed(texts: list[str]) -> list[list[float]]:
            return [[0.0] * 1024 for _ in texts]

        provider.embed_texts = fake_embed  # type: ignore[method-assign]
        assert await provider.verify_dimension() == 1024

    async def test_mismatch_raises_with_both_numbers(self) -> None:
        provider = ApiEmbeddingProvider(
            _settings(
                embedding_provider="api",
                embedding_api_key="k",
                embedding_api_model="text-embedding-v3",
            )
        )

        async def fake_embed(texts: list[str]) -> list[list[float]]:
            return [[0.0] * 768 for _ in texts]

        provider.embed_texts = fake_embed  # type: ignore[method-assign]
        with pytest.raises(ValueError, match="维度不符") as exc:
            await provider.verify_dimension()
        assert "1024" in str(exc.value)
        assert "768" in str(exc.value)


def test_identity_differs_between_providers() -> None:
    local = LocalEmbeddingProvider(_settings(embedding_local_model="model-a"))
    api = ApiEmbeddingProvider(
        _settings(
            embedding_provider="api",
            embedding_api_key="k",
            embedding_api_model="model-a",
        )
    )
    # 同名模型也必须区分：本地与 API 的向量空间不保证一致。
    assert local.identity != api.identity


def test_identity_is_stable_across_instances() -> None:
    first = LocalEmbeddingProvider(_settings(embedding_local_model="model-a"))
    second = LocalEmbeddingProvider(_settings(embedding_local_model="model-a"))
    assert first.identity == second.identity


def test_identity_changes_with_model_name() -> None:
    a = LocalEmbeddingProvider(_settings(embedding_local_model="model-a"))
    b = LocalEmbeddingProvider(_settings(embedding_local_model="model-b"))
    assert a.identity != b.identity
