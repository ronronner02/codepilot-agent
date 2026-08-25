"""MCP server（R19、R20、R21、KTD15）。

用 FastMCP 的 list_tools / call_tool 直接打服务，不起真实客户端进程：要验证的是 schema
契约与工具委托，那与传输层无关，而起进程会让测试变慢且难定位失败。

三条贯穿约束：

**schema 是对外契约**（Interface Contracts：「工具名与参数一旦发布即视为对外契约」）。
描述缺失或参数没说明，客户端就不会正确调用——而 MCP 没有别的途径让它了解语义。

**实现与内部管道共用同一函数**（R21）。测试断言调用链指向 backend.tools，而不是比对
两边的输出——后者在两套实现恰好一致时会漏过。

**路径校验在 MCP 边界不放宽**（KTD15）。外部客户端是不可信输入源，`../` 与指向仓库外的
链接都要被拒。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backend.config import Settings
from backend.mcp_server.repos import RepoNotAnalysed, SkeletonCache, list_analysed_repos
from backend.mcp_server.server import create_server
from tests.support import junction_or_skip, make_settings


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """造一个「已分析」的工作区：repos 下有克隆好的仓库。"""
    repos = tmp_path / "ws" / "repos" / "acme__widget"
    (repos / "pkg").mkdir(parents=True)
    (repos / "pkg" / "core.py").write_text(
        "def helper(value):\n"
        "    return value * 2\n"
        "\n"
        "\n"
        "class Service:\n"
        "    def run(self):\n"
        "        return helper(1)\n",
        encoding="utf-8",
    )
    (repos / "pkg" / "api.py").write_text(
        "from pkg.core import helper\n\n\ndef handle():\n    return helper(3)\n",
        encoding="utf-8",
    )
    (repos / "README.md").write_text("# widget\n", encoding="utf-8")
    return tmp_path / "ws"


@pytest.fixture
def settings(workspace: Path) -> Settings:
    return make_settings(workspace_root=workspace)


@pytest.fixture
def server(settings: Settings) -> Any:
    return create_server(settings)


async def call(server: Any, tool_name: str, /, **arguments: Any) -> str:
    """调一个工具并把结果取成文本。

    工具名用仅位置参数（`/`）：有工具的参数就叫 name（find_definition），
    普通关键字参数会与它冲突而报「got multiple values」。
    """
    result = await server.call_tool(tool_name, arguments)
    # FastMCP 返回 ContentBlock 序列或 (blocks, structured) 元组，取决于版本与工具签名。
    blocks = result[0] if isinstance(result, tuple) else result
    if isinstance(blocks, dict):
        return json.dumps(blocks, ensure_ascii=False)
    return "\n".join(getattr(block, "text", str(block)) for block in blocks)


class TestSchemaContract:
    async def test_all_tools_have_non_empty_description(self, server: Any) -> None:
        """描述是客户端判断「何时用这个工具」的唯一依据。"""
        tools = await server.list_tools()
        assert tools
        for tool in tools:
            assert tool.description, f"{tool.name} 缺描述"
            assert len(tool.description) > 20, f"{tool.name} 的描述过短，客户端无从判断用途"

    async def test_all_parameters_have_descriptions(self, server: Any) -> None:
        """实测教训：本版 FastMCP 不解析 docstring 的 Args 段。

        写在那里的参数说明不会进 schema，客户端只看到参数名。必须用 Annotated + Field。
        """
        missing: list[str] = []
        for tool in await server.list_tools():
            for name, spec in (tool.inputSchema or {}).get("properties", {}).items():
                if not spec.get("description"):
                    missing.append(f"{tool.name}.{name}")
        assert missing == [], f"以下参数缺描述，客户端无从正确调用：{missing}"

    async def test_required_parameters_declared(self, server: Any) -> None:
        by_name = {t.name: t for t in await server.list_tools()}
        assert by_name["read_file"].inputSchema["required"] == ["repo", "path"]
        assert by_name["find_definition"].inputSchema["required"] == ["repo", "name"]
        # path 可选：省略即返回整体概览。
        assert by_name["query_dependencies"].inputSchema["required"] == ["repo"]

    async def test_tool_set_covers_required_capabilities(self, server: Any) -> None:
        """R19：工具集覆盖读结构、查依赖、查符号定义、检索代码。"""
        names = {t.name for t in await server.list_tools()}
        assert {"read_repo_structure", "query_dependencies", "find_definition", "search_code"} <= names

    async def test_server_has_instructions(self, server: Any) -> None:
        """instructions 告诉客户端整体用法（先 list_repos 再调其余）。"""
        assert server.instructions
        assert "list_repos" in server.instructions


class TestVectorStoreWarmup:
    """Chroma 必须在 stdio 接管标准流之前初始化。

    实测：之后初始化会挂住（独立进程 0.2 秒，stdio 服务内 >120 秒不返回）——rust 绑定层
    在标准流被替换为协议管道后无法完成初始化。预热放在 main() 里，run() 之前。
    """

    def test_warm_up_does_not_raise(self, settings: Settings) -> None:
        from backend.mcp_server.server import _warm_vector_store

        _warm_vector_store(settings)

    def test_warm_up_survives_broken_index_dir(self, tmp_path: Path) -> None:
        """预热失败不该阻止启动：其余六个工具不需要向量库。"""
        from backend.mcp_server.server import _warm_vector_store

        blocked = tmp_path / "blocked"
        blocked.write_text("这是文件而非目录", encoding="utf-8")
        _warm_vector_store(make_settings(workspace_root=blocked))


class TestRepoResolution:
    def test_slug_and_workdir_name_both_accepted(self, settings: Settings) -> None:
        """客户端可能给 owner/name 或工作目录名。只认一种会让它反复试错。"""
        cache = SkeletonCache(settings)
        assert cache.get("acme/widget").slug == "acme/widget"
        assert cache.get("acme__widget").slug == "acme/widget"

    def test_full_url_accepted(self, settings: Settings) -> None:
        cache = SkeletonCache(settings)
        assert cache.get("https://github.com/acme/widget").slug == "acme/widget"
        assert cache.get("https://github.com/acme/widget.git").slug == "acme/widget"

    def test_unanalysed_repo_raises_with_available_list(self, settings: Settings) -> None:
        """R20：未分析仓库返回明确错误。错误里带可用列表，客户端才知道该用什么。"""
        cache = SkeletonCache(settings)
        with pytest.raises(RepoNotAnalysed) as exc:
            cache.get("never/analysed")
        assert "尚未分析" in str(exc.value)
        assert "acme/widget" in str(exc.value)

    def test_skeleton_cached_across_calls(self, settings: Settings) -> None:
        """解析代价不小，会话内应只付一次。"""
        cache = SkeletonCache(settings)
        first = cache.get("acme/widget")
        assert cache.get("acme/widget") is first

    def test_analysis_layer_is_lazy(self, settings: Settings) -> None:
        """基础层（符号表 + 图）与分析层（聚类、入口点、中心度）分开构建。

        实测教训：一次性全算时 fastapi 的聚类在 MCP server 进程里要 42.8 秒（独立进程只
        0.1 秒，差异来自 stdio 读取线程与工作线程争 GIL）。而七个工具里只有
        query_dependencies 的概览用得上分析层——让 find_definition 也等 40 秒会让客户端
        直接超时（Claude Code 默认约 60 秒）。
        """
        skeleton = SkeletonCache(settings).get("acme/widget")
        # 基础层已就位。
        assert skeleton.parse_outcome.parsed
        assert skeleton.graph.nodes
        # 分析层尚未构建。
        assert skeleton._modules is None

        skeleton.ensure_analysis()
        assert skeleton._modules is not None
        assert skeleton.modules

    def test_analysis_layer_built_once(self, settings: Settings) -> None:
        skeleton = SkeletonCache(settings).get("acme/widget")
        skeleton.ensure_analysis()
        first = skeleton._modules
        skeleton.ensure_analysis()
        assert skeleton._modules is first

    def test_list_repos_from_workdir_not_index(self, settings: Settings) -> None:
        """看工作目录而非向量索引：索引可能因 provider 变更对不上键，但代码仍在磁盘上，
        读结构、查依赖、查定义三个工具不需要索引。
        """
        assert list_analysed_repos(settings) == ["acme/widget"]

    def test_empty_workspace_lists_nothing(self, tmp_path: Path) -> None:
        assert list_analysed_repos(make_settings(workspace_root=tmp_path / "empty")) == []


class TestUnanalysedRepoErrors:
    """R20：对未分析仓库调用任一工具返回明确错误，非空结果。"""

    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            ("read_repo_structure", {"path": "."}),
            ("read_file", {"path": "pkg/core.py"}),
            ("find_definition", {"name": "helper"}),
            ("find_references", {"name": "helper"}),
            ("query_dependencies", {}),
            ("search_code", {"query": "认证逻辑"}),
        ],
    )
    async def test_each_tool_reports_error(
        self, server: Any, tool: str, arguments: dict[str, Any]
    ) -> None:
        text = await call(server, tool, repo="never/analysed", **arguments)
        assert "错误" in text
        assert "尚未分析" in text
        # 空结果会让客户端以为「这个仓库里没有这些东西」，而实际是没分析过。
        assert text.strip() != ""

    async def test_list_repos_states_emptiness(self, tmp_path: Path) -> None:
        empty = create_server(make_settings(workspace_root=tmp_path / "empty"))
        text = await call(empty, "list_repos")
        assert "没有已分析的仓库" in text
        assert "提交一次分析" in text


class TestSharedImplementation:
    """R21：工具实现与内部管道共用同一工具层，不维护两套逻辑。"""

    async def test_delegates_to_backend_tools(
        self, server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """断言调用链指向 backend.tools.call_tool，而非比对两边输出——后者在两套实现
        恰好一致时会漏过，而那正是「维护了两套逻辑」的典型形态。
        """
        recorded: list[tuple[str, dict[str, Any]]] = []
        from backend.tools.results import ToolError

        def spy(_ctx: Any, name: str, arguments: dict[str, Any]) -> ToolError:
            recorded.append((name, arguments))
            return ToolError(message="spy")

        monkeypatch.setattr("backend.mcp_server.server.call_tool", spy)

        await call(server, "read_file", repo="acme/widget", path="pkg/core.py")
        await call(server, "find_definition", repo="acme/widget", name="helper")
        await call(server, "read_repo_structure", repo="acme/widget", path=".")
        await call(server, "find_references", repo="acme/widget", name="helper")

        assert [name for name, _ in recorded] == [
            "read_file",
            "find_definition",
            "list_structure",
            "find_references",
        ]

    async def test_find_definition_matches_symbol_table(self, server: Any, settings: Settings) -> None:
        """结果与 U3 的符号表一致。"""
        text = await call(server, "find_definition", repo="acme/widget", name="helper")
        skeleton = SkeletonCache(settings).get("acme/widget")
        expected = next(
            s for f in skeleton.parse_outcome.parsed for s in f.symbols if s.name == "helper"
        )
        assert f"{expected.path}:{expected.start_line}" in text

    async def test_dependencies_match_graph(self, server: Any, settings: Settings) -> None:
        """依赖关系与 U4 的图一致。"""
        text = await call(server, "query_dependencies", repo="acme/widget", path="pkg/api.py")
        skeleton = SkeletonCache(settings).get("acme/widget")
        for target in skeleton.graph.edges["pkg/api.py"]:
            assert target in text


class TestPathSecurity:
    """KTD15：路径校验在 MCP 边界同样生效且不放宽。

    外部客户端是不可信输入源。校验由工具层的 resolve_within 承担（单一实现），
    这些测试确认它在 MCP 这条路径上确实生效。
    """

    async def test_dotdot_path_rejected_on_read_file(self, server: Any, workspace: Path) -> None:
        secret = workspace.parent / "outside.txt"
        secret.write_text("do-not-leak\n", encoding="utf-8")

        text = await call(
            server, "read_file", repo="acme/widget", path="../../../outside.txt"
        )
        assert "do-not-leak" not in text
        assert "路径校验" in text or "不存在" in text

    async def test_dotdot_path_rejected_on_structure(self, server: Any) -> None:
        text = await call(server, "read_repo_structure", repo="acme/widget", path="..")
        assert "路径校验" in text

    async def test_junction_outside_repo_not_listed(self, server: Any, workspace: Path) -> None:
        """目录联接免提权可建，是比符号链接更现实的逃逸载体。"""
        outside = workspace.parent / "outside_dir"
        outside.mkdir(exist_ok=True)
        (outside / "secret.txt").write_text("leak\n", encoding="utf-8")
        junction_or_skip(workspace / "repos" / "acme__widget" / "vendored", outside)

        text = await call(server, "read_repo_structure", repo="acme/widget", path=".")
        assert "vendored" not in text

    async def test_junction_path_read_rejected(self, server: Any, workspace: Path) -> None:
        outside = workspace.parent / "outside_read"
        outside.mkdir(exist_ok=True)
        (outside / "secret.txt").write_text("must-not-appear\n", encoding="utf-8")
        junction_or_skip(workspace / "repos" / "acme__widget" / "linked", outside)

        text = await call(
            server, "read_file", repo="acme/widget", path="linked/secret.txt"
        )
        assert "must-not-appear" not in text


class TestToolResults:
    async def test_structure_returns_entries(self, server: Any) -> None:
        text = await call(server, "read_repo_structure", repo="acme/widget", path=".")
        assert "pkg" in text

    async def test_read_file_includes_line_numbers(self, server: Any) -> None:
        """行号进结果：客户端要能引用到具体位置。"""
        text = await call(
            server, "read_file", repo="acme/widget", path="pkg/core.py", start_line=1, end_line=2
        )
        assert "def helper" in text
        assert "1|" in text

    async def test_unknown_symbol_states_not_found(self, server: Any) -> None:
        text = await call(server, "find_definition", repo="acme/widget", name="no_such")
        assert "未找到" in text

    async def test_single_file_query_avoids_analysis_layer(
        self, server: Any, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """带 path 的查询只用基础层——它是常用路径，不该付聚类的代价。"""
        called: list[str] = []
        import backend.mcp_server.repos as repos_mod

        original = repos_mod.cluster_modules

        def spy(graph: Any) -> Any:
            called.append("cluster")
            return original(graph)

        monkeypatch.setattr(repos_mod, "cluster_modules", spy)
        await call(server, "query_dependencies", repo="acme/widget", path="pkg/api.py")
        assert called == [], "单文件查询不该触发模块聚类"

    async def test_dependency_overview_without_path(self, server: Any) -> None:
        text = await call(server, "query_dependencies", repo="acme/widget")
        assert "依赖概览" in text
        assert "模块" in text

    async def test_dependency_unknown_path_explains(self, server: Any) -> None:
        """不在图里的文件（README.md）要说明原因，不能只报「找不到」。"""
        text = await call(server, "query_dependencies", repo="acme/widget", path="README.md")
        assert "不在依赖图中" in text
        assert "符号级解析" in text

    async def test_search_without_index_reports_clearly(self, server: Any) -> None:
        """「没索引」与「没找到」的下一步动作不同，必须可区分。"""
        text = await call(server, "search_code", repo="acme/widget", query="认证逻辑")
        assert "错误" in text
        assert "尚未建立向量索引" in text
        # 其余工具仍可用，这一点要告诉客户端。
        assert "不需要索引" in text

    async def test_list_repos_returns_slug(self, server: Any) -> None:
        text = await call(server, "list_repos")
        assert "acme/widget" in text
