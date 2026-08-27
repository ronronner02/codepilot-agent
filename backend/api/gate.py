"""并发闸门与排队（U6，R-54）。

**超上限时排队而非拒绝。** 限流针对的是滥用（同一 IP 反复提交），闸门针对的是容量
（机器同时只能跑这么多）。后者不是用户的错，拒绝它等于把调度问题推给用户去轮询重试。

**为什么是纯同步的数据结构而不是 asyncio.Semaphore。** 信号量能挡住超额的协程，但它不
暴露「你排在第几位」——而 R-54 明确要求显示排队位置。用显式的队列换来可查询的状态，代价
是调度要由调用方（TaskRegistry）驱动：拿到许可才启动，任务终止时归还许可并放行下一个。

**为什么不用 asyncio.Queue。** 这里需要按 task_id 查位置、从中间移除（任务被取消），
而 Queue 只支持两端操作。

排队态与重启易失性叠加出一条边界：排队中的任务在进程内存里，重启后消失。它不得出现在
最近分析列表中，界面也不给「继续」入口——这是 R-46 的延伸（origin 只写了未完成任务）。
本模块只负责不把排队任务当成完成任务；列表的过滤在路由层。
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger("codepilot.api.gate")


class QueueFull(Exception):
    """队列已满。排到几百位的等待没有意义，所以队列本身也有上限。"""


@dataclass(frozen=True)
class Admission:
    """一次准入判定的结果。

    `admitted` 为真表示可立即启动；为假表示已进队列，`position` 是 1-based 的排队位置。
    """

    admitted: bool
    position: int = 0


class ConcurrencyGate:
    """在跑任务数上限与等待队列。"""

    def __init__(self, max_concurrent: int, max_queued: int) -> None:
        self._max_concurrent = max(1, max_concurrent)
        self._max_queued = max(0, max_queued)
        self._running: set[str] = set()
        self._queue: deque[str] = deque()

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def running_count(self) -> int:
        return len(self._running)

    @property
    def queued_count(self) -> int:
        return len(self._queue)

    def is_queued(self, task_id: str) -> bool:
        return task_id in self._queue

    def is_running(self, task_id: str) -> bool:
        return task_id in self._running

    def acquire(self, task_id: str) -> Admission:
        """申请一个名额。有空位就占住，没有就进队列尾部。

        重复申请同一个 task_id 是幂等的——返回它当前的状态而非把它排两次。
        """
        if task_id in self._running:
            return Admission(admitted=True)
        if task_id in self._queue:
            return Admission(admitted=False, position=self.position_of(task_id))

        if len(self._running) < self._max_concurrent:
            self._running.add(task_id)
            return Admission(admitted=True)

        if len(self._queue) >= self._max_queued:
            raise QueueFull(
                f"排队已满（{len(self._queue)}/{self._max_queued}），请稍后再提交"
            )
        self._queue.append(task_id)
        return Admission(admitted=False, position=len(self._queue))

    def release(self, task_id: str) -> str | None:
        """归还名额并放行队首。返回被放行的 task_id，队列为空时返回 None。

        **必须在任务失败时也调用**，否则一个失败的任务会永久占住名额，队列从此不再前进。
        调用方用 try/finally 保证这一点。
        """
        self._running.discard(task_id)
        # 也从队列里摘掉：任务在排队期间被取消时走的是这条路。
        if task_id in self._queue:
            self._queue.remove(task_id)

        if not self._queue:
            return None
        if len(self._running) >= self._max_concurrent:
            return None
        promoted = self._queue.popleft()
        self._running.add(promoted)
        return promoted

    def position_of(self, task_id: str) -> int:
        """1-based 排队位置。不在队列中返回 0。"""
        try:
            return self._queue.index(task_id) + 1
        except ValueError:
            return 0

    def queued_ids(self) -> list[str]:
        """按排队顺序的 task_id 列表。用于在放行后刷新其余任务的位置。"""
        return list(self._queue)
