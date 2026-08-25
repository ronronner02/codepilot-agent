"""MCP server。以标准 MCP 协议对外暴露仓库分析工具（R19、R20、R21）。

**这是工具层之上的薄胶水，不持有业务判断。** 四个工具的实现全部委托给
`backend.tools`——那一层从设计时就不依赖 LangGraph state（KTD7），正是为了让这里能直接
复用。MCP server 只做三件事：协议翻译、仓库标识解析、错误映射。

R21 要求「不维护两套逻辑」，所以这里没有任何 if/else 判断代码语义的地方。若某个工具在
MCP 侧需要不同行为，那是工具层的参数该表达的事，不该在这里分叉。

**路径校验在 MCP 边界同样生效且不放宽（KTD15）。** 这条不是靠本模块自觉——`read_file`
与 `list_structure` 内部就调 `resolve_within`，MCP 拿到的是同一份实现。外部客户端传
`../` 或指向仓库外的链接会被那一层拒绝，本模块不需要（也不应该）另加一道判断：两道判断
容易在演进中分叉，而分叉的那一侧就是漏洞。

启动：
    python -m backend.mcp_server.server
调试：
    mcp dev backend/mcp_server/server.py
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from backend.cache.key import build_cache_key
from backend.config import Settings, get_settings
from backend.mcp_server.repos import RepoNotAnalysed, SkeletonCache, list_analysed_repos
from backend.providers.embedding import build_embedding_provider
from backend.rag.store import VectorStore
from backend.tools import call_tool
from backend.tools.results import ToolError

logger = logging.getLogger("codepilot.mcp")

# 参数描述用 Annotated + Field，不写在 docstring 的 Args 段里。
#
# 实测教训：本版 FastMCP 不解析 Google 风格 docstring 的参数段——那样写出来的 schema 里
# 每个参数只有 title 与 type，客户端看得到参数名却看不到含义。而参数描述是外部契约的一部分
# （Interface Contracts：「工具名与参数一旦发布即视为对外契约」），写不清客户端就不会正确
# 调用，而 MCP 没有别的途径让客户端了解参数语义。
RepoParam = Annotated[
    str, Field(description="仓库标识，如 fastapi/fastapi，或完整仓库地址。用 list_repos 查看可用的标识")
]

INSTRUCTIONS = """CodePilot 的仓库分析工具。

先用 list_repos 看哪些仓库已分析，再对具体仓库调用其余工具。所有工具的 repo 参数接受
`owner/name`（如 fastapi/fastapi）或完整仓库地址。

未分析的仓库会返回明确错误而非空结果——那种情况需要先通过 CodePilot 的界面或 API 提交
一次分析。"""


def create_server(settings: Settings | None = None) -> FastMCP:
    """构造 MCP server。settings 可注入，让测试不依赖 .env。"""
    resolved = settings or get_settings()
    skeletons = SkeletonCache(resolved)
    server: FastMCP = FastMCP(name="codepilot-mcp", instructions=INSTRUCTIONS)

    def _delegate(repo: str, tool: str, arguments: dict[str, Any]) -> str:
        """把调用转给工具层。

        返回渲染后的文本而非结构化对象：MCP 的工具结果最终要给模型读，而工具层的
        `render()` 已经是为「给模型读」设计的（含行号前缀、截断提示、检索方式说明）。
        再包一层 JSON 会让模型多一步解析，也丢掉那些说明性文字。
        """
        try:
            skeleton = skeletons.get(repo)
        except RepoNotAnalysed as exc:
            # 明确错误而非空结果（R20）。
            return f"错误：{exc}"

        result = call_tool(skeleton.tool_context(resolved.max_file_bytes), tool, arguments)
        return result.render()

    @server.tool(
        name="list_repos",
        description=(
            "列出已分析的仓库。其余工具的 repo 参数需要用这里返回的标识。"
            "返回为空说明尚未分析过任何仓库。"
        ),
    )
    def list_repos() -> str:
        repos = list_analysed_repos(resolved)
        if not repos:
            return (
                "当前没有已分析的仓库。请先通过 CodePilot 的界面或 API "
                "（POST /api/analyses）提交一次分析。"
            )
        lines = [f"已分析的仓库（{len(repos)} 个）："]
        lines.extend(f"  {slug}" for slug in repos)
        return "\n".join(lines)

    @server.tool(
        name="read_repo_structure",
        description=(
            "列出仓库内某个目录的直接子项（只列一层）。"
            "用它了解仓库的组织方式，或在读文件前确认路径。"
        ),
    )
    def read_repo_structure(
        repo: RepoParam,
        path: Annotated[
            str, Field(description="相对仓库根的目录路径，默认仓库根")
        ] = ".",
    ) -> str:
        return _delegate(repo, "list_structure", {"path": path})

    @server.tool(
        name="read_file",
        description=(
            "读取仓库内文件的指定行范围。行号 1-based。"
            "越界范围会收敛到有效区间而非报错，单次最多返回 200 行。"
        ),
    )
    def read_file(
        repo: RepoParam,
        path: Annotated[str, Field(description="相对仓库根的文件路径")],
        start_line: Annotated[int, Field(description="起始行，1-based，默认 1")] = 1,
        end_line: Annotated[
            int | None, Field(description="结束行，1-based 闭区间。省略则读到单次行数上限")
        ] = None,
    ) -> str:
        arguments: dict[str, Any] = {"path": path, "start_line": start_line}
        if end_line is not None:
            arguments["end_line"] = end_line
        return _delegate(repo, "read_file", arguments)

    @server.tool(
        name="find_definition",
        description=(
            "按名字查符号定义，返回文件路径与行号。"
            "支持裸名（helper）、限定名（Service.method）、带路径（src/a.py:helper）。"
            "结果来自静态解析的符号表，覆盖 Python 与 TypeScript。"
        ),
    )
    def find_definition(
        repo: RepoParam,
        name: Annotated[
            str,
            Field(
                description="符号名。支持裸名（helper）、限定名（Service.method）、"
                "带文件路径（src/a.py:helper）三种写法"
            ),
        ],
    ) -> str:
        return _delegate(repo, "find_definition", {"name": name})

    @server.tool(
        name="find_references",
        description=(
            "查符号的引用点。检索范围由依赖图定界——先由符号表定位定义文件，"
            "再在导入它的文件中检索。注意这是文本层匹配，同名局部变量与注释也可能命中。"
        ),
    )
    def find_references(
        repo: RepoParam,
        name: Annotated[str, Field(description="符号名。限定名会按最后一段做文本匹配")],
    ) -> str:
        return _delegate(repo, "find_references", {"name": name})

    @server.tool(
        name="query_dependencies",
        description=(
            "查询仓库的依赖关系：某个文件依赖谁、被谁依赖，以及模块划分与循环依赖。"
            "不给 path 时返回整体概览（模块划分、入口点、循环依赖）。"
            "注意：本工具需要模块聚类，在大仓库上首次调用可能耗时数十秒；"
            "只查单个文件的依赖时给出 path 会快得多。"
        ),
    )
    def query_dependencies(
        repo: RepoParam,
        path: Annotated[
            str | None,
            Field(
                description="相对仓库根的文件路径。给出时返回该文件的双向依赖与中心度；"
                "省略则返回整体概览（模块划分、入口点、循环依赖）"
            ),
        ] = None,
    ) -> str:
        try:
            skeleton = skeletons.get(repo)
        except RepoNotAnalysed as exc:
            return f"错误：{exc}"

        graph = skeleton.graph
        if path:
            if path not in graph.edges:
                return (
                    f"文件 {path} 不在依赖图中。它可能不是 Python/TypeScript 文件，"
                    f"或未通过符号级解析。用 read_repo_structure 确认路径。"
                )
            # 单文件查询只用基础层（图的双向边），不触发分析层——那要数十秒。
            reverse = graph.reverse_edges()
            imports = sorted(graph.edges.get(path, frozenset()))
            imported_by = sorted(reverse.get(path, frozenset()))
            lines = [f"{path} 的依赖关系："]
            lines.append(f"  它导入了 {len(imports)} 个仓库内文件：")
            lines.extend(f"    {target}" for target in imports[:30])
            lines.append(f"  被 {len(imported_by)} 个文件导入：")
            lines.extend(f"    {source}" for source in imported_by[:30])
            lines.append(
                f"  入度 {len(imported_by)}、出度 {len(imports)}"
                "（中心度需要全图分析，见不带 path 的概览）"
            )
            return "\n".join(lines)

        # 概览需要模块聚类。显式触发，让「这次为什么慢」有迹可循。
        skeleton.ensure_analysis()

        lines = [
            f"{skeleton.slug} 的依赖概览：",
            f"  文件级节点 {len(graph.nodes)} 个，依赖边 "
            f"{sum(len(t) for t in graph.edges.values())} 条",
            f"  仓库外依赖 {len(graph.external)} 个",
            f"  模块 {len(skeleton.modules)} 个",
        ]
        if graph.granularity.value != "file":
            # 降级必须标注，否则读者以为看到的是文件级精度。
            lines.append(f"  注意：依赖图已降级为{graph.granularity.value}级粒度")
            if graph.degraded_reason:
                lines.append(f"    {graph.degraded_reason}")

        if graph.cycles:
            lines.append(f"  循环依赖 {len(graph.cycles)} 个，最长的几个：")
            for cycle in sorted(graph.cycles, key=len, reverse=True)[:5]:
                lines.append(f"    {' -> '.join(cycle)} -> {cycle[0]}")

        lines.append("  规模最大的模块：")
        for module in sorted(skeleton.modules, key=lambda m: -len(m.files))[:10]:
            lines.append(
                f"    {module.name}（{len(module.files)} 个文件，"
                f"内部边 {module.internal_edges}，外部边 {module.external_edges}）"
            )

        if skeleton.entrypoints:
            lines.append("  入口点：")
            for entry in skeleton.entrypoints[:10]:
                lines.append(f"    [{entry.kind.value}] {entry.path} —— {entry.evidence}")

        if graph.unresolved:
            # 解析缺口要可见：残缺的依赖图会让「没有这条依赖」与「解析不到」混为一谈。
            lines.append(
                f"  未解析的 import {len(graph.unresolved)} 条"
                f"（解析规则的缺口，非「没有依赖」）"
            )
        return "\n".join(lines)

    @server.tool(
        name="search_code",
        description=(
            "按语义检索代码片段，返回带文件路径与行号的结果。"
            "需要该仓库已建立向量索引；未建索引时返回明确错误。"
            "适合「某个功能在哪实现」这类问题，不适合精确查符号名（那用 find_definition）。"
        ),
    )
    async def search_code(
        repo: RepoParam,
        query: Annotated[
            str, Field(description="自然语言查询，如「认证逻辑在哪里实现」")
        ],
        limit: Annotated[int, Field(description="返回条数，默认 5，上限 20")] = 5,
    ) -> str:
        try:
            skeleton = skeletons.get(repo)
        except RepoNotAnalysed as exc:
            return f"错误：{exc}"

        cleaned = query.strip()
        if not cleaned:
            return "错误：查询为空。"

        embedding = build_embedding_provider(resolved)
        key = build_cache_key(
            repo=skeleton.slug,
            commit_sha=skeleton.commit_sha,
            provider_identity=embedding.identity,
        )
        store = VectorStore(resolved.index_dir, key.digest)
        if not store.exists():
            # 明确说明是「没索引」而非「没找到」——两者的下一步动作不同。
            return (
                f"错误：{skeleton.slug} 尚未建立向量索引（{key.describe()}），无法语义检索。\n"
                f"请先通过 CodePilot 的界面或 API 完成一次分析；"
                f"若已分析过，可能是 embedding provider 变更导致索引键不匹配。\n"
                f"读结构、查依赖、查定义三个工具不需要索引，仍可使用。"
            )

        try:
            vector = (await embedding.embed_texts([cleaned]))[0]
        except Exception as exc:  # noqa: BLE001 — 向量化失败要说清，不返回空结果
            return f"错误：查询向量化失败（{type(exc).__name__}: {exc}）。"

        hits = store.search(vector, limit=max(1, min(limit, 20)))
        if not hits:
            return f"未检索到与「{cleaned}」相关的代码片段。"

        lines = [f"「{cleaned}」的检索结果（{len(hits)} 条，距离越小越相关）："]
        for hit in hits:
            lines.append(f"\n--- {hit.citation}（{hit.symbol or '模块级'}）距离 {hit.distance:.4f} ---")
            lines.append(hit.content)
        return "\n".join(lines)

    return server


def _warm_vector_store(settings: Settings) -> None:
    """在接管标准流之前初始化 Chroma 客户端。

    **必须在 run() 之前做。** 实测：Chroma 的 PersistentClient 在 stdio 传输接管标准流
    之后初始化会挂住（独立进程 0.2 秒，stdio 服务内 >120 秒超时且不返回）。它的 rust
    绑定层在标准流被替换为协议管道后无法完成初始化。

    预热用一个不存在的集合键：只为触发客户端构造，不碰真实数据。构造好的客户端由
    VectorStore 各实例各自持有，所以这里预热的收益是「Chroma 的进程级初始化已完成」，
    后续实例构造只付很小的成本。

    失败不阻止启动：其余六个工具不需要向量库，让它们可用比因检索工具不可用而整个服务
    起不来更合理。search_code 届时会返回明确错误。
    """
    try:
        started = time.perf_counter()
        VectorStore(settings.index_dir, "0" * 16).exists()
        logger.info("向量库客户端预热完成 %.2fs", time.perf_counter() - started)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "向量库预热失败（%s: %s）。search_code 可能不可用，其余工具不受影响",
            type(exc).__name__,
            exc,
        )


def main() -> None:
    """stdio 传输启动（KTD7）。

    日志走 stderr：stdio 传输用 stdout 传协议消息，往 stdout 写日志会破坏协议帧。
    """
    import sys

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    _warm_vector_store(settings)
    create_server(settings).run(transport="stdio")


if __name__ == "__main__":
    main()
