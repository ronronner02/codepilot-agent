"""按 IP 的提交限流（U6，R-53）。

**滑动时间窗而非固定窗。** 固定窗（每小时整点清零）允许在窗口边界处双倍突发：59 分提交
3 次、01 分再提交 3 次，两分钟内 6 次全部通过。滑动窗记住每次提交的时刻，"过去一小时内"
按当前时刻回看，边界突发不成立。代价是每个 IP 要存一串时间戳而非一个计数——在单机单
worker 的规模下这点内存无关紧要。

**拒绝时必须给可重试时间**（R-53）。只说「请稍后再试」等于让用户去猜，而这个值是可算的：
窗口内最早一次提交的时刻 + 窗口长度 - 当前时刻。

**时间源可注入**是为了测试。真实实现取 `time.monotonic`；测试传一个可控的计数器，从而
断言"窗口滑过后可再次提交"而不必真的睡一小时。用 monotonic 而非 wall clock：系统时间被
NTP 回调时 wall clock 会倒退，那会让限流窗口凭空变长或计数错乱。
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic

logger = logging.getLogger("codepilot.api.ratelimit")

# 触发全表清扫的 IP 数阈值。
#
# 为什么需要它：按访问时才剪枝只会剪到"正在提交的那个 IP"，而公网端点上会积累大量
# 只来过一次的 IP，它们的过期条目永远没人碰。这是个慢速内存泄漏——单条很小，但没有上界。
_SWEEP_THRESHOLD = 1024


@dataclass(frozen=True)
class RateDecision:
    """一次限流判定。

    `retry_after_seconds` 只在被拒时有意义，向上取整到秒——给用户看"还要等 0.3 秒"没有
    意义，而向下取整会让按它重试的客户端刚好再被拒一次。
    """

    allowed: bool
    retry_after_seconds: int = 0
    current_count: int = 0
    limit: int = 0


class SubmissionRateLimiter:
    """同一 IP 在滑动时间窗内的提交次数上限。"""

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._limit = max(1, limit)
        self._window = max(1.0, float(window_seconds))
        self._clock = clock
        # deque 而非 list：过期条目总是从左端出，popleft 是 O(1) 而 list.pop(0) 是 O(n)。
        self._hits: dict[str, deque[float]] = {}

    @property
    def limit(self) -> int:
        return self._limit

    def _prune(self, stamps: deque[float], now: float) -> None:
        cutoff = now - self._window
        while stamps and stamps[0] <= cutoff:
            stamps.popleft()

    def _sweep(self, now: float) -> None:
        """丢掉所有条目都已过期的 IP。见 _SWEEP_THRESHOLD 的说明。"""
        if len(self._hits) < _SWEEP_THRESHOLD:
            return
        for key in [k for k, v in self._hits.items() if not v or v[-1] <= now - self._window]:
            del self._hits[key]

    def check(self, client_key: str) -> RateDecision:
        """判定但**不计数**。

        与 record 分开是因为校验顺序要求限流判定发生在建任务之前，而计数只应在真正受理
        一次提交时发生——否则一个因地址非法而被拒的请求也会吃掉配额，用户改对地址后
        反而被限流拦住。
        """
        now = self._clock()
        stamps = self._hits.get(client_key)
        if stamps is None:
            return RateDecision(allowed=True, current_count=0, limit=self._limit)
        self._prune(stamps, now)
        if len(stamps) < self._limit:
            return RateDecision(
                allowed=True, current_count=len(stamps), limit=self._limit
            )
        # 最早一次提交滑出窗口的时刻，就是下一次可提交的时刻。
        retry_after = stamps[0] + self._window - now
        return RateDecision(
            allowed=False,
            # 至少 1 秒：算出 0.2 秒时返回 0 会让界面显示「0 秒后可重试」却仍被拒。
            retry_after_seconds=max(1, int(retry_after) + (1 if retry_after % 1 else 0)),
            current_count=len(stamps),
            limit=self._limit,
        )

    def record(self, client_key: str) -> None:
        """记一次已受理的提交。"""
        now = self._clock()
        stamps = self._hits.setdefault(client_key, deque())
        self._prune(stamps, now)
        stamps.append(now)
        self._sweep(now)

    def refund(self, client_key: str) -> bool:
        """退还该 IP 最近一次计数。返回是否真的退了。

        **为什么需要退还。** 限流在提交阶段计数，而克隆发生在后台任务里——一次因**我们这侧**
        的网络故障（TLS 断连、DNS 失败）而失败的分析，此刻已经吃掉了用户的一次配额。按 3 次/
        小时的默认值，三次网络抖动就把人锁一小时，而他什么都没做错。

        这与「未配置凭证不计数」是同一条原则：没有产生真实工作的提交不该扣配额。区别只是
        凭证那道门在计数之前，而克隆失败发生在计数之后，所以只能退。

        只退最近一次而非按 task 精确匹配：限流器存的是时间戳而非 task_id，加一层映射会让它
        从「一个 IP 一串时间戳」变成「还要知道哪次提交对应哪个任务」——那是调用方的知识，
        不该渗进限流器。代价是并发提交时可能退错一次的归属，而配额本身是按 IP 计的，
        退哪一次时间戳对 IP 的总量没有区别。
        """
        stamps = self._hits.get(client_key)
        if not stamps:
            return False
        stamps.pop()
        if not stamps:
            del self._hits[client_key]
        return True

    def current_count(self, client_key: str) -> int:
        stamps = self._hits.get(client_key)
        if stamps is None:
            return 0
        self._prune(stamps, self._clock())
        return len(stamps)


def client_key(forwarded_for: str | None, direct_host: str | None) -> str:
    """取客户端标识。

    优先 `X-Forwarded-For` 的**最左**项：链路上每一跳都会向右追加，最左是原始客户端。
    取最右会取到最后一个代理，那时全站共用一个配额。

    这个头是客户端可伪造的——直连暴露时任何人都能靠改头绕过限流。它成立的前提是反代
    覆写该头并透传真实 IP（README 的公网部署一节写明了这个要求），而 KTD9 已把「不做
    反代配置本身」定为范围外。回落到直连 IP 是为了本地与无反代环境。
    """
    if forwarded_for:
        first = forwarded_for.split(",")[0].strip()
        if first:
            return first
    return (direct_host or "unknown").strip() or "unknown"
