"""双门限准入判断。

主门是可解析文件数，副门是 git 对象体积。为什么不用单一门限：
- 只看总文件数会误拒有效仓库（fastapi 共 3139 个文件，其中仅 1138 个可解析，
  其余是测试固件、文档、翻译）；解析与 embedding 的成本只跟可解析文件数相关。
- 只看体积无法预测成本（KB/文件 比值在 18–40 间摆动）。

副门的实际作用有限：`size` 报的是全量 git 对象体积，而我们做有界浅克隆，实际下载
量小得多。它拦的是「即使浅克隆也过大」的情形，比如近期提交里有大体积二进制。
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.config import Settings
from backend.ingest.github_api import RepoMetadata, fetch_metadata
from backend.ingest.guards import RejectReason, RepoRef, RepoRejected
from backend.ingest.tree import FileInventory, fetch_inventory


@dataclass(frozen=True)
class AdmissionResult:
    ref: RepoRef
    metadata: RepoMetadata
    inventory: FileInventory


def check_thresholds(
    ref: RepoRef,
    metadata: RepoMetadata,
    inventory: FileInventory,
    settings: Settings,
) -> None:
    """超限则抛 RepoRejected(TOO_LARGE)。纯判断，不做网络请求，便于单独测试。"""
    if inventory.truncated:
        raise RepoRejected(
            RejectReason.TOO_LARGE,
            f"仓库文件数超出 GitHub API 单次返回上限，远超本系统阈值：{ref.slug}",
        )
    if inventory.parseable_files > settings.max_parseable_files:
        raise RepoRejected(
            RejectReason.TOO_LARGE,
            f"可解析文件数 {inventory.parseable_files} 超出上限 "
            f"{settings.max_parseable_files}：{ref.slug}",
        )
    if metadata.size_kb > settings.max_repo_size_kb:
        raise RepoRejected(
            RejectReason.TOO_LARGE,
            f"仓库体积 {metadata.size_kb} KB 超出上限 "
            f"{settings.max_repo_size_kb} KB：{ref.slug}",
        )


async def check_admissible(ref: RepoRef, settings: Settings) -> AdmissionResult:
    """元数据 + 文件清单 + 阈值判断。任一环节不通过即抛 RepoRejected。"""
    metadata = await fetch_metadata(ref, settings)
    inventory = await fetch_inventory(ref, metadata.default_branch, settings)
    check_thresholds(ref, metadata, inventory, settings)
    return AdmissionResult(ref=ref, metadata=metadata, inventory=inventory)
