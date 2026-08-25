"""路径校验的逃逸测试（KTD14）。

符号链接用真实文件系统对象构造，不是只测字符串——字符串检查挡不住 realpath
层面的逃逸，而那正是这个函数存在的理由。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.paths import PathEscapeError, resolve_within
from tests.support import junction_or_skip


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    return root


@pytest.fixture
def outside_secret(tmp_path: Path) -> Path:
    secret = tmp_path / "outside" / "secret.txt"
    secret.parent.mkdir(parents=True)
    secret.write_text("do-not-read-me\n", encoding="utf-8")
    return secret


def _symlink_or_skip(link: Path, target: Path) -> None:
    """Windows 上未开启开发者模式时创建符号链接需要权限，跳过而不是假过。"""
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"当前环境无法创建符号链接：{exc}")


_junction_or_skip = junction_or_skip


def test_plain_path_inside_root_is_allowed(repo_root: Path) -> None:
    resolved = resolve_within(repo_root, "src/main.py")
    assert resolved.read_text(encoding="utf-8").startswith("print")


def test_dotdot_escape_is_rejected(repo_root: Path, outside_secret: Path) -> None:
    with pytest.raises(PathEscapeError):
        resolve_within(repo_root, "../outside/secret.txt")


def test_absolute_path_outside_root_is_rejected(
    repo_root: Path, outside_secret: Path
) -> None:
    with pytest.raises(PathEscapeError):
        resolve_within(repo_root, outside_secret)


def test_symlink_pointing_outside_root_is_rejected(
    repo_root: Path, outside_secret: Path
) -> None:
    link = repo_root / "src" / "leak.txt"
    _symlink_or_skip(link, outside_secret)

    with pytest.raises(PathEscapeError):
        resolve_within(repo_root, "src/leak.txt")


def test_junction_pointing_outside_root_is_rejected(
    repo_root: Path, outside_secret: Path
) -> None:
    """目录联接指向仓库外必须拒绝。

    这条曾是真实漏洞：`Path.is_symlink()` 对 junction 返回 False（要用
    `is_junction()`），而 `os.path.normpath` 不解析 reparse point，于是路径在字面上
    仍位于 root 内，逐段检查与包含判断双双放行。实测能读出仓库外文件内容。

    junction 免提权可建，这让它比符号链接更容易出现在不可信输入里——而
    resolve_within 同时是 MCP 的外部边界（KTD15）。
    """
    link = repo_root / "src" / "leakdir"
    _junction_or_skip(link, outside_secret.parent)

    with pytest.raises(PathEscapeError):
        resolve_within(repo_root, "src/leakdir/secret.txt")


def test_junction_inside_root_is_also_rejected(repo_root: Path) -> None:
    """指向 root 之内的 junction 同样拒绝——与符号链接策略保持一致。

    实现选择的是「一律拒绝 reparse point，不看指向哪里」。一致性本身有价值：
    「只拒指向外部的」需要先解析再判断，而解析过程就是 TOCTOU 窗口。
    """
    inner = repo_root / "src" / "inner"
    inner.mkdir()
    (inner / "ok.txt").write_text("fine\n", encoding="utf-8")
    link = repo_root / "src" / "innerlink"
    _junction_or_skip(link, inner)

    with pytest.raises(PathEscapeError):
        resolve_within(repo_root, "src/innerlink/ok.txt")


def test_nonexistent_path_inside_root_does_not_escape(repo_root: Path) -> None:
    """不存在的路径不应被当成逃逸——调用方需要「文件不存在」而非「路径非法」。"""
    resolved = resolve_within(repo_root, "src/not_created_yet.py")
    assert not resolved.exists()
    assert repo_root in resolved.parents
