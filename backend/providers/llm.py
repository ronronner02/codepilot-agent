"""LLM 调用封装。只做调用转发、重试与配置解析，不含业务逻辑。

档位对应 KTD3：flash 用于模块扇出（高频低判断），pro 用于汇总与评审（低频高判断）。
具体模型由配置决定——抽象包的是 OpenAI 兼容端点，换 provider 只改配置。

**重试是必需而非健壮性修饰。** 实测所用网关会返回 429
（`channel_rpm_limit_exceeded`），而 flash 档要承担并行扇出（扇出宽度上限见配置），
不处理限流会让部分模块分析随机失败——那种失败表现为「报告少了一节」，比报错更难
察觉。5xx 同理：网关的上游通道不稳定时会短暂不可用。
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Literal

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    BadRequestError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
)

from backend.config import Settings

logger = logging.getLogger("codepilot.llm")

Tier = Literal["flash", "pro"]

# 可重试的异常。其余（认证失败、模型不存在、请求格式错）重试无意义——它们不会因为
# 再试一次而变好，只会浪费时间并掩盖配置错误。
RETRYABLE = (RateLimitError, InternalServerError, APIConnectionError, APITimeoutError)

# 网关把上游瞬时故障也报成 403 时，body 里带的标记。
#
# 实测教训：中转网关返回 403 且 body 为
# `{'code': 'bad_response_status_code', 'type': 'upstream_error'}`，随后同样的请求
# 立即成功——那是上游通道瞬时不可用，不是鉴权拒绝。
#
# 为什么不把所有 403 都重试：真正的 key 配错也是 403，重试四次才报出会让排查绕远，
# 真实原因还被埋在重试日志后面。按 body 标记区分，两种情况各得其所。
_UPSTREAM_ERROR_MARKERS = ("upstream_error", "bad_response_status_code")


def _is_transient_upstream_error(exc: APIStatusError) -> bool:
    body = str(getattr(exc, "body", "") or "") + str(exc)
    return any(marker in body for marker in _UPSTREAM_ERROR_MARKERS)

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 1.5


class LLMProvider:
    def __init__(
        self,
        settings: Settings,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        base_delay: float = DEFAULT_BASE_DELAY,
        max_concurrency: int | None = None,
    ) -> None:
        self._settings = settings
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        # 并发闸门。provider 是所有 LLM 调用的唯一收口点，放这里能约束全部调用方——
        # 放在扇出侧则只管扇出，Reviewer 的批量判断与 Planner 又会各自绕过。
        limit = max_concurrency if max_concurrency is not None else settings.llm_max_concurrency
        self._gate = asyncio.Semaphore(max(1, limit))
        # SDK 自带的 max_retries 关掉：它对 429 的退避策略不可控，且与这里的退避
        # 叠加会让实际等待时间变成两层的乘积，难以推算。重试逻辑集中在一处。
        self._client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            max_retries=0,
        )

    def model_for(self, tier: Tier) -> str:
        return (
            self._settings.llm_model_flash
            if tier == "flash"
            else self._settings.llm_model_pro
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tier: Tier = "flash",
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> Any:
        """发一次对话请求，限流与临时故障自动重试。

        指数退避加随机抖动：并行扇出时多路请求会同时撞上限流，固定间隔重试会让它们
        继续同步撞车（惊群），抖动把重试时刻打散。
        """
        kwargs: dict[str, Any] = {
            "model": self.model_for(tier),
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        if response_format:
            kwargs["response_format"] = response_format

        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                # 闸门只圈住实际请求，不圈退避等待——否则重试时占着名额睡觉，
                # 其他调用被白白挡住，整体吞吐反而更差。
                async with self._gate:
                    return await self._client.chat.completions.create(**kwargs)
            except BadRequestError as exc:
                # 并非所有端点都支持 response_format。实测所用网关对
                # `{"type": "json_object"}` 直接返回 400，而不传它时模型本就返回干净
                # JSON。降级重试一次而非整批失败——调用方已有容错解析，少一个参数不
                # 影响结果，而让整类检查记为失败是不成比例的。
                if "response_format" not in kwargs:
                    raise
                logger.warning(
                    "端点拒绝 response_format（%s），去掉该参数重试", type(exc).__name__
                )
                kwargs.pop("response_format")
                continue
            except PermissionDeniedError as exc:
                # 只有 body 标明是上游瞬时故障时才重试；真正的鉴权拒绝立刻抛出。
                if not _is_transient_upstream_error(exc):
                    raise
                last_error = exc
                if attempt == self._max_attempts:
                    break
                delay = self._base_delay * (2 ** (attempt - 1))
                delay += random.uniform(0, self._base_delay)
                logger.warning(
                    "上游瞬时故障（第 %d/%d 次，HTTP 403），%.1fs 后重试",
                    attempt,
                    self._max_attempts,
                    delay,
                )
                await asyncio.sleep(delay)
            except RETRYABLE as exc:
                last_error = exc
                if attempt == self._max_attempts:
                    break
                delay = self._base_delay * (2 ** (attempt - 1))
                delay += random.uniform(0, self._base_delay)
                logger.warning(
                    "LLM 调用失败（第 %d/%d 次）%s，%.1fs 后重试",
                    attempt,
                    self._max_attempts,
                    type(exc).__name__,
                    delay,
                )
                await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error
