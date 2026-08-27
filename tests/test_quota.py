"""磁盘配额与 LRU 清理（U7，R-55、AE-15）。

用真实文件系统而非 mock：要验证的是体积统计与删除本身，mock 掉文件系统就只是在断言
自己写的假实现。造几 KB 的文件足够——配额在测试里也调到几 KB。
"""

from __future__ import annotations

import os
from pathlib import Path

from backend.workspace.quota import (
    ensure_repo_quota,
    quota_usage,
    reset_last_reclaimed,
    touch_repo,
)
from tests.support import junction_or_skip, make_settings


def _repo(root: Path, name: str, size_kb: int, mtime: float | None = None) -> Path:
    """造一个仓库副本目录，填指定体积，可设最后使用时间。"""
    path = root / name
    (path / "src").mkdir(parents=True, exist_ok=True)
    (path / "src" / "main.py").write_bytes(b"x" * (size_kb * 1024))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _settings(tmp_path: Path, quota_bytes: int, min_ratio: float = 0.10) -> object:
    return make_settings(
        workspace_root=tmp_path / "ws",
        repos_quota_bytes=quota_bytes,
        quota_min_reclaim_ratio=min_ratio,
    )


class TestUsage:
    def test_reports_zero_when_no_repos_dir(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, quota_bytes=1024)
        usage = quota_usage(settings)  # type: ignore[arg-type]
        assert usage.total_bytes == 0
        assert usage.repo_count == 0
        assert not usage.over_quota

    def test_size_is_same_order_as_actual(self, tmp_path: Path) -> None:
        """不要求精确到字节，但不能差一个数量级。"""
        settings = _settings(tmp_path, quota_bytes=10 * 1024 * 1024)
        repos = settings.repos_dir  # type: ignore[attr-defined]
        repos.mkdir(parents=True)
        _repo(repos, "acme__widget", size_kb=100)

        usage = quota_usage(settings)  # type: ignore[arg-type]
        assert usage.repo_count == 1
        assert 100 * 1024 <= usage.total_bytes < 120 * 1024

    def test_skips_symlinked_entries(self, tmp_path: Path) -> None:
        """junction 会被 iterdir 穿透——统计跑到系统盘会卡住几分钟。"""
        settings = _settings(tmp_path, quota_bytes=10 * 1024 * 1024)
        repos = settings.repos_dir  # type: ignore[attr-defined]
        repos.mkdir(parents=True)
        real = _repo(repos, "acme__widget", size_kb=50)
        outside = tmp_path / "outside"
        _repo(outside.parent, "outside", size_kb=400)
        junction_or_skip(real / "link", outside)

        usage = quota_usage(settings)  # type: ignore[arg-type]
        # 只该数到 real 自己的 50KB，不该把 junction 指向的 400KB 计入。
        assert usage.total_bytes < 120 * 1024


class TestCleanup:
    def test_under_quota_removes_nothing(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, quota_bytes=10 * 1024 * 1024)
        repos = settings.repos_dir  # type: ignore[attr-defined]
        repos.mkdir(parents=True)
        kept = _repo(repos, "acme__widget", size_kb=50)

        outcome = ensure_repo_quota(settings, set())  # type: ignore[arg-type]
        assert outcome.admitted
        assert outcome.reclaimed_bytes == 0
        assert kept.is_dir()

    def test_evicts_least_recently_used(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, quota_bytes=150 * 1024)
        repos = settings.repos_dir  # type: ignore[attr-defined]
        repos.mkdir(parents=True)
        old = _repo(repos, "old__repo", size_kb=100, mtime=1000.0)
        fresh = _repo(repos, "fresh__repo", size_kb=100, mtime=9_000_000_000.0)

        outcome = ensure_repo_quota(settings, set())  # type: ignore[arg-type]
        assert outcome.admitted
        assert not old.is_dir()
        assert fresh.is_dir()
        assert "old__repo" in outcome.removed

    def test_running_repo_is_never_evicted(self, tmp_path: Path) -> None:
        """清掉正在分析的仓库会让那次分析以一个莫名的文件缺失错误失败。"""
        settings = _settings(tmp_path, quota_bytes=150 * 1024)
        repos = settings.repos_dir  # type: ignore[attr-defined]
        repos.mkdir(parents=True)
        # 在跑的那个恰好是最久未使用的——正是最容易被误删的情形。
        running = _repo(repos, "running__repo", size_kb=100, mtime=1000.0)
        idle = _repo(repos, "idle__repo", size_kb=100, mtime=9_000_000_000.0)

        outcome = ensure_repo_quota(settings, {str(running)})  # type: ignore[arg-type]
        assert running.is_dir()
        assert not idle.is_dir()
        assert outcome.admitted

    def test_admits_without_cleanup_when_all_reclaimable_are_running(
        self, tmp_path: Path
    ) -> None:
        """磁盘超一点比清掉正在分析的仓库损失小。"""
        settings = _settings(tmp_path, quota_bytes=50 * 1024)
        repos = settings.repos_dir  # type: ignore[attr-defined]
        repos.mkdir(parents=True)
        running = _repo(repos, "running__repo", size_kb=200)

        outcome = ensure_repo_quota(settings, {str(running)})  # type: ignore[arg-type]
        assert outcome.admitted
        assert outcome.reclaimed_bytes == 0
        assert running.is_dir()
        assert "在分析中" in outcome.note

    def test_refuses_when_reclaimable_below_floor(self, tmp_path: Path) -> None:
        """KTD6 的回收量下限：宁可明确失败，也不做一次收益微小的清理。

        防的是「清理与克隆互相追赶」——配额贴顶时反复清理反复克隆，磁盘始终贴顶而克隆
        成本被反复付出。这种配置错误应当以明确失败暴露。
        """
        settings = _settings(tmp_path, quota_bytes=1024 * 1024, min_ratio=0.50)
        repos = settings.repos_dir  # type: ignore[attr-defined]
        repos.mkdir(parents=True)
        # 可回收的只有 10KB，而下限是配额的一半（512KB）。
        _repo(repos, "tiny__repo", size_kb=10)
        big = _repo(repos, "big__repo", size_kb=2000)

        outcome = ensure_repo_quota(settings, {str(big)})  # type: ignore[arg-type]
        assert not outcome.admitted
        assert "低于下限" in outcome.note
        assert "REPOS_QUOTA_BYTES" in outcome.note

    def test_stops_evicting_once_under_quota(self, tmp_path: Path) -> None:
        """删到回到配额以下即停：多删没有好处，那些副本还能被复用。"""
        settings = _settings(tmp_path, quota_bytes=250 * 1024)
        repos = settings.repos_dir  # type: ignore[attr-defined]
        repos.mkdir(parents=True)
        oldest = _repo(repos, "a__repo", size_kb=100, mtime=1000.0)
        middle = _repo(repos, "b__repo", size_kb=100, mtime=2000.0)
        newest = _repo(repos, "c__repo", size_kb=100, mtime=3000.0)

        ensure_repo_quota(settings, set())  # type: ignore[arg-type]
        assert not oldest.is_dir()
        assert middle.is_dir()
        assert newest.is_dir()

    def test_cleanup_spares_index_and_analyses_dirs(self, tmp_path: Path) -> None:
        """KTD10：清理只删仓库副本。索引与落盘历史的失效条件不同。"""
        settings = _settings(tmp_path, quota_bytes=50 * 1024)
        repos = settings.repos_dir  # type: ignore[attr-defined]
        repos.mkdir(parents=True)
        _repo(repos, "old__repo", size_kb=200, mtime=1000.0)

        index_dir = settings.index_dir  # type: ignore[attr-defined]
        index_dir.mkdir(parents=True)
        (index_dir / "chroma.sqlite3").write_bytes(b"i" * 4096)
        analyses = settings.analyses_dir  # type: ignore[attr-defined]
        analyses.mkdir(parents=True)
        (analyses / "abc.json").write_text("{}", encoding="utf-8")

        ensure_repo_quota(settings, set())  # type: ignore[arg-type]
        assert (index_dir / "chroma.sqlite3").is_file()
        assert (analyses / "abc.json").is_file()


class TestTouch:
    def test_touch_pushes_repo_later_in_lru_order(self, tmp_path: Path) -> None:
        """分析完成时刷 mtime，否则被反复分析的仓库反而最先被删。"""
        settings = _settings(tmp_path, quota_bytes=150 * 1024)
        repos = settings.repos_dir  # type: ignore[attr-defined]
        repos.mkdir(parents=True)
        revisited = _repo(repos, "revisited__repo", size_kb=100, mtime=1000.0)
        other = _repo(repos, "other__repo", size_kb=100, mtime=2000.0)

        touch_repo(revisited)
        ensure_repo_quota(settings, set())  # type: ignore[arg-type]

        assert revisited.is_dir()
        assert not other.is_dir()

    def test_touch_on_missing_path_does_not_raise(self, tmp_path: Path) -> None:
        touch_repo(tmp_path / "nope" / "deeper")


class TestObservability:
    def test_usage_reports_last_reclaimed(self, tmp_path: Path) -> None:
        """KTD11 的第三个信号：配额是否贴顶要能从 /api/health 看出来。"""
        reset_last_reclaimed()
        settings = _settings(tmp_path, quota_bytes=150 * 1024)
        repos = settings.repos_dir  # type: ignore[attr-defined]
        repos.mkdir(parents=True)
        _repo(repos, "old__repo", size_kb=100, mtime=1000.0)
        _repo(repos, "fresh__repo", size_kb=100, mtime=9_000_000_000.0)

        ensure_repo_quota(settings, set())  # type: ignore[arg-type]
        usage = quota_usage(settings)  # type: ignore[arg-type]
        assert usage.last_reclaimed_bytes > 0
        assert usage.quota_bytes == 150 * 1024
        reset_last_reclaimed()
