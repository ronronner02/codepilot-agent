"""克隆前的工作目录清理。

只覆盖不需要网络的部分。clone 本身的行为（浅克隆深度、子模块、错误区分）在计划里
列为需要真实仓库的验证项，由基准仓库实跑承担。

这里的重点是 `_remove_tree`：它在 Windows 上曾静默失败，导致同一仓库无法二次分析。
用真实只读文件构造，而非断言字符串——只读位是 OS 层行为，模拟不出来。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from backend.ingest.clone import _remove_tree
from backend.ingest.guards import RepoRejected


class TestRemoveTree:
    def test_missing_directory_is_noop(self, tmp_path: Path) -> None:
        _remove_tree(tmp_path / "never-existed")

    def test_plain_directory_removed(self, tmp_path: Path) -> None:
        target = tmp_path / "repo"
        (target / "src").mkdir(parents=True)
        (target / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        _remove_tree(target)
        assert not target.exists()

    def test_readonly_files_removed(self, tmp_path: Path) -> None:
        """git 把 .git/objects 下的对象文件设为只读，Windows 的 unlink 会因此失败。

        这是真实踩到的失效路径：清理静默失败后目录残留，git 随后报「destination path
        already exists」，错误信息离根因很远。
        """
        target = tmp_path / "repo"
        objects = target / ".git" / "objects" / "ab"
        objects.mkdir(parents=True)
        blob = objects / "cdef123456"
        blob.write_bytes(b"fake git object")
        os.chmod(blob, stat.S_IREAD)

        _remove_tree(target)
        assert not target.exists()

    def test_nested_readonly_tree_removed(self, tmp_path: Path) -> None:
        target = tmp_path / "repo"
        for depth in range(3):
            nested = target.joinpath(*[f"level{i}" for i in range(depth + 1)])
            nested.mkdir(parents=True, exist_ok=True)
            item = nested / "obj"
            item.write_bytes(b"data")
            os.chmod(item, stat.S_IREAD)

        _remove_tree(target)
        assert not target.exists()

    def test_undeletable_directory_raises_instead_of_silent_residue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """删不掉时必须报错。静默残留会让 git 抛出与根因无关的错误信息。"""
        target = tmp_path / "repo"
        target.mkdir()
        (target / "keep.txt").write_text("x", encoding="utf-8")

        def refuse(*args: object, **kwargs: object) -> None:
            return None  # 假装删除成功但什么都不做

        monkeypatch.setattr("backend.ingest.clone.shutil.rmtree", refuse)
        with pytest.raises(RepoRejected, match="无法清理既有工作目录"):
            _remove_tree(target)
