"""路径校验（KTD14）。所有读文件路径必经此处。

为什么集中在单一函数：解析层、工具层、MCP 工具三处都要校验，分散实现漏一处即
失效。克隆下来的仓库可以包含指向工作目录之外的符号链接——跟随它就能读出仓库外
的文件内容。

KTD15 要求 MCP 边界复用同一校验且不放宽。
"""

from __future__ import annotations

from pathlib import Path
import os

class PathEscapeError(ValueError):
    """请求的路径解析后落在允许的根目录之外。"""

    def __init__(self, requested: Path, root: Path) -> None:
        super().__init__(f"路径逃逸：{requested} 不在 {root} 之内")
        self.requested = requested
        self.root = root


def _is_reparse_point(path: Path) -> bool:
    """路径这一段是否是符号链接或 Windows 目录联接（junction）。

    为什么不能只用 `is_symlink()`：实测确认它对 junction 返回 False，要用
    `is_junction()`。而 `os.path.normpath` 也不解析 reparse point，于是 junction
    在字面上仍位于 root 之内——逐段检查与包含判断会双双放行，构成真实的逃逸漏洞
    （实测通过 `src/leakdir/secret.txt` 读出了仓库外文件内容）。

    junction 比符号链接更需要防：它在 Windows 上免提权即可创建，而本函数同时是
    MCP 的外部边界（KTD15），那里的输入不可信程度更高。

    `is_junction()` 是 3.12 引入的；3.11 上用 `os.readlink` 兜底——它对 junction
    也能成功返回目标，对普通文件抛 OSError。
    """
    if path.is_symlink():
        return True
    if hasattr(path, "is_junction"):
        return bool(path.is_junction())
    try:
        os.readlink(path)
    except OSError:
        return False
    return True


def resolve_within(root: Path, candidate: str | Path) -> Path:
    """把 candidate 解析为真实路径并确认它在 root 之内，否则抛 PathEscapeError。

    策略：一律拒绝 reparse point（符号链接与 junction）。路径上任何一段是就拒，
    不看它指向哪里。

    为什么不看指向哪里：「只拒指向外部的」需要先解析链接再判断，而解析与随后的读取
    之间存在 TOCTOU 窗口——攻击者可在两步之间改写链接目标。一律拒绝没有这个窗口，
    代价是仓库内的合法链接也用不了，对代码分析场景可以接受。

    四步：

    1. 归一化 root，作为包含判断的基准。
    2. 相对路径拼到 root 之下；绝对路径从 anchor 起走，但仍须过第 3、4 步——
       绝对路径不代表合法。
    3. 逐段检查 reparse point。只检查 candidate 引入的段：root 自身已 resolve 过，
       若 root 位于某个符号链接之下（macOS 的 /tmp、部分 CI 环境常见），连带检查
       会把全部合法请求拒掉。
    4. 用 `is_relative_to` 判断包含，不做字符串前缀比较——`/repo-evil` 以 `/repo`
       为前缀却不在其内。

    返回前不调用 `.resolve()`：第 3 步已保证路径上无 reparse point，再 resolve 多余；
    且它对不存在路径的跨平台行为有差异，而本函数不做存在性检查——调用方需要区分
    「路径非法」与「文件不存在」，后者应正常返回。
    """
    resolved_root = root.resolve()
    candidate_path = Path(candidate)

    if candidate_path.is_absolute():
        # 从 anchor（Windows 的盘符、POSIX 的 /）起走，跳过 anchor 自身。
        current_path = Path(candidate_path.anchor)
        parts = candidate_path.parts[1:]
    else:
        current_path = resolved_root
        parts = candidate_path.parts

    for part in parts:
        current_path = current_path / part
        if _is_reparse_point(current_path):
            raise PathEscapeError(candidate_path, resolved_root)

    normalized = Path(os.path.normpath(current_path))

    if not normalized.is_relative_to(resolved_root):
        raise PathEscapeError(candidate_path, resolved_root)

    return normalized
