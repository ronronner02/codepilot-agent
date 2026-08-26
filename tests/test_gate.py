"""并发闸门与排队（U6，R-54、AE-15）。

闸门是纯同步数据结构，所以这套测试不涉及协程——调度由 TaskRegistry 驱动，那一层的
行为在 tests/test_api.py 的排队用例里断言。
"""

from __future__ import annotations

import pytest

from backend.api.gate import ConcurrencyGate, QueueFull


def _gate(max_concurrent: int = 2, max_queued: int = 8) -> ConcurrencyGate:
    return ConcurrencyGate(max_concurrent=max_concurrent, max_queued=max_queued)


class TestAdmission:
    def test_admits_up_to_limit(self) -> None:
        gate = _gate(max_concurrent=2)
        assert gate.acquire("a").admitted
        assert gate.acquire("b").admitted
        assert gate.running_count == 2

    def test_third_task_queues_with_position(self) -> None:
        gate = _gate(max_concurrent=2)
        gate.acquire("a")
        gate.acquire("b")

        admission = gate.acquire("c")
        assert not admission.admitted
        assert admission.position == 1
        assert gate.queued_count == 1

    def test_queue_positions_are_sequential(self) -> None:
        gate = _gate(max_concurrent=1)
        gate.acquire("a")
        assert gate.acquire("b").position == 1
        assert gate.acquire("c").position == 2
        assert gate.acquire("d").position == 3

    def test_acquire_is_idempotent(self) -> None:
        """重复申请同一个 task_id 不该把它排两次。"""
        gate = _gate(max_concurrent=1)
        gate.acquire("a")
        gate.acquire("b")
        again = gate.acquire("b")
        assert not again.admitted
        assert again.position == 1
        assert gate.queued_count == 1

    def test_queue_full_raises(self) -> None:
        """队列本身也是资源：排到几百位的等待没有意义。"""
        gate = _gate(max_concurrent=1, max_queued=2)
        gate.acquire("a")
        gate.acquire("b")
        gate.acquire("c")
        with pytest.raises(QueueFull):
            gate.acquire("d")


class TestRelease:
    def test_release_promotes_queue_head(self) -> None:
        gate = _gate(max_concurrent=1)
        gate.acquire("a")
        gate.acquire("b")
        gate.acquire("c")

        assert gate.release("a") == "b"
        assert gate.is_running("b")
        assert gate.position_of("c") == 1

    def test_release_returns_none_when_queue_empty(self) -> None:
        gate = _gate(max_concurrent=2)
        gate.acquire("a")
        assert gate.release("a") is None
        assert gate.running_count == 0

    def test_release_of_failed_task_still_promotes(self) -> None:
        """失败的任务同样要归还名额，否则队列永久卡住。

        闸门本身不知道成功与失败——调用方在 finally 里 release。这条断言钉住的是：
        release 的语义只是「这个 task 不再占用名额」，与它为什么结束无关。
        """
        gate = _gate(max_concurrent=1)
        gate.acquire("a")
        gate.acquire("b")
        assert gate.release("a") == "b"

    def test_release_of_queued_task_removes_it(self) -> None:
        """排队期间被取消：从队列里摘掉，不占位置。"""
        gate = _gate(max_concurrent=1)
        gate.acquire("a")
        gate.acquire("b")
        gate.acquire("c")

        gate.release("b")
        assert not gate.is_queued("b")
        assert gate.position_of("c") == 1

    def test_release_does_not_overfill(self) -> None:
        """名额没空出来时不放行——两个在跑、上限二，release 一个只该放行一个。"""
        gate = _gate(max_concurrent=2)
        gate.acquire("a")
        gate.acquire("b")
        gate.acquire("c")
        gate.acquire("d")

        assert gate.release("a") == "c"
        assert gate.running_count == 2
        assert gate.queued_count == 1


class TestQuery:
    def test_position_of_unknown_is_zero(self) -> None:
        gate = _gate()
        assert gate.position_of("nope") == 0

    def test_queued_ids_preserve_order(self) -> None:
        gate = _gate(max_concurrent=1)
        gate.acquire("a")
        for name in ("b", "c", "d"):
            gate.acquire(name)
        assert gate.queued_ids() == ["b", "c", "d"]
