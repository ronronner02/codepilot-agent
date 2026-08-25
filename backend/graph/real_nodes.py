"""生产节点集。把各单元的实现装进 LangGraph 的节点形状。

与 stub_nodes 对称：那边是不调 LLM 的固定数据（供图结构验证与回归基线），这边是真实
实现。builder 只认 NodeSet，两者可互换——这也是 builder 不直接实现节点的理由。

**节点是薄的。** 每个节点只做三件事：从 state 取输入、调用对应单元的实现、把产出整成
state 字段。业务逻辑留在各单元自己的模块里，因为那些模块要能被单独测试，也要能被
MCP server 与离线脚本复用（工具层尤其如此，见 KTD7）。

节点全部 async：ReAct 循环与 LLM 调用本就是异步的，图用 `ainvoke` 驱动。混用同步节点
会让 LangGraph 在线程池里跑它们，而那会让并发闸门（provider 里的 Semaphore）失效——
Semaphore 绑定事件循环，跨线程不生效。
"""

from __future__ import annotations

import logging
from pathlib import Path

from backend.cache.key import build_cache_key
from backend.cache.manager import check_cache, write_manifest
from backend.config import Settings
from backend.graph.builder import NodeSet
from backend.graph.nodes.module_agent import analyze_module
from backend.graph.nodes.planner import plan_modules
from backend.graph.nodes.reviewer import run_review
from backend.graph.nodes.synthesize import synthesize_report
from backend.graph.observability import observed
from backend.graph.state import AnalysisState, LanguageProfile, ModuleTask
from backend.ingest.admission import check_admissible
from backend.ingest.clone import clone_repo
from backend.ingest.guards import parse_repo_url
from backend.ingest.language_detect import detect_languages
from backend.providers.embedding import build_embedding_provider
from backend.providers.llm import LLMProvider
from backend.rag.indexer import build_index
from backend.rag.store import VectorStore
from backend.review.select_files import collect_churn, select_review_targets
from backend.static_analysis.cluster import cluster_modules
from backend.static_analysis.entrypoints import find_entrypoints
from backend.static_analysis.graph import build_graph, centrality
from backend.static_analysis.parser import parse_repo

logger = logging.getLogger("codepilot.nodes")

# 遍历工作树时跳过的目录。与工具层的 SKIP_NAMES 同源但独立：这里是「哪些文件进解析」，
# 那里是「哪些条目给 Agent 看」，两者的取舍未必永远一致。
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

# 送进评审的文件数。
#
# 取 10 的依据：错误处理与安全类检查要过 LLM 判断，成本与文件数成正比。10 个高中心度
# 文件已能覆盖仓库的要害位置，而 Reviewer 的标准是求准不求全。
DEFAULT_REVIEW_TARGETS = 10


def collect_repo_files(workdir: Path) -> list[str]:
    """列出工作树内待解析的文件（仓库相对路径）。"""
    return [
        path.relative_to(workdir).as_posix()
        for path in workdir.rglob("*")
        if path.is_file() and not (SKIP_DIRS & set(path.relative_to(workdir).parts))
    ]


def _repo_slug(repo_url: str) -> str:
    """从仓库地址取 owner/name 作缓存键的仓库标识。

    不用完整 URL：同一仓库的 https 与 ssh 形态、带不带 .git 后缀都指向同一份代码，
    用 URL 原文会让它们各建一份索引。解析失败时退回原文——宁可多建一份索引，
    也不要让不同仓库共用同一个键。
    """
    from backend.ingest.guards import RepoRejected, parse_repo_url

    try:
        return parse_repo_url(repo_url).slug
    except RepoRejected:
        return repo_url


def make_real_nodes(settings: Settings, provider: LLMProvider | None = None) -> NodeSet:
    """构造生产节点集。

    ingest / parse / cluster 标 fatal=True：它们构成管道前缀，任一失败后下游拿不到输入，
    兜住异常只会让每个节点依次静默失败，最终产出「完成」加一份空报告（见 observed 的说明）。

    provider 为 None 时各 LLM 节点走自己的降级路径（Planner 用纯规则、Reviewer 跳过
    需要判断的两类、synthesize 产出说明原因的空报告），而非整图失败——这让「没有 key」
    仍能跑出静态骨架，也是调试静态层时的常用形态。
    """

    @observed("ingest", fatal=True)
    async def ingest(state: AnalysisState) -> dict[str, object]:
        ref = parse_repo_url(state["repo_url"])
        admission = await check_admissible(ref, settings)
        clone = await clone_repo(ref, settings)
        profile = detect_languages(clone.workdir)
        return {
            "workdir": str(clone.workdir),
            "commit_sha": clone.head_sha,
            "language_profile": LanguageProfile(
                total_files=profile.total_files,
                parseable_files=profile.parseable_files,
                by_language=dict(profile.by_language),
            ),
            # 准入信息不进 state：它只在这一步有意义，且 inventory 与 profile 重复。
            # 记入日志便于排查，见 observed 装饰器的摘要。
            "admission_note": (
                f"可解析 {admission.inventory.parseable_files} / "
                f"总 {admission.inventory.total_files}，"
                f"体积 {admission.metadata.size_kb} KB"
            ),
        }

    @observed("parse", fatal=True)
    async def parse(state: AnalysisState) -> dict[str, object]:
        workdir = Path(state["workdir"])
        files = collect_repo_files(workdir)
        return {"parse_outcome": parse_repo(workdir, files, settings.max_file_bytes)}

    @observed("cluster", fatal=True)
    async def cluster(state: AnalysisState) -> dict[str, object]:
        workdir = Path(state["workdir"])
        outcome = state["parse_outcome"]
        graph = build_graph(workdir, outcome.parsed, max_nodes=settings.max_graph_nodes)
        return {
            "dependency_graph": graph,
            "modules": cluster_modules(graph),
            "entrypoints": find_entrypoints(workdir, graph),
            "centrality": centrality(graph),
        }

    @observed("planner")
    async def planner(state: AnalysisState) -> dict[str, object]:
        return await plan_modules(state, settings, provider)

    @observed("module_agent", scope_key="module")
    async def module_agent(state: ModuleTask) -> dict[str, object]:
        if provider is None:
            # 没有 provider 时不能分析模块。返回失败而非空分析——后者会让报告以为
            # 这个模块「没什么可说的」。
            from backend.graph.state import NodeFailure

            return {
                "module_failures": [
                    NodeFailure(
                        node="module_agent",
                        scope=state["module"].name,
                        error="未配置 LLM provider，模块分析未执行",
                    )
                ]
            }
        return await analyze_module(state, settings, provider)

    @observed("synthesize")
    async def synthesize(state: AnalysisState) -> dict[str, object]:
        return await synthesize_report(state, settings, provider)

    @observed("select_files")
    async def select_files(state: AnalysisState) -> dict[str, object]:
        workdir = Path(state["workdir"])
        graph = state["dependency_graph"]
        churn = collect_churn(workdir)
        selection = select_review_targets(
            list(graph.nodes),
            state.get("centrality") or {},
            churn,
            limit=DEFAULT_REVIEW_TARGETS,
        )
        return {
            "review_targets": list(selection.targets),
            "review_selection_basis": selection.basis,
        }

    @observed("reviewer")
    async def reviewer(state: AnalysisState) -> dict[str, object]:
        report = await run_review(state, settings, provider)
        return {
            "review_report": report,
            "findings": [
                f"[{f.severity.value}] {f.kind} {f.path}:{f.line} {f.message}"
                for f in report.all_findings
            ],
        }

    @observed("chunk_and_index")
    async def chunk_and_index(state: AnalysisState) -> dict[str, object]:
        workdir = Path(state["workdir"])
        outcome = state["parse_outcome"]
        embedding = build_embedding_provider(settings)

        key = build_cache_key(
            repo=_repo_slug(state.get("repo_url", "")),
            commit_sha=state.get("commit_sha", ""),
            provider_identity=embedding.identity,
        )
        store = VectorStore(settings.index_dir, key.digest)
        status = check_cache(settings.index_dir, key, store.exists(), store.count())

        # 命中检查在索引之前，且不调 embedding——AE3 要求二次提交不重复产生成本。
        result = await build_index(
            repo_root=workdir,
            rel_paths=[f.path for f in outcome.parsed],
            cache_key=key,
            provider=embedding,
            index_dir=settings.index_dir,
            max_file_bytes=settings.max_file_bytes,
        )

        if not result.cache_hit and result.chunk_count:
            # 清单只在真正建了索引后写：写在前面会让中断的索引留下误导性的记录。
            write_manifest(settings.index_dir, key, result.chunk_count)

        note = f"{status.reason}；{result.note}"
        if result.failed_batches:
            note += f"（失败批次：{result.failed_batches[0]}）"

        return {
            "indexed_chunks": result.chunk_count,
            "index_identity": key.provider_identity,
            "index_note": note,
            "index_cache_hit": result.cache_hit,
            "index_digest": key.digest,
        }

    return NodeSet(
        ingest=ingest,
        parse=parse,
        cluster=cluster,
        planner=planner,
        module_agent=module_agent,
        synthesize=synthesize,
        select_files=select_files,
        reviewer=reviewer,
        chunk_and_index=chunk_and_index,
    )
