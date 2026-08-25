"""已分析仓库的发现与上下文重建。

MCP 客户端只给一个仓库标识，而工具层需要 ToolContext（工作目录 + 符号表 + 依赖图）。
后两者是分析时的内存状态，没有持久化，所以这里重新解析。

**重新解析而不是持久化骨架。** 解析是确定的（同一份代码得同一份符号表），加一种落盘格式
就要处理版本迁移与失效判断，而那两件事已经由向量索引的缓存键承担了一次——再来一套是重复
的复杂度。代价是每个仓库首次调用要等解析完成（fastapi 约 40 秒），所以结果按工作目录缓存
在进程内：MCP server 在 stdio 会话期间常驻，后续调用直接命中。

**仓库标识接受两种写法。** 客户端可能给 `fastapi/fastapi`（自然写法）或
`fastapi__fastapi`（工作目录名）。只认一种会让客户端反复试错，而 MCP 的错误信息是客户端
唯一的线索。
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from backend.config import Settings
from backend.ingest.clone import git_env
from backend.static_analysis.cluster import cluster_modules
from backend.static_analysis.entrypoints import find_entrypoints
from backend.static_analysis.graph import build_graph, centrality
from backend.static_analysis.models import (
    DependencyGraph,
    EntryPoint,
    Module,
    ParseOutcome,
)
from backend.static_analysis.parser import parse_repo
from backend.tools.context import ToolContext

logger = logging.getLogger("codepilot.mcp")

# 遍历工作树时跳过的目录。与 real_nodes 的 SKIP_DIRS 同源。
SKIP_DIRS = frozenset(
    {
        "node_modules",
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".nx",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
    }
)


class RepoNotAnalysed(Exception):
    """请求的仓库没有已分析的记录。

    单独一个异常类型而非返回空结果：R20 要求未分析仓库返回明确错误。空结果会让客户端
    以为「这个仓库里没有这些东西」，而实际是「还没分析过这个仓库」——两者的下一步动作
    完全不同。
    """


@dataclass
class RepoSkeleton:
    """一个仓库的静态骨架。

    **分两层惰性构建。** 基础层（符号表 + 依赖图）约 2 秒；分析层（模块聚类、入口点、
    中心度）在 fastapi 规模上要 40 秒以上。

    为什么分开：七个工具里只有 `query_dependencies` 用得上分析层，其余五个只需要基础层。
    一次性全算会让 `find_definition` 这类瞬时查询也等 40 秒——而 MCP 客户端有自己的超时
    （Claude Code 默认约 60 秒），首次调用会直接失败。

    实测数据（fastapi，1138 个文件）：
      遍历文件树 0.3s / 解析 1.6s / 建图 0.5s / 聚类与中心度 42.8s

    最后一项在独立进程里只要 0.1 秒。差异来自 GIL：MCP server 的 stdio 读取线程持续活跃，
    与工作线程里的 CPU 密集计算反复争抢，把 O(模块数²) 的聚类放大了两个数量级。这是又一条
    「只算需要的东西」的理由——即便没有 GIL 问题，为不用它的工具付这个成本也是浪费。
    """

    slug: str
    workdir: Path
    commit_sha: str
    parse_outcome: ParseOutcome
    graph: DependencyGraph

    # 分析层。按需构建，未构建时为 None。
    _modules: list[Module] | None = None
    _entrypoints: list[EntryPoint] | None = None
    _centrality: dict[str, float] | None = None

    def tool_context(self, max_file_bytes: int) -> ToolContext:
        """工具层上下文。只需要基础层，不触发分析层构建。"""
        return ToolContext(
            workdir=self.workdir,
            parse_outcome=self.parse_outcome,
            graph=self.graph,
            max_file_bytes=max_file_bytes,
        )

    def ensure_analysis(self) -> None:
        """构建分析层。已构建则直接返回。

        由需要它的工具显式调用，而不是在属性访问时隐式触发——隐式触发会让「这次调用为什么
        慢」难以定位，而这一层的代价正是数十秒量级。
        """
        if self._modules is not None:
            return
        started = time.perf_counter()
        self._modules = cluster_modules(self.graph)
        self._entrypoints = find_entrypoints(self.workdir, self.graph)
        self._centrality = centrality(self.graph)
        logger.info(
            "%s 的分析层就绪：%d 个模块，耗时 %.1fs",
            self.slug,
            len(self._modules),
            time.perf_counter() - started,
        )

    @property
    def modules(self) -> list[Module]:
        self.ensure_analysis()
        assert self._modules is not None
        return self._modules

    @property
    def entrypoints(self) -> list[EntryPoint]:
        self.ensure_analysis()
        assert self._entrypoints is not None
        return self._entrypoints

    @property
    def centrality(self) -> dict[str, float]:
        self.ensure_analysis()
        assert self._centrality is not None
        return self._centrality


def _workdir_name(identifier: str) -> str:
    """把仓库标识归一化为工作目录名。

    `fastapi/fastapi` 与 `fastapi__fastapi` 都指向同一个目录。去掉可能的 URL 前缀与
    `.git` 后缀——客户端可能直接粘贴仓库地址。
    """
    cleaned = identifier.strip().rstrip("/")
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    if cleaned.endswith(".git"):
        cleaned = cleaned[: -len(".git")]
    return cleaned.replace("/", "__")


def list_analysed_repos(settings: Settings) -> list[str]:
    """列出已克隆的仓库标识（`owner/name` 形态）。

    看工作目录而非向量索引：索引可能因 provider 变更而对不上键，但代码仍在磁盘上，
    读结构、查依赖、查定义三个工具不需要索引也能用。只有检索代码需要索引。
    """
    repos_dir = settings.repos_dir
    if not repos_dir.is_dir():
        return []
    found: list[str] = []
    for path in sorted(repos_dir.iterdir()):
        if path.is_dir() and "__" in path.name:
            found.append(path.name.replace("__", "/", 1))
    return found


def _head_sha(workdir: Path) -> str:
    """取 HEAD 的 SHA。失败返回空串——检索工具会因缓存键不匹配而报未索引，
    那比让整个工具调用失败好。

    stdin 显式指向 DEVNULL：不给的话子进程会继承父进程的 stdin。在 MCP server 里那是
    协议管道（stdio 传输），git 继承它可能吞掉协议字节或让调用挂住——实测表现为首个触发
    git 的工具调用固定停顿数十秒，且与仓库规模无关（2 个文件的仓库同样卡住）。
    git 本身不需要 stdin，断开它没有代价。
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(workdir),
            env=git_env(),
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=15.0,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


class SkeletonCache:
    """按工作目录缓存骨架。

    MCP server 在 stdio 会话期间常驻，所以进程内缓存有实际收益：首次调用付解析成本，
    后续调用直接命中。缓存不失效——会话期间仓库内容不会变（分析是另一个进程做的，
    而那会产生新的 commit 与新的工作目录状态，本会话看不到）。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cache: dict[str, RepoSkeleton] = {}

    def get(self, identifier: str) -> RepoSkeleton:
        """取骨架。仓库未分析时抛 RepoNotAnalysed。"""
        name = _workdir_name(identifier)
        if name in self._cache:
            return self._cache[name]

        workdir = self._settings.repos_dir / name
        if not workdir.is_dir():
            available = list_analysed_repos(self._settings)
            hint = (
                f"已分析的仓库：{', '.join(available)}"
                if available
                else "当前没有任何已分析的仓库。请先通过 CodePilot 的界面或 API 提交一次分析。"
            )
            raise RepoNotAnalysed(f"仓库 {identifier} 尚未分析。{hint}")

        logger.info("首次为 %s 重建基础骨架（解析中）", name)
        started = time.perf_counter()
        files = [
            path.relative_to(workdir).as_posix()
            for path in workdir.rglob("*")
            if path.is_file() and not (SKIP_DIRS & set(path.relative_to(workdir).parts))
        ]
        outcome = parse_repo(workdir, files, self._settings.max_file_bytes)
        graph = build_graph(
            workdir, outcome.parsed, max_nodes=self._settings.max_graph_nodes
        )
        skeleton = RepoSkeleton(
            slug=name.replace("__", "/", 1),
            workdir=workdir,
            commit_sha=_head_sha(workdir),
            parse_outcome=outcome,
            graph=graph,
        )
        self._cache[name] = skeleton
        logger.info(
            "基础骨架就绪 %s：%d 个文件、%d 条依赖边，耗时 %.1fs"
            "（模块聚类等分析层按需构建）",
            name,
            len(outcome.parsed),
            sum(len(t) for t in graph.edges.values()),
            time.perf_counter() - started,
        )
        return skeleton
