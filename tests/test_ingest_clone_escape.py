"""克隆产物流经解析层时的逃逸拒绝（U2 的 Definition of Done 第二层）。

计划的 Definition of Done 明写符号链接逃逸测试要在三处各有覆盖：U1 函数级、
U2 克隆结果级、U14 MCP 边界级。U1 在 `test_paths.py`，U14 在 `test_mcp_server.py`，
这一层此前缺失。

**为什么两者不等价。** U1 测的是 `resolve_within` 这个函数本身：给它一条逃逸路径，
它会抛。这份测的是「克隆下来的仓库里含指向外部的链接时，实际遍历与读取的那几层是否
真的调用了它」——被分析的仓库是不可信输入，作者完全可以在仓库里放一个指向 `/etc` 或
`C:\\Users` 的链接。函数正确但调用点漏掉校验，是这一层唯一能抓的缺陷。

用 junction 而非符号链接：Windows 上建符号链接要提权，实测报 WinError 1314，测试会被
跳过——而这条的 proof intent 是 required，跳过等于门没关。junction 免提权，同样是
reparse point，`iterdir` 与 `realpath` 都会穿透它。
"""

from __future__ import annotations

from pathlib import Path

from backend.static_analysis.models import DependencyGraph, ParseOutcome, UnparsedFile
from backend.static_analysis.parser import parse_tree
from backend.tools.context import ToolContext
from backend.tools.list_structure import list_structure
from backend.tools.read_file import read_file
from backend.tools.results import ToolError
from tests.support import junction_or_skip

SECRET = "SECRET_OUTSIDE_REPO = 'must-not-be-read'\n"


def _fake_clone(tmp_path: Path) -> tuple[Path, Path]:
    """造一份「克隆产物」：仓库目录内含一个指向仓库外的目录联接。

    不真的去克隆：要验证的是解析层对含链接的工作树的处理，而链接是克隆完成后才存在于
    磁盘上的东西，与克隆过程本身无关。真实网络克隆的场景另有测试覆盖。
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text(SECRET, encoding="utf-8")

    workdir = tmp_path / "repos" / "acme__widget"
    (workdir / "pkg").mkdir(parents=True)
    (workdir / "pkg" / "core.py").write_text("def ok() -> int:\n    return 1\n", encoding="utf-8")

    # 仓库内的联接指向仓库外——这就是不可信仓库能构造出的攻击面。
    junction_or_skip(workdir / "pkg" / "leak", outside)
    return workdir, outside


def _context(workdir: Path) -> ToolContext:
    return ToolContext(
        workdir=workdir,
        parse_outcome=ParseOutcome(),
        graph=DependencyGraph(),
    )


class TestCloneResultEscape:
    def test_parser_refuses_file_reached_through_junction(self, tmp_path: Path) -> None:
        """解析层不跟随仓库内指向外部的联接。"""
        workdir, _ = _fake_clone(tmp_path)

        outcome = parse_tree(workdir, "pkg/leak/secret.py", max_bytes=1_048_576)

        assert isinstance(outcome, UnparsedFile)
        assert "路径校验未通过" in outcome.reason
        # 关键断言：外部文件的内容一个字节都没进来。
        assert "SECRET_OUTSIDE_REPO" not in outcome.reason

    def test_read_file_refuses_path_through_junction(self, tmp_path: Path) -> None:
        """读文件工具在克隆产物上同样拒绝，不返回仓库外内容。"""
        workdir, _ = _fake_clone(tmp_path)

        result = read_file(_context(workdir), "pkg/leak/secret.py")

        assert isinstance(result, ToolError)
        assert "路径校验未通过" in result.message

    def test_list_structure_skips_junction_entry(self, tmp_path: Path) -> None:
        """列目录穿透联接时逐条校验生效：不安全条目被跳过而非列出。

        这条针对的是 `iterdir()` 的行为——它会穿透联接，所以仓库内一个指向外部的联接
        就能让条目落到仓库外。校验必须逐条做，不能只校验入口目录。
        """
        workdir, _ = _fake_clone(tmp_path)

        result = list_structure(_context(workdir), "pkg")

        assert not isinstance(result, ToolError)
        paths = [entry.path for entry in result.entries]
        assert any(path.endswith("core.py") for path in paths)
        assert not any("leak" in path for path in paths)
        # 跳过原因要能被看到，否则「列不出来」与「被拒绝」在调用方看来一样。
        assert "路径校验未通过" in result.truncated_note

    def test_safe_file_in_same_clone_still_parses(self, tmp_path: Path) -> None:
        """反向对照：同一份克隆产物里的正常文件仍能解析。

        没有这条，上面三条用「什么都拒绝」也能通过——那是把门焊死，不是把门看住。
        """
        workdir, _ = _fake_clone(tmp_path)

        outcome = parse_tree(workdir, "pkg/core.py", max_bytes=1_048_576)

        assert not isinstance(outcome, UnparsedFile)
        assert outcome.path == "pkg/core.py"
