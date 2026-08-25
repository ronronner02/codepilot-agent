"""有界浅克隆（KTD12）。

三道防线，任一单独都够用，冗余是有意的：
1. `parse_repo_url` 拒绝以 `-` 开头的段（选项注入的经典入口）
2. `subprocess` 传列表而非 shell 字符串——没有 shell 解析，就没有 shell 注入
3. `--` 分隔符让 git 不把后续参数当选项

深度不设 1：实测 depth 1 只有 1 个提交、提交频次信号恒为空（KTD10 依赖它）；
depth 200 在中型仓库拿到上千提交（depth 限制每条父链深度，合并提交产生多条链），
体积仅增约 2M。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from backend.config import Settings
from backend.ingest.guards import RejectReason, RepoRef, RepoRejected


@dataclass(frozen=True)
class CloneResult:
    workdir: Path
    head_sha: str
    commit_count: int


def _clear_readonly(func: Callable[[str], object], path: str, _exc: object) -> None:
    """rmtree 的错误处理：清掉只读位后重试。

    git 把 `.git/objects` 下的对象文件设为只读。Windows 的 unlink 会因此失败
    （POSIX 只看目录权限，所以这个问题只在 Windows 上出现）。
    """
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def _remove_tree(target: Path) -> None:
    """删除目录树，并校验删掉了。

    不用 `ignore_errors=True`：它会把「删不掉」静默吞掉，留下残留目录，随后 git 报
    「destination path already exists and is not an empty directory」——错误信息离
    根因很远。实测踩到过：同一仓库的二次分析在 Windows 上必然失败。
    """
    if not target.exists():
        return

    # onerror 在 3.12 起弃用，改名 onexc；两者签名一致，按版本选参数名。
    if sys.version_info >= (3, 12):
        shutil.rmtree(target, onexc=_clear_readonly)  # type: ignore[call-arg]
    else:
        shutil.rmtree(target, onerror=_clear_readonly)  # type: ignore[call-arg]

    if target.exists():
        raise RepoRejected(
            RejectReason.NETWORK_ERROR,
            f"无法清理既有工作目录 {target}——可能有进程正占用其中的文件",
        )


def git_env() -> dict[str, str]:
    """git 子进程的环境。

    公开而非私有：churn 统计（U11）也要跑 git，而这里的两个设置是安全相关的，
    在两处各写一份就会漂移。
    """
    env = dict(os.environ)
    # 无凭据时不要挂在交互提示上。GitHub 对私有仓库直接 404 不问凭据，但换成
    # 自建 GitLab 之类的主机会阻塞到超时。
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    return env


async def _run_git(args: list[str], cwd: Path | None, timeout: float) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd) if cwd else None,
        env=git_env(),
        # 断开 stdin：继承父进程的 stdin 在 stdio 服务里会吞协议字节，
        # 而 git 在 GIT_TERMINAL_PROMPT=0 下本就不需要它。
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RepoRejected(
            RejectReason.NETWORK_ERROR, f"git 操作超时（{timeout:.0f}s）：{' '.join(args)}"
        ) from None
    return process.returncode or 0, stdout.decode("utf-8", errors="replace")


async def clone_repo(
    ref: RepoRef, settings: Settings, timeout: float = 180.0
) -> CloneResult:
    workdir = settings.repos_dir / ref.workdir_name
    workdir.parent.mkdir(parents=True, exist_ok=True)
    # 残留的半成品克隆会让后续解析读到不完整的树，且难以察觉。整体重来。
    _remove_tree(workdir)

    code, output = await _run_git(
        [
            "clone",
            "--depth",
            str(settings.clone_depth),
            "--no-recurse-submodules",
            "--quiet",
            "--",
            ref.clone_url,
            str(workdir),
        ],
        cwd=None,
        timeout=timeout,
    )
    if code != 0:
        # 失败清理用 ignore_errors：此时已经在报错路径上，清理失败不应掩盖真正的
        # 克隆错误（那才是用户需要看到的信息）。
        shutil.rmtree(workdir, ignore_errors=True)
        raise RepoRejected(
            RejectReason.NOT_FOUND,
            f"克隆失败：{ref.slug}。git 输出：{output.strip()[:300]}",
        )

    _, sha_out = await _run_git(["rev-parse", "HEAD"], cwd=workdir, timeout=30.0)
    _, count_out = await _run_git(
        ["rev-list", "--count", "HEAD"], cwd=workdir, timeout=30.0
    )
    return CloneResult(
        workdir=workdir,
        head_sha=sha_out.strip(),
        commit_count=int(count_out.strip() or 0),
    )
