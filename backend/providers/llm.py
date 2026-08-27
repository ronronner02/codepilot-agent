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
import hashlib
import logging
import random
from dataclasses import dataclass
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


@dataclass(frozen=True)
class GuestCredentials:
    """访客自带的 LLM 凭证（KTD3）。

    定义在 provider 层而非 API 层：provider 是全部 LLM 调用的唯一收口点，让它反向依赖
    `backend.api.schemas` 会把 HTTP 形状带进领域层。API 层负责把请求体映射成这个类型。

    **不挂在 Settings 上**是本类型存在的全部理由。Settings 被 `/api/health` 摘要输出、
    被日志打印、被 `lru_cache` 缓存；凭证挂上去之后 BR-004 就只能靠「记得不要打印」维持。
    独立类型让「凭证不在 Settings 里」成为类型层面的事实。

    四个字段逐项回落到服务端配置：只填了 key 的访客用服务端的 base_url 是有意义的组合，
    而强制四项齐全会让「换个模型名」也得重填一遍。
    """

    api_key: str
    base_url: str = ""
    model_flash: str = ""
    model_pro: str = ""


# 进程级并发闸门。
#
# **为什么必须是进程级。** 改成按请求构造 provider 之后，实例级信号量失去全局约束力——
# 每个任务各有一个实例、各有一个闸门，全局在途请求数变成 `并发任务数 × llm_max_concurrency`。
# 而 `backend/config.py` 记录的实测是「经中转网关时 3 路并发就触发 Cloudflare 524，四次
# 重试全撞在饱和的网关上」。不处理这一点等于把一个已知的生产故障重新引入。
#
# **为什么按凭证身份分池而非全局单池。** 实测的饱和点在网关侧，所以真正互相影响的是同一
# base_url 的访客。全局单池会让一个访客的分析卡住另一个访客，那是不必要的耦合。
#
# 键是 base_url + api_key 的摘要而非明文：这个键会进日志与异常文本。
_identity_gates: dict[str, tuple[asyncio.Semaphore, int]] = {}
_global_gate: tuple[asyncio.Semaphore, int] | None = None


def credential_identity(base_url: str, api_key: str) -> str:
    """凭证身份的稳定摘要。不含明文，可安全出现在日志与错误信息里。"""
    material = f"{base_url}\x00{api_key}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


def _resolve_gate(identity: str, limit: int) -> asyncio.Semaphore:
    """取该凭证身份的名额池。上限变了就换一个新的信号量。

    换新而非调整既有信号量：asyncio.Semaphore 没有改上限的接口，而「改了配置却还按旧
    上限跑」比「重建池子」更难排查。代价是切换瞬间在途请求可能短暂超过新上限。
    """
    cached = _identity_gates.get(identity)
    if cached is None or cached[1] != limit:
        cached = (asyncio.Semaphore(max(1, limit)), limit)
        _identity_gates[identity] = cached
    return cached[0]


def _resolve_global_gate(limit: int) -> asyncio.Semaphore:
    """全局总闸。兜住「多个不同 base_url 的访客同时在跑」的情形。"""
    global _global_gate
    if _global_gate is None or _global_gate[1] != limit:
        _global_gate = (asyncio.Semaphore(max(1, limit)), limit)
    return _global_gate[0]


def reset_gates() -> None:
    """清空闸门注册表。仅供测试使用——进程级状态会在用例之间串流。"""
    global _global_gate
    _identity_gates.clear()
    _global_gate = None


class LLMProvider:
    def __init__(
        self,
        settings: Settings,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        base_delay: float = DEFAULT_BASE_DELAY,
        max_concurrency: int | None = None,
        credentials: GuestCredentials | None = None,
        global_max_concurrency: int | None = None,
    ) -> None:
        self._settings = settings
        self._max_attempts = max_attempts
        self._base_delay = base_delay

        # 访客凭证逐项覆盖服务端配置（KTD3）。为空时行为与本单元之前完全一致——
        # 既有 14 处不传该参数的构造点因此不受影响。
        api_key = (credentials.api_key if credentials else "") or settings.deepseek_api_key
        base_url = (credentials.base_url if credentials else "") or settings.deepseek_base_url
        self._model_flash = (
            credentials.model_flash if credentials else ""
        ) or settings.llm_model_flash
        self._model_pro = (
            credentials.model_pro if credentials else ""
        ) or settings.llm_model_pro

        # 闸门是进程级的，按凭证身份分池（见模块内 _identity_gates 的说明）。
        # provider 仍是所有 LLM 调用的唯一收口点，所以闸门放这里能约束全部调用方——
        # 放在扇出侧则只管扇出，Reviewer 的批量判断与 Planner 又会各自绕过。
        self._identity = credential_identity(base_url, api_key)
        limit = (
            max_concurrency if max_concurrency is not None else settings.llm_max_concurrency
        )
        self._gate = _resolve_gate(self._identity, limit)
        global_limit = (
            global_max_concurrency
            if global_max_concurrency is not None
            else settings.llm_global_max_concurrency
        )
        self._global_gate = _resolve_global_gate(global_limit)

        # SDK 自带的 max_retries 关掉：它对 429 的退避策略不可控，且与这里的退避
        # 叠加会让实际等待时间变成两层的乘积，难以推算。重试逻辑集中在一处。
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=0,
        )

    @property
    def gate_identity(self) -> str:
        """本 provider 所属的名额池标识。摘要形态，不含凭证明文。"""
        return self._identity

    def model_for(self, tier: Tier) -> str:
        return self._model_flash if tier == "flash" else self._model_pro

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
                #
                # 先全局再按身份：反过来的话，拿到身份名额的请求会排在全局闸门外等待，
                # 而它占着的身份名额挡住了同一访客的其他请求——两个闸门叠出一个死等窗口。
                async with self._global_gate, self._gate:
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
