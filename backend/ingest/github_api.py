"""克隆前的 GitHub 元数据预检。

为什么预检：克隆是不可撤销的网络与磁盘开销，2GB 的仓库克隆完再拒绝，代价已经付
出了。一次 API 请求就能判断。

私有与不存在的区分：GitHub 对未认证请求把两者都返回 404，这是有意设计——若私有
仓库返回 403，就能枚举出「哪些私有仓库存在」。带 token 时才区分（403/404）。所以
`NO_ACCESS` 只在配置了 token 时可能出现。
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from backend.config import Settings
from backend.ingest.guards import RejectReason, RepoRef, RepoRejected


@dataclass(frozen=True)
class RepoMetadata:
    """预检拿到的元数据。size_kb 是 git 对象总体积，非工作树体积。"""

    size_kb: int
    default_branch: str
    archived: bool
    is_fork: bool
    primary_language: str | None


def build_headers(settings: Settings) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return headers


def map_error_status(status: int, ref: RepoRef, authenticated: bool) -> RepoRejected:
    if status == 404:
        if authenticated:
            detail = f"仓库不存在：{ref.slug}"
        else:
            # 无 token 时无法区分，文案必须诚实——否则用户会以为自己拼错了地址，
            # 而实际可能是私有仓库。
            detail = (
                f"仓库不可访问：{ref.slug}。可能不存在、已删除，或为私有仓库。"
                f"配置 GITHUB_TOKEN 后可区分这两种情况。"
            )
        return RepoRejected(RejectReason.NOT_FOUND, detail)
    if status == 403:
        # 带 token 时的 403 才是真正的「无权限」。也可能是速率超限，需看响应体。
        return RepoRejected(
            RejectReason.NO_ACCESS, f"无访问权限或已超出 API 速率上限：{ref.slug}"
        )
    if status == 451:
        return RepoRejected(
            RejectReason.NO_ACCESS, f"仓库因法律原因不可用：{ref.slug}"
        )
    return RepoRejected(
        RejectReason.NETWORK_ERROR, f"GitHub API 返回 HTTP {status}：{ref.slug}"
    )


async def fetch_metadata(ref: RepoRef, settings: Settings) -> RepoMetadata:
    """取仓库元数据。不做体积判断——那是 check_admissible 的职责。"""
    url = f"https://api.github.com/repos/{ref.owner}/{ref.name}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, headers=build_headers(settings))
    except httpx.HTTPError as exc:
        raise RepoRejected(
            RejectReason.NETWORK_ERROR, f"无法连接 GitHub API：{exc}"
        ) from exc

    if response.status_code != 200:
        raise map_error_status(
            response.status_code, ref, authenticated=bool(settings.github_token)
        )

    payload = response.json()
    return RepoMetadata(
        size_kb=int(payload.get("size", 0)),
        default_branch=str(payload.get("default_branch") or "main"),
        archived=bool(payload.get("archived")),
        is_fork=bool(payload.get("fork")),
        primary_language=payload.get("language"),
    )
