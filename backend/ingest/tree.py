"""克隆前的文件清单。用 trees API 拿精确文件数，不用 size 估算。

为什么不用 `size`：实测 KB/文件 的比值在 18–40 之间摆动（click 31、fastapi 18、
django 40），因为 size 计的是 git 对象总量含全部历史，与工作树文件数没有稳定关系。
trees API 一次请求给出精确清单。

代价是每次分析多一个 API 请求（共两个）。未认证速率 60 次/小时，够用但开发期反复
测试要留意。
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from backend.config import Settings
from backend.ingest.github_api import build_headers, map_error_status
from backend.ingest.guards import RejectReason, RepoRef, RepoRejected

# 计入准入规模门的源码扩展名。这是成本代理指标，不是「解析层能解析的集合」——
# 后者是 parser.py 的 SUFFIX_TO_GRAMMAR，只含 Python 与 TypeScript。
#
# 两者为什么不同：语言范围决定「其他语言的文件计入文件树与规模统计，但不做符号级
# 解析」。JS 属于后一类，仍是仓库规模的一部分（影响 clone 体积与文件树遍历成本），
# 故计入规模门；但它不进符号表、不切块、不向量化。
#
# 这个偏差方向是保守的：JS 多的仓库会被算得比实际解析成本更大，宁可多拒不可少拒。
# 若日后要让门限只反映真实解析成本，需重新在基准仓库上调 max_parseable_files，
# 不能只删后缀——阈值 1500 是按含 JS 的口径实测调出来的。
SCALE_COUNTED_SUFFIXES = frozenset(
    {".py", ".pyi", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
)


@dataclass(frozen=True)
class FileInventory:
    total_files: int
    parseable_files: int
    truncated: bool
    """GitHub 在约 10 万条时截断。截断本身就说明仓库远超阈值。"""


async def fetch_inventory(
    ref: RepoRef, default_branch: str, settings: Settings
) -> FileInventory:
    url = (
        f"https://api.github.com/repos/{ref.owner}/{ref.name}"
        f"/git/trees/{default_branch}?recursive=1"
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=build_headers(settings))
    except httpx.HTTPError as exc:
        raise RepoRejected(
            RejectReason.NETWORK_ERROR, f"无法读取仓库文件清单：{exc}"
        ) from exc

    if response.status_code != 200:
        raise map_error_status(
            response.status_code, ref, authenticated=bool(settings.github_token)
        )

    payload = response.json()
    blobs = [item for item in payload.get("tree", []) if item.get("type") == "blob"]
    parseable = sum(
        1
        for item in blobs
        if any(str(item.get("path", "")).endswith(s) for s in SCALE_COUNTED_SUFFIXES)
    )
    return FileInventory(
        total_files=len(blobs),
        parseable_files=parseable,
        truncated=bool(payload.get("truncated")),
    )
