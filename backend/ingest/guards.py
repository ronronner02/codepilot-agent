"""克隆前的准入检查（KTD12）。

为什么在克隆前查而不是克隆后：克隆是不可撤销的网络与磁盘开销。一个 2GB 的仓库
克隆完再拒绝，代价已经付出了。GitHub API 的仓库元数据里有体积字段，一次请求就能
判断。

这是系统唯一的不可信输入面。用户提交的地址可以指向任意公开仓库。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse
from pathlib import Path

import re

class RejectReason(str, Enum):
    """拒绝原因需要可区分——界面要按 AE6 分别呈现，不能统一报「失败」。"""

    INVALID_URL = "invalid_url"
    NOT_FOUND = "not_found"
    NO_ACCESS = "no_access"
    TOO_LARGE = "too_large"
    NETWORK_ERROR = "network_error"


class RepoRejected(Exception):
    def __init__(self, reason: RejectReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class RepoRef:
    """规范化后的仓库标识。owner/name 同时用于克隆与缓存键。"""

    owner: str
    name: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}.git"

    @property
    def workdir_name(self) -> str:
        """磁盘目录名。owner 与 name 都已通过校验，不含路径分隔符。"""
        return f"{self.owner}__{self.name}"


def parse_repo_url(raw: str) -> RepoRef:
    """把用户输入的地址解析为 RepoRef，无法解析则抛 RepoRejected(INVALID_URL)。

    ── 待你实现（U2 核心逻辑）──────────────────────────────────────────

    这个函数是不可信输入的第一道门。解析出的 owner/name 会流向两个危险位置：
    拼进 clone URL，和拼成磁盘目录名。所以校验不只是「能不能解析」，还要保证
    解析结果不含能改变这两处语义的字符。

    必须拒绝的形态（都要有对应测试）：

    - `..` 作为 owner 或 name —— 会让 workdir_name 变成 `..__x`，虽然不含分隔符
      但语义可疑；更重要的是 name 为 `..` 时 clone URL 也失去意义。
    - 含 `/`、`\\`、`:` 的段 —— 直接改变目录层级或盘符。
    - 空段（`github.com//repo`）。
    - 非 GitHub 主机（`gitlab.com/x/y`、`evil.com/github.com/x/y`）。计划的范围
      是公开 GitHub 仓库，别的主机在这一版明确拒绝而不是静默尝试。
    - 以 `-` 开头的段 —— 会被 git 当成命令行选项。这条容易漏，但 owner 名
      `--upload-pack=...` 之类的输入正是命令注入的经典入口。

    GitHub 自身的命名规则：owner 与 repo 都是字母数字加 `-`、`_`、`.`，且不以
    `-` 开头。按这个规则收紧比逐一枚举攻击形态更可靠。

    要你决定的是接受哪些输入形态。至少支持第一种，其余按你的判断：

    1. `https://github.com/owner/repo` —— 必须支持，最常见
    2. 带 `.git` 后缀 —— 一行 strip 的事，建议支持
    3. 带尾部路径 `/tree/main`、`/blob/main/x.py` —— 用户从浏览器地址栏直接粘贴
       时很常见。支持它就要决定「截断到 owner/repo」还是「拒绝」
    4. `git@github.com:owner/repo.git` SSH 形态 —— 本项目只读公开仓库，SSH 没有
       必要，但用户可能习惯性粘贴
    5. 裸 `owner/repo` —— 最省事的输入，但与相对路径形态难以区分

    我的建议：支持 1、2、3，拒绝 4、5。理由是 1-3 都是「浏览器里能看到的东西」，
    用户预期它们可用；4 引入 SSH 语义但本项目不需要认证；5 与本地路径歧义，而且
    将来若支持本地仓库分析会撞车。

    返回：RepoRef。不做网络请求——存在性与体积由 check_repo_admissible 负责。

    对应测试：tests/test_ingest_guards.py
    """
    url = urlparse(raw)
    if url.scheme not in ("https", "http") or url.hostname != "github.com":
        raise RepoRejected(RejectReason.INVALID_URL, f"无效的 GitHub URL:{raw}")

    path_parts = url.path.strip("/").split("/")
    if len(path_parts) < 2:
        raise RepoRejected(RejectReason.INVALID_URL, f"URL 不包含 owner/repo:{raw}")

    owner, name = path_parts[0], path_parts[1]
    name = name.removesuffix(".git")

    # 校验 owner 和 name
    for part in (owner, name):
        if not part.strip("."):
            raise RepoRejected(RejectReason.INVALID_URL, f"段无效:{part}")
        
        """
        any()有一个为true，整体为true,
        for c in "/\\:"外层循环判断字符，\\:是转义字符，表示\
        """ 
        if not part or part.startswith("-") or ".." in part or any(c in part for c in "/\\:"): 
            raise RepoRejected(RejectReason.INVALID_URL, f"无效的 owner/repo 段:{part}")

        if not re.fullmatch(r"[A-Za-z0-9._-]+", part):
            raise RepoRejected(RejectReason.INVALID_URL, f"owner/repo 段包含非法字符:{part}")


    return RepoRef(owner=owner, name=name)
