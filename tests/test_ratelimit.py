"""IP 提交限流（U6，R-53、AE-15）。

时间源注入是这套测试成立的前提：断言「窗口滑过后可再次提交」不能靠真的睡一小时。
限流器接受一个 clock 可调用对象，这里传一个可推进的假时钟。
"""

from __future__ import annotations

from backend.api.ratelimit import SubmissionRateLimiter, client_key


class FakeClock:
    """可手动推进的单调时钟。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _limiter(limit: int = 3, window: float = 3600.0) -> tuple[SubmissionRateLimiter, FakeClock]:
    clock = FakeClock()
    return SubmissionRateLimiter(limit=limit, window_seconds=window, clock=clock), clock


class TestWindow:
    def test_fourth_submission_in_window_is_rejected(self) -> None:
        limiter, _ = _limiter(limit=3)
        for _ in range(3):
            assert limiter.check("1.2.3.4").allowed
            limiter.record("1.2.3.4")

        decision = limiter.check("1.2.3.4")
        assert not decision.allowed
        assert decision.current_count == 3
        assert decision.limit == 3

    def test_rejection_carries_retry_after(self) -> None:
        """R-53 要求给可重试时间。只说「请稍后」等于让用户去猜。"""
        limiter, clock = _limiter(limit=1, window=600.0)
        limiter.record("1.2.3.4")
        clock.advance(100.0)

        decision = limiter.check("1.2.3.4")
        assert not decision.allowed
        # 最早一次在 100 秒前，窗口 600 秒 → 还要等约 500 秒。
        assert decision.retry_after_seconds == 500

    def test_retry_after_is_at_least_one_second(self) -> None:
        """算出不足 1 秒时返回 0 会让界面显示「0 秒后可重试」却仍被拒。"""
        limiter, clock = _limiter(limit=1, window=600.0)
        limiter.record("1.2.3.4")
        clock.advance(599.7)
        assert limiter.check("1.2.3.4").retry_after_seconds >= 1

    def test_window_slides_and_allows_again(self) -> None:
        limiter, clock = _limiter(limit=2, window=3600.0)
        limiter.record("1.2.3.4")
        limiter.record("1.2.3.4")
        assert not limiter.check("1.2.3.4").allowed

        clock.advance(3601.0)
        assert limiter.check("1.2.3.4").allowed
        assert limiter.current_count("1.2.3.4") == 0

    def test_partial_slide_frees_exactly_one_slot(self) -> None:
        """滑动窗的关键行为：过期的是最早那一条，不是整窗清零。"""
        limiter, clock = _limiter(limit=2, window=1000.0)
        limiter.record("1.2.3.4")
        clock.advance(600.0)
        limiter.record("1.2.3.4")
        assert not limiter.check("1.2.3.4").allowed

        # 再过 401 秒，第一条（1001 秒前）出窗，第二条（401 秒前）仍在窗内。
        clock.advance(401.0)
        assert limiter.check("1.2.3.4").allowed
        assert limiter.current_count("1.2.3.4") == 1


class TestIsolation:
    def test_distinct_ips_have_independent_counters(self) -> None:
        limiter, _ = _limiter(limit=1)
        limiter.record("1.1.1.1")
        assert not limiter.check("1.1.1.1").allowed
        assert limiter.check("2.2.2.2").allowed

    def test_check_does_not_consume_quota(self) -> None:
        """判定与计数分开：因地址非法被拒的请求不该吃掉配额。"""
        limiter, _ = _limiter(limit=1)
        for _ in range(5):
            assert limiter.check("1.2.3.4").allowed
        assert limiter.current_count("1.2.3.4") == 0


class TestClientKey:
    def test_prefers_leftmost_forwarded_for(self) -> None:
        """链路上每跳向右追加，最左是原始客户端。取最右会让全站共用一个配额。"""
        assert client_key("203.0.113.7, 10.0.0.1, 10.0.0.2", "10.0.0.2") == "203.0.113.7"

    def test_falls_back_to_direct_host(self) -> None:
        assert client_key(None, "198.51.100.9") == "198.51.100.9"
        assert client_key("", "198.51.100.9") == "198.51.100.9"

    def test_blank_forwarded_for_falls_back(self) -> None:
        assert client_key("   ,10.0.0.1", "198.51.100.9") == "198.51.100.9"

    def test_unknown_when_nothing_available(self) -> None:
        assert client_key(None, None) == "unknown"


class TestRefund:
    """网络类失败退还配额。

    起因：克隆在后台任务里跑，而限流在提交阶段就计了数。一次 TLS 断连（我们这侧的问题）
    会白扣用户一次配额——按 3 次/小时，三次抖动就把人锁一小时。
    """

    def test_refund_frees_one_slot(self) -> None:
        limiter, _ = _limiter(limit=1)
        limiter.record("1.2.3.4")
        assert not limiter.check("1.2.3.4").allowed

        assert limiter.refund("1.2.3.4") is True
        assert limiter.check("1.2.3.4").allowed

    def test_refund_on_unknown_key_is_noop(self) -> None:
        limiter, _ = _limiter()
        assert limiter.refund("never-seen") is False

    def test_refund_does_not_go_negative(self) -> None:
        """退多于记的次数不该把计数搞成负数或抛异常。"""
        limiter, _ = _limiter()
        limiter.record("1.2.3.4")
        assert limiter.refund("1.2.3.4") is True
        assert limiter.refund("1.2.3.4") is False
        assert limiter.current_count("1.2.3.4") == 0

    def test_refund_only_affects_that_client(self) -> None:
        limiter, _ = _limiter(limit=1)
        limiter.record("1.1.1.1")
        limiter.record("2.2.2.2")
        limiter.refund("1.1.1.1")

        assert limiter.check("1.1.1.1").allowed
        assert not limiter.check("2.2.2.2").allowed
