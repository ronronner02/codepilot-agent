"""仓库工作副本的磁盘配额与 LRU 清理（U7，R-55）。

**只清仓库副本，不清索引与落盘历史**（KTD10）。三者的失效条件不同：

- 仓库副本可重新克隆，删了只损失克隆的时间与带宽；
- 向量索引删了会让 Code Search 与 AI Chat 一并失效，而它的重建成本是 embedding 额度；
- 落盘历史删了历史列表就空了，而那是「这次分析发生过」的唯一记录。

只有第一项能靠重新获取恢复且不花钱，所以「腾磁盘」的必要范围就止于它。被清理的仓库
重新分析时索引缓存仍命中（键是 repo + commit_sha + provider_identity，不含工作副本
路径），所以 embedding 不重算。

**用目录 mtime 而非 atime 表达「最后使用」。** 多数生产文件系统挂载时带 `noatime` 或
`relatime`，atime 要么不更新要么只按天更新——按它排序等于按随机顺序删。分析完成时主动
刷一次目录 mtime，使它表达「最后被分析或被查看的时间」。

**回收量下限**（KTD6）是这个模块最不直观的一条：可回收量低于配额的一定比例时**不清理**，
让本次分析因磁盘不足而失败。这防的是「清理与克隆互相追赶」——配额贴顶时每次新分析清掉
上一个副本，下次分析同一仓库又要重新克隆，磁盘始终贴顶而克隆成本被反复付出。宁可让配置
错误（配额太小以致放不下两个仓库）以明确失败暴露，也不做一次收益微小的清理。
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from backend.config import Settings

logger = logging.getLogger("codepilot.workspace.quota")

# 统计体积时跳过的目录。与 mcp_server.repos.SKIP_DIRS 同源的判断，但这里只用于**统计**：
# 清理时删的是整个仓库目录，不做选择性删除。
#
# 为什么统计要跳过：.git 在浅克隆下仍可能占大头，而它与工作树一起被删，重复计入不影响
# 排序但会让「用量」这个给人看的数字偏离实际可回收量。这里的取舍是让 usage 反映**可回收
# 的量**，所以 .git 也计入——它确实会被 rmtree 删掉。真正跳过的只有不属于该副本的东西。
_SKIP_NAMES = frozenset({"node_modules", "__pycache__", ".venv", "venv"})


@dataclass(frozen=True)
class RepoUsage:
    """一个仓库副本的用量记录。"""

    path: Path
    size_bytes: int
    last_used: float


@dataclass
class QuotaUsage:
    """当前用量快照。`/api/health` 直接输出它（KTD11 的第三个信号）。"""

    total_bytes: int = 0
    quota_bytes: int = 0
    repo_count: int = 0
    last_reclaimed_bytes: int = 0
    repos: list[RepoUsage] = field(default_factory=list)

    @property
    def over_quota(self) -> bool:
        return self.total_bytes > self.quota_bytes


@dataclass(frozen=True)
class QuotaOutcome:
    """一次配额检查的结果。

    `admitted` 为假时本次分析不该开始——磁盘放不下且清不出空间。`note` 是给用户看的说明，
    要说清是配额太小还是可清的都在跑，两者的下一步动作不同。
    """

    admitted: bool
    reclaimed_bytes: int = 0
    removed: tuple[str, ...] = ()
    note: str = ""


# 最近一次清理的回收量。`/api/health` 读它（KTD11）。
#
# 模块级变量而非 Settings 字段：它是运行时观测值，不是配置。单 worker 下进程内即全局。
_last_reclaimed_bytes = 0


def _dir_size(path: Path) -> int:
    """目录体积。跳过 _SKIP_NAMES，忽略读取失败的条目。

    不用 `sum(f.stat().st_size for f in path.rglob('*'))`：rglob 会穿透 reparse point，
    一个指向系统盘的 junction 就能让统计跑到几百 GB 并卡住几分钟。这里显式走 iterdir
    并跳过链接。
    """
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            for child in current.iterdir():
                if child.name in _SKIP_NAMES:
                    continue
                try:
                    # is_symlink 挡符号链接；junction 在 Windows 上 is_dir 为真而
                    # is_symlink 为假，所以另比对 resolve 后是否还在 path 内。
                    if child.is_symlink():
                        continue
                    if child.is_dir():
                        if not child.resolve().is_relative_to(path.resolve()):
                            continue
                        stack.append(child)
                    else:
                        total += child.stat().st_size
                except OSError:
                    continue
        except OSError:
            continue
    return total


def quota_usage(settings: Settings) -> QuotaUsage:
    """扫描仓库目录，给出用量快照。"""
    usage = QuotaUsage(
        quota_bytes=settings.repos_quota_bytes,
        last_reclaimed_bytes=_last_reclaimed_bytes,
    )
    repos_dir = settings.repos_dir
    if not repos_dir.is_dir():
        return usage

    records: list[RepoUsage] = []
    try:
        children = sorted(repos_dir.iterdir())
    except OSError:
        return usage

    for child in children:
        if not child.is_dir() or child.is_symlink():
            continue
        try:
            last_used = child.stat().st_mtime
        except OSError:
            continue
        records.append(
            RepoUsage(path=child, size_bytes=_dir_size(child), last_used=last_used)
        )

    usage.repos = records
    usage.repo_count = len(records)
    usage.total_bytes = sum(r.size_bytes for r in records)
    return usage


def touch_repo(path: Path) -> None:
    """刷新仓库目录的 mtime，表达「刚被使用」。

    分析完成时调用。不刷的话一个被反复分析的仓库会因为 mtime 停在首次克隆时刻而最先
    被清理——那正好是最该留下的那个。
    """
    try:
        path.touch(exist_ok=True)
    except OSError as exc:
        # 刷新失败只影响清理顺序的优劣，不影响正确性，所以不上抛。
        logger.debug("刷新 %s 的 mtime 失败：%s", path, exc)


def ensure_repo_quota(settings: Settings, running_repos: set[str]) -> QuotaOutcome:
    """未超配额直接放行；超了则按 LRU 清理非在跑副本。

    `running_repos` 是在跑任务的 workdir 路径集合。在跑副本永不清理——清掉正在分析的
    仓库会让那次分析以一个莫名的文件缺失错误失败。判定读任务注册表而非用文件锁：单
    worker 下注册表就是全局真相（KTD6），文件锁还要处理残留锁的清理。
    """
    global _last_reclaimed_bytes

    usage = quota_usage(settings)
    if not usage.over_quota:
        return QuotaOutcome(admitted=True)

    running = {str(Path(p).resolve()) for p in running_repos}
    reclaimable = [r for r in usage.repos if str(r.path.resolve()) not in running]

    if not reclaimable:
        # 配额触发但唯一可清的都是在跑仓库：不清理，让新任务照常建立。
        # 磁盘超一点比清掉正在分析的仓库损失小。
        logger.warning(
            "磁盘配额已超（%d/%d 字节）但可清理的副本都在分析中，本次不清理",
            usage.total_bytes,
            usage.quota_bytes,
        )
        return QuotaOutcome(admitted=True, note="配额已超但无可清理副本（均在分析中）")

    # 回收量下限（KTD6）。低于它就不做这次清理。
    threshold = int(usage.quota_bytes * settings.quota_min_reclaim_ratio)
    available = sum(r.size_bytes for r in reclaimable)
    if available < threshold:
        note = (
            f"磁盘配额已超（{usage.total_bytes} / {usage.quota_bytes} 字节），"
            f"但可回收量 {available} 字节低于下限 {threshold} 字节，本次不清理。"
            f"这通常意味着配额过小，放不下两个仓库副本——请调大 REPOS_QUOTA_BYTES。"
        )
        logger.warning("%s", note)
        return QuotaOutcome(admitted=False, note=note)

    # 最久未使用的先删。删到回到配额以下即停——多删没有好处，那些副本还能被复用。
    reclaimable.sort(key=lambda r: r.last_used)
    reclaimed = 0
    removed: list[str] = []
    remaining = usage.total_bytes
    for record in reclaimable:
        if remaining <= usage.quota_bytes:
            break
        try:
            shutil.rmtree(record.path)
        except OSError as exc:
            logger.warning("清理 %s 失败：%s", record.path, exc)
            continue
        reclaimed += record.size_bytes
        remaining -= record.size_bytes
        removed.append(record.path.name)

    _last_reclaimed_bytes = reclaimed
    logger.info(
        "配额清理回收 %d 字节，删除 %d 个副本：%s",
        reclaimed,
        len(removed),
        ", ".join(removed) or "无",
    )
    return QuotaOutcome(
        admitted=True,
        reclaimed_bytes=reclaimed,
        removed=tuple(removed),
        note=f"已回收 {reclaimed} 字节，删除 {len(removed)} 个副本",
    )


def reset_last_reclaimed() -> None:
    """清零最近回收量。仅供测试——模块级状态会在用例之间串流。"""
    global _last_reclaimed_bytes
    _last_reclaimed_bytes = 0
