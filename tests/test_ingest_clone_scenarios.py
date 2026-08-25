"""克隆场景（U2 的 Test scenarios 三条）。

计划列的三条此前都没有测试：合法仓库完成浅克隆、含子模块的仓库克隆后子模块目录为空、
克隆后 `git rev-list --count HEAD` 大于 1。已覆盖的只是纯函数部分（地址解析、准入阈值、
HTTP 状态映射）。基准仓库实跑时克隆确实成功过，但那是实跑观察，不是测试；子模块那条
完全没验过。

**为什么用本地仓库而非打网络。** backlog 给了两个选项，本地裸仓库更可控：不依赖 GitHub
可达性、不占速率额度、不因网络波动变成随机失败。代价是不覆盖真实 HTTPS 传输——那一层
由 `test_ingest_github_api.py` 的状态映射测试与基准仓库实跑覆盖。

`RepoRef.clone_url` 硬编码 github.com，所以这里 monkeypatch 掉那个 property 指向本地
路径。被换掉的只有「去哪儿取」，`clone_repo` 的浅克隆参数、子模块开关、清理与计数逻辑
全部照原样跑。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.ingest.clone import clone_repo
from backend.ingest.guards import RepoRef
from tests.support import make_settings


def _git(*args: str, cwd: Path) -> str:
    """跑一条 git 命令。失败直接抛，让构造阶段的问题立刻可见。"""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        # 隔离全局配置：开发机上的 commit.gpgsign、user.name 缺失或 hooks 都会让构造失败。
        env={
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "PATH": __import__("os").environ.get("PATH", ""),
        },
    )
    return result.stdout.strip()


def _make_repo(root: Path, name: str, commits: int) -> Path:
    """造一个带若干提交的本地仓库。"""
    repo = root / name
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    for index in range(commits):
        (repo / f"file{index}.py").write_text(f"VALUE = {index}\n", encoding="utf-8")
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", f"c{index}", cwd=repo)
    return repo


class TestCloneScenarios:
    async def test_shallow_clone_succeeds_and_counts_commits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """合法仓库完成浅克隆，且提交数大于 1。

        提交数这条不是凑数：`clone_depth` 若被改回 1，提交频次信号（KTD10 依赖它）会
        恒为空，而界面与报告都看不出异常。这里钉住「深度不是 1」这个决定。
        """
        upstream = _make_repo(tmp_path / "src", "upstream", commits=5)
        settings = make_settings(workspace_root=tmp_path / "ws")
        ref = RepoRef(owner="acme", name="upstream")
        monkeypatch.setattr(
            RepoRef, "clone_url", property(lambda _self: upstream.as_uri()), raising=True
        )

        result = await clone_repo(ref, settings)

        assert result.workdir.is_dir()
        assert (result.workdir / "file0.py").is_file()
        assert len(result.head_sha) == 40
        assert result.commit_count > 1
        assert result.commit_count == 5

    async def test_submodule_directory_stays_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """含子模块的仓库克隆后子模块目录为空。

        `--no-recurse-submodules` 的效果必须能被观察到：子模块会显著放大克隆体积与耗时，
        而它们的代码不属于被分析仓库的作者意图。目录存在但为空是正确结果——不是不存在，
        因为 git 仍会建出挂载点。
        """
        sub = _make_repo(tmp_path / "src", "sub", commits=2)
        upstream = _make_repo(tmp_path / "src", "withsub", commits=2)
        _git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            sub.as_uri(),
            "vendor/sub",
            cwd=upstream,
        )
        _git("commit", "-q", "-m", "add submodule", cwd=upstream)

        settings = make_settings(workspace_root=tmp_path / "ws")
        ref = RepoRef(owner="acme", name="withsub")
        monkeypatch.setattr(
            RepoRef, "clone_url", property(lambda _self: upstream.as_uri()), raising=True
        )

        result = await clone_repo(ref, settings)

        mount = result.workdir / "vendor" / "sub"
        assert mount.is_dir(), "子模块挂载点仍应存在——git 会建出目录"
        assert list(mount.iterdir()) == [], "子模块内容不应被拉取"
        # 主仓库自身的文件不受影响。
        assert (result.workdir / "file0.py").is_file()

    async def test_stale_workdir_is_replaced_not_merged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """已存在的残留工作目录被整体重来，不与新克隆混在一起。

        残留的半成品克隆会让解析层读到不完整的树，且难以察觉——这条钉住 `_remove_tree`
        在克隆前真的跑了。
        """
        upstream = _make_repo(tmp_path / "src", "fresh", commits=2)
        settings = make_settings(workspace_root=tmp_path / "ws")
        ref = RepoRef(owner="acme", name="fresh")

        stale = settings.repos_dir / ref.workdir_name
        stale.mkdir(parents=True)
        (stale / "leftover.py").write_text("STALE = 1\n", encoding="utf-8")

        monkeypatch.setattr(
            RepoRef, "clone_url", property(lambda _self: upstream.as_uri()), raising=True
        )

        result = await clone_repo(ref, settings)

        assert not (result.workdir / "leftover.py").exists()
        assert (result.workdir / "file0.py").is_file()
