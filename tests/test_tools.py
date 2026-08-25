"""工具层。内部管道与 MCP server 的共同实现（KTD7），所以测试直接打工具而不经 Agent。

两条贯穿约束：
  路径不得逃逸仓库工作目录（KTD14/KTD15），且 iterdir/glob 会穿透联接。
  「查到了但为空」与「工具出错」必须可区分——混同会让 Agent 无法判断该重试还是该换路。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.static_analysis.graph import build_graph
from backend.static_analysis.parser import parse_repo
from backend.tools import TOOL_NAMES, TOOL_SCHEMAS, call_tool
from backend.tools.context import ToolContext
from backend.tools.results import (
    DefinitionResult,
    FileSlice,
    ReferenceResult,
    StructureResult,
    ToolError,
)
from tests.support import junction_or_skip

MAX_BYTES = 1_048_576


def _write(root: Path, rel: str, body: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    """一个小仓库：core 定义 helper 与 Service，两个消费者导入它。"""
    root = tmp_path / "repo"
    _write(
        root,
        "pkg/core.py",
        "def helper(value):\n"
        "    return value * 2\n"
        "\n"
        "\n"
        "class Service:\n"
        "    def run(self):\n"
        "        return helper(1)\n",
    )
    _write(root, "pkg/consumer_a.py", "from pkg.core import helper\n\n\ndef go():\n    return helper(3)\n")
    _write(root, "pkg/consumer_b.py", "from pkg.core import Service\n\n\ndef go():\n    return Service().run()\n")
    _write(root, "pkg/unrelated.py", "def helper_like():\n    return 0\n")
    _write(root, "docs/notes.md", "# 说明\n")

    files = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]
    outcome = parse_repo(root, files, MAX_BYTES)
    graph = build_graph(root, outcome.parsed, max_nodes=5000)
    return ToolContext(
        workdir=root, parse_outcome=outcome, graph=graph, max_file_bytes=MAX_BYTES
    )


class TestReadFile:
    def test_line_range_returned(self, ctx: ToolContext) -> None:
        result = call_tool(ctx, "read_file", {"path": "pkg/core.py", "start_line": 1, "end_line": 2})
        assert isinstance(result, FileSlice)
        assert result.start_line == 1
        assert result.end_line == 2
        assert "def helper" in result.content
        assert "class Service" not in result.content

    def test_out_of_range_end_converges(self, ctx: ToolContext) -> None:
        """模型对行数只有估计，`read_file(path, 1, 9999)` 是常见调用。

        为此报错会让它浪费一轮试探边界，所以收敛到有效区间并说明。
        """
        result = call_tool(
            ctx, "read_file", {"path": "pkg/core.py", "start_line": 1, "end_line": 9999}
        )
        assert isinstance(result, FileSlice)
        assert result.end_line == result.total_lines
        assert "超出文件末尾" in result.truncated_note

    def test_start_beyond_end_returns_last_line(self, ctx: ToolContext) -> None:
        result = call_tool(ctx, "read_file", {"path": "pkg/core.py", "start_line": 500})
        assert isinstance(result, FileSlice)
        assert result.start_line == result.total_lines
        assert "超出文件末尾" in result.truncated_note

    def test_missing_path_returns_error_not_empty(self, ctx: ToolContext) -> None:
        """空内容会被模型读成「文件是空的」，继续往下推断。"""
        result = call_tool(ctx, "read_file", {"path": "pkg/nope.py"})
        assert isinstance(result, ToolError)
        assert "不存在" in result.message
        assert result.hint

    def test_directory_path_returns_error_with_hint(self, ctx: ToolContext) -> None:
        result = call_tool(ctx, "read_file", {"path": "pkg"})
        assert isinstance(result, ToolError)
        assert "list_structure" in result.hint

    def test_escape_rejected(self, ctx: ToolContext) -> None:
        result = call_tool(ctx, "read_file", {"path": "../outside.txt"})
        assert isinstance(result, ToolError)
        assert "路径校验" in result.message

    def test_line_cap_disclosed(self, tmp_path: Path, ctx: ToolContext) -> None:
        """截断必须可见——Agent 需要知道还有内容没看到，才会决定是否继续读。"""
        _write(ctx.workdir, "pkg/big.py", "\n".join(f"x = {i}" for i in range(500)))
        result = call_tool(ctx, "read_file", {"path": "pkg/big.py", "max_lines": 10})
        assert isinstance(result, FileSlice)
        assert result.end_line == 10
        assert "继续读请用 start_line=11" in result.truncated_note

    def test_long_line_clipped_with_note(self, ctx: ToolContext) -> None:
        """压缩过的单行可能几万字符，一行就能撑爆上下文。"""
        _write(ctx.workdir, "pkg/min.js.py", "x = '" + "a" * 2000 + "'\n")
        result = call_tool(ctx, "read_file", {"path": "pkg/min.js.py"})
        assert isinstance(result, FileSlice)
        assert "本行已截断" in result.content
        assert "字符已截断" in result.truncated_note

    def test_empty_file_states_it(self, ctx: ToolContext) -> None:
        _write(ctx.workdir, "pkg/empty.py", "")
        result = call_tool(ctx, "read_file", {"path": "pkg/empty.py"})
        assert isinstance(result, FileSlice)
        assert result.truncated_note == "文件为空"

    def test_render_includes_line_numbers(self, ctx: ToolContext) -> None:
        """行号进渲染结果：报告要能引用到具体行（R7）。"""
        result = call_tool(ctx, "read_file", {"path": "pkg/core.py", "start_line": 1, "end_line": 1})
        assert isinstance(result, FileSlice)
        assert "1|" in result.render()


class TestFindDefinition:
    def test_known_symbol_matches_symbol_table(self, ctx: ToolContext) -> None:
        result = call_tool(ctx, "find_definition", {"name": "helper"})
        assert isinstance(result, DefinitionResult)
        assert len(result.locations) == 1
        location = result.locations[0]
        assert location.path == "pkg/core.py"
        assert location.start_line == 1
        assert location.kind == "function"

    def test_qualified_name_resolves_method(self, ctx: ToolContext) -> None:
        result = call_tool(ctx, "find_definition", {"name": "Service.run"})
        assert isinstance(result, DefinitionResult)
        assert len(result.locations) == 1
        assert result.locations[0].qualified_name == "Service.run"

    def test_bare_method_name_also_resolves(self, ctx: ToolContext) -> None:
        """模型不总知道方法属于哪个类。"""
        result = call_tool(ctx, "find_definition", {"name": "run"})
        assert isinstance(result, DefinitionResult)
        assert any(loc.qualified_name == "Service.run" for loc in result.locations)

    def test_path_scoped_query(self, ctx: ToolContext) -> None:
        result = call_tool(ctx, "find_definition", {"name": "pkg/core.py:helper"})
        assert isinstance(result, DefinitionResult)
        assert len(result.locations) == 1

    def test_unknown_symbol_states_not_found(self, ctx: ToolContext) -> None:
        """「没找到」是正常查询结果而非工具故障，两者混同会让 Agent 无法区分。"""
        result = call_tool(ctx, "find_definition", {"name": "no_such_symbol"})
        assert isinstance(result, DefinitionResult)
        assert result.locations == ()
        assert "未找到" in result.render()
        assert "符号表中不存在" in result.render()

    def test_similar_name_not_matched(self, ctx: ToolContext) -> None:
        """`helper_like` 不是 `helper`——按名字精确匹配，不做模糊。"""
        result = call_tool(ctx, "find_definition", {"name": "helper"})
        assert isinstance(result, DefinitionResult)
        assert all(loc.name == "helper" for loc in result.locations)

    def test_order_is_deterministic(self, ctx: ToolContext) -> None:
        first = call_tool(ctx, "find_definition", {"name": "go"})
        second = call_tool(ctx, "find_definition", {"name": "go"})
        assert isinstance(first, DefinitionResult) and isinstance(second, DefinitionResult)
        assert [(l.path, l.start_line) for l in first.locations] == [
            (l.path, l.start_line) for l in second.locations
        ]


class TestFindReferences:
    def test_multiple_reference_sites_returned(self, ctx: ToolContext) -> None:
        result = call_tool(ctx, "find_references", {"name": "helper"})
        assert isinstance(result, ReferenceResult)
        paths = {hit.path for hit in result.hits}
        # 定义文件自身（含内部调用）与导入它的消费者都应命中。
        assert "pkg/core.py" in paths
        assert "pkg/consumer_a.py" in paths

    def test_scope_bounded_by_dependency_graph(self, ctx: ToolContext) -> None:
        """未导入定义文件的文件不进检索范围——这是比全仓库 grep 精确的地方。"""
        result = call_tool(ctx, "find_references", {"name": "helper"})
        assert isinstance(result, ReferenceResult)
        assert "pkg/unrelated.py" not in {hit.path for hit in result.hits}

    def test_method_note_discloses_text_level_matching(self, ctx: ToolContext) -> None:
        """解析层不提取调用点，这是文本层匹配。精度边界必须随结果一起返回。"""
        result = call_tool(ctx, "find_references", {"name": "helper"})
        assert isinstance(result, ReferenceResult)
        assert "不提取调用点" in result.method_note
        assert "注释" in result.method_note

    def test_unknown_symbol_does_not_grep_whole_repo(self, ctx: ToolContext) -> None:
        """退化为全仓库检索时 `get`、`run` 这类名字会返回上千命中，无信息量。"""
        result = call_tool(ctx, "find_references", {"name": "nonexistent_thing"})
        assert isinstance(result, ReferenceResult)
        assert result.hits == ()
        assert result.searched_files == 0
        assert "未做检索" in result.method_note

    def test_qualified_name_uses_bare_for_matching(self, ctx: ToolContext) -> None:
        """调用点写成 `obj.method`，拿限定名去匹配会一个都找不到。"""
        result = call_tool(ctx, "find_references", {"name": "Service.run"})
        assert isinstance(result, ReferenceResult)
        assert result.hits

    def test_hit_cap_disclosed(self, ctx: ToolContext) -> None:
        result = call_tool(ctx, "find_references", {"name": "helper", "max_hits": 1})
        assert isinstance(result, ReferenceResult)
        assert len(result.hits) == 1
        assert "上限" in result.truncated_note


class TestListStructure:
    def test_lists_direct_children(self, ctx: ToolContext) -> None:
        result = call_tool(ctx, "list_structure", {"path": "."})
        assert isinstance(result, StructureResult)
        paths = {entry.path for entry in result.entries}
        assert "pkg" in paths
        assert "docs" in paths

    def test_directory_entry_carries_file_count(self, ctx: ToolContext) -> None:
        """给 Agent 判断值不值得展开的依据。"""
        result = call_tool(ctx, "list_structure", {"path": "."})
        assert isinstance(result, StructureResult)
        pkg = next(e for e in result.entries if e.path == "pkg")
        assert pkg.is_dir
        assert pkg.file_count >= 4

    def test_does_not_escape_workdir_via_junction(self, ctx: ToolContext) -> None:
        """iterdir 会穿透联接。仓库内一个指向外部的联接就能让条目落到仓库外。

        这正是 U4 的 manifest 扫描踩过的坑，工具层同样要挡。
        """
        outside = ctx.workdir.parent / "outside"
        outside.mkdir(exist_ok=True)
        (outside / "secret.txt").write_text("leak\n", encoding="utf-8")
        junction_or_skip(ctx.workdir / "vendored", outside)

        result = call_tool(ctx, "list_structure", {"path": "."})
        assert isinstance(result, StructureResult)
        assert "vendored" not in {e.path for e in result.entries}
        assert "路径校验未通过" in result.truncated_note

    def test_works_with_relative_workdir(
        self, ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """工作目录是相对路径时也要能列目录。

        实测教训：配置默认 `./.workspace`（相对），而 iterdir 返回的条目是绝对路径——
        拿相对根去 relative_to 绝对条目会抛 ValueError。本文件其余测试全用 tmp_path
        （绝对），所以从未触发这个缺陷，而默认配置下这个工具必然失败。
        """
        monkeypatch.chdir(ctx.workdir.parent)
        relative_ctx = ToolContext(
            workdir=Path(ctx.workdir.name),
            parse_outcome=ctx.parse_outcome,
            graph=ctx.graph,
            max_file_bytes=MAX_BYTES,
        )
        result = call_tool(relative_ctx, "list_structure", {"path": "."})
        assert isinstance(result, StructureResult), f"相对工作目录下失败：{result}"
        assert "pkg" in {entry.path for entry in result.entries}

    def test_escape_path_rejected(self, ctx: ToolContext) -> None:
        result = call_tool(ctx, "list_structure", {"path": ".."})
        assert isinstance(result, ToolError)
        assert "路径校验" in result.message

    def test_file_path_returns_error_with_hint(self, ctx: ToolContext) -> None:
        result = call_tool(ctx, "list_structure", {"path": "pkg/core.py"})
        assert isinstance(result, ToolError)
        assert "read_file" in result.hint

    def test_noise_directories_skipped(self, ctx: ToolContext) -> None:
        (ctx.workdir / "node_modules").mkdir()
        (ctx.workdir / "node_modules" / "dep.js").write_text("x\n", encoding="utf-8")
        result = call_tool(ctx, "list_structure", {"path": "."})
        assert isinstance(result, StructureResult)
        assert "node_modules" not in {e.path for e in result.entries}

    def test_entry_cap_disclosed(self, ctx: ToolContext) -> None:
        for i in range(30):
            _write(ctx.workdir, f"many/f{i}.py", "x = 1\n")
        result = call_tool(ctx, "list_structure", {"path": "many", "max_entries": 5})
        assert isinstance(result, StructureResult)
        assert len(result.entries) == 5
        assert "仅返回前 5 个条目" in result.truncated_note


class TestDispatch:
    def test_schema_names_match_dispatch_table(self) -> None:
        """不一致会让模型调一个不存在的工具，运行时表现为「工具没反应」，离根因很远。"""
        schema_names = {s["function"]["name"] for s in TOOL_SCHEMAS}
        assert schema_names == set(TOOL_NAMES)

    def test_unknown_tool_returns_error_listing_available(self, ctx: ToolContext) -> None:
        result = call_tool(ctx, "grep_repo", {"pattern": "x"})
        assert isinstance(result, ToolError)
        assert "未知工具" in result.message
        assert "read_file" in result.hint

    def test_bad_arguments_return_error_with_parameter_hint(self, ctx: ToolContext) -> None:
        """模型偶尔会编造参数名。返回错误让它重试，而非中断整个循环。"""
        result = call_tool(ctx, "read_file", {"filepath": "pkg/core.py"})
        assert isinstance(result, ToolError)
        assert "参数不正确" in result.message
        assert "path" in result.hint

    def test_every_schema_has_description_and_params(self) -> None:
        for schema in TOOL_SCHEMAS:
            function = schema["function"]
            assert function["description"], f"{function['name']} 缺描述，模型无从判断何时用它"
            assert "properties" in function["parameters"]
