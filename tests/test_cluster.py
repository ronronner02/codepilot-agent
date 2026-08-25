"""模块聚类：目录先验、杂物袋拆分、高耦合合并。

聚类是全计划最影响报告质量的一处，所以这些测试断言的是「分组决定」而非实现细节：
给定一种输入结构，期望的分组形态是什么。阈值调参时这些断言应当仍然成立——若不成立，
说明调参改变了聚类的定性行为，需要重新核对。

直接构造 DependencyGraph 而非走文件解析：这一层的输入契约就是图，用图做输入让
「哪种图结构导致哪种分组」的因果关系直接可读，不被 import 语法细节干扰。
"""

from __future__ import annotations

from backend.static_analysis.cluster import _merged_name, cluster_modules
from backend.static_analysis.models import DependencyGraph, Module


def _graph(edges: dict[str, list[str]], extra_nodes: tuple[str, ...] = ()) -> DependencyGraph:
    nodes = sorted({*edges, *(t for targets in edges.values() for t in targets), *extra_nodes})
    return DependencyGraph(
        nodes=tuple(nodes),
        edges={n: frozenset(edges.get(n, ())) for n in nodes},
    )


def _by_name(modules: list[Module]) -> dict[str, Module]:
    return {m.name: m for m in modules}


def _module_of(modules: list[Module], path: str) -> Module:
    return next(m for m in modules if path in m.files)


class TestDirectoryPrior:
    def test_clean_directory_structure_matches_directories(self) -> None:
        """目录清晰、跨目录耦合低时，分组应与目录一致——目录是作者的显式意图。"""
        modules = cluster_modules(
            _graph(
                {
                    "auth/login.py": ["auth/token.py"],
                    "auth/token.py": ["auth/store.py"],
                    "auth/store.py": [],
                    "billing/charge.py": ["billing/invoice.py"],
                    "billing/invoice.py": ["billing/tax.py"],
                    "billing/tax.py": [],
                }
            )
        )
        names = set(_by_name(modules))
        assert names == {"auth", "billing"}
        assert _by_name(modules)["auth"].origin == "directory"

    def test_root_level_files_grouped(self) -> None:
        modules = cluster_modules(_graph({"setup.py": [], "conftest.py": []}))
        assert _by_name(modules)["<root>"].files == ("conftest.py", "setup.py")

    def test_empty_graph_returns_no_modules(self) -> None:
        assert cluster_modules(DependencyGraph()) == []

    def test_output_is_deterministic(self) -> None:
        graph = _graph(
            {
                "a/one.py": ["a/two.py"],
                "a/two.py": [],
                "b/three.py": ["b/four.py"],
                "b/four.py": [],
            }
        )
        first = cluster_modules(graph)
        second = cluster_modules(graph)
        assert [(m.name, m.files) for m in first] == [(m.name, m.files) for m in second]


class TestSplitting:
    def test_junk_drawer_split_by_connectivity(self) -> None:
        """`utils/` 里两组互不相关的东西应拆开——目录名相同不代表是一个模块。"""
        modules = cluster_modules(
            _graph(
                {
                    "utils/date.py": ["utils/date_fmt.py"],
                    "utils/date_fmt.py": [],
                    "utils/crypto.py": ["utils/crypto_impl.py"],
                    "utils/crypto_impl.py": [],
                }
            )
        )
        assert len(modules) == 2, f"预期拆成两组，实际：{[m.name for m in modules]}"
        assert all(m.origin == "split" for m in modules)
        assert _module_of(modules, "utils/date.py") is not _module_of(modules, "utils/crypto.py")

    def test_small_directory_not_split(self) -> None:
        """小目录即使互不 import 也不拆——拆成单文件模块只是噪声。"""
        modules = cluster_modules(
            _graph({}, extra_nodes=("tiny/a.py", "tiny/b.py")),
        )
        assert len(modules) == 1
        assert _by_name(modules)["tiny"].origin == "directory"

    def test_singleton_fragments_folded_into_largest(self) -> None:
        """碎片回填：不产出一堆单文件模块。

        big 三件互连成一个分量，loose_one/loose_two 各自孤立。期望是一个模块，
        而非「一个三文件模块 + 两个单文件模块」。
        """
        modules = cluster_modules(
            _graph(
                {
                    "mix/big_a.py": ["mix/big_b.py"],
                    "mix/big_b.py": ["mix/big_c.py"],
                    "mix/big_c.py": [],
                },
                extra_nodes=("mix/loose_one.py", "mix/loose_two.py"),
            )
        )
        assert len(modules) == 1
        assert set(modules[0].files) == {
            "mix/big_a.py",
            "mix/big_b.py",
            "mix/big_c.py",
            "mix/loose_one.py",
            "mix/loose_two.py",
        }

    def test_fragments_outnumbering_structure_keep_directory_whole(self) -> None:
        """碎片远多于连通部分时不回填，整体保留。

        基准仓库实测教训：fastapi 的 `tests/` 有 200 多个互不 import 的测试文件加一个
        3 文件的小连通分量。无上限回填会产出「207 个文件、9 条内部边」的组，标记为
        split 却既不是作者的目录单元也不是图上的内聚单元——一个假模块。
        """
        edges: dict[str, list[str]] = {
            "tests/util_a.py": ["tests/util_b.py"],
            "tests/util_b.py": ["tests/util_c.py"],
            "tests/util_c.py": [],
        }
        modules = cluster_modules(
            _graph(edges, extra_nodes=tuple(f"tests/test_{i}.py" for i in range(20)))
        )
        assert len(modules) == 1
        assert modules[0].origin == "directory", "假模块被标成 split 会误导人工核对"
        assert len(modules[0].files) == 23

    def test_fully_disconnected_directory_kept_whole(self) -> None:
        """纯工具目录（内部零连通）整体保留，拆开只会得到一堆单文件模块。"""
        modules = cluster_modules(
            _graph({}, extra_nodes=tuple(f"helpers/h{i}.py" for i in range(6)))
        )
        assert len(modules) == 1
        assert len(modules[0].files) == 6


class TestMerging:
    def test_high_cross_directory_coupling_merges(self) -> None:
        """跨目录高耦合应合并，而非机械照搬目录。

        api/ 的每个文件都依赖 handlers/ 的对应文件，跨组边数达到较小组规模，
        说明这是被目录切开的同一个模块。
        """
        modules = cluster_modules(
            _graph(
                {
                    "api/a.py": ["handlers/a.py", "api/b.py"],
                    "api/b.py": ["handlers/b.py"],
                    "handlers/a.py": ["handlers/b.py"],
                    "handlers/b.py": [],
                }
            )
        )
        assert len(modules) == 1, f"预期合并，实际：{[m.name for m in modules]}"
        assert modules[0].origin == "merged"
        assert set(modules[0].files) == {
            "api/a.py",
            "api/b.py",
            "handlers/a.py",
            "handlers/b.py",
        }

    def test_low_coupling_stays_separate(self) -> None:
        """一条跨组边不足以合并——模块间的正常协作不该被当成同一模块。"""
        modules = cluster_modules(
            _graph(
                {
                    "auth/login.py": ["auth/token.py", "shared/log.py"],
                    "auth/token.py": ["auth/store.py"],
                    "auth/store.py": [],
                    "shared/log.py": ["shared/fmt.py"],
                    "shared/fmt.py": ["shared/io.py"],
                    "shared/io.py": [],
                }
            )
        )
        assert set(_by_name(modules)) == {"auth", "shared"}

    def test_merged_name_uses_common_prefix_with_branches(self) -> None:
        """合并后的名字取公共前缀，并列出区分性的子目录。

        只取公共前缀会撞名：ghostfolio 上 `apps/api/src` 一个名字对应了 11 个互不
        相同的模块。带上子目录既唯一，也告诉读者这个模块包含什么。
        """
        modules = cluster_modules(
            _graph(
                {
                    "svc/api/a.py": ["svc/core/a.py", "svc/api/b.py"],
                    "svc/api/b.py": ["svc/core/b.py"],
                    "svc/core/a.py": ["svc/core/b.py"],
                    "svc/core/b.py": [],
                }
            )
        )
        assert len(modules) == 1
        assert modules[0].name == "svc/{api,core}"

    def test_merged_name_lists_branches_up_to_limit(self) -> None:
        """分支数在上限内时逐个列出。"""
        members = {"top/api/a.py", "top/core/b.py", "top/util/c.py"}
        assert _merged_name("top/api", "top/core", members) == "top/{api,core,util}"

    def test_merged_name_collapses_many_branches_to_count(self) -> None:
        """分支过多时只给数量——名字再长就不可读了。"""
        members = {f"top/sub{i}/a.py" for i in range(6)}
        assert _merged_name("top/sub0", "top/sub1", members) == "top/{6 个子目录}"

    def test_merged_name_without_common_prefix_lists_both(self) -> None:
        members = {"alpha/a.py", "beta/b.py"}
        assert _merged_name("alpha", "beta", members) == "alpha+beta"

    def test_merge_threshold_is_tunable(self) -> None:
        """阈值是显式参数：调低应合并更多，调高应保留目录分组。

        这组输入的耦合密度是 1/(2*2)=0.25——两组各 2 个文件，其中一对相连。
        """
        graph = _graph(
            {
                "x/one.py": ["y/one.py"],
                "x/two.py": ["x/one.py"],
                "y/one.py": ["y/two.py"],
                "y/two.py": [],
            }
        )
        assert len(cluster_modules(graph, merge_threshold=0.2)) == 1
        assert len(cluster_modules(graph, merge_threshold=0.8)) == 2

    def test_merging_does_not_cascade_into_one_blob(self) -> None:
        """大组不应雪球式吸收所有小组。

        tests/ 的每个文件都依赖 core/ 与 api/，耦合的绝对边数很高；但按可能对数
        归一化后密度不足，三组应各自保留。用「跨组边数 / 较小组规模」做判据时这里
        会并成一坨——那是本仓库实测暴露过的失效形态。
        """
        edges: dict[str, list[str]] = {}
        for i in range(6):
            edges[f"core/c{i}.py"] = [f"core/c{(i + 1) % 6}.py"]
        for i in range(5):
            edges[f"api/a{i}.py"] = [f"api/a{(i + 1) % 5}.py", "core/c0.py"]
        for i in range(8):
            edges[f"tests/t{i}.py"] = ["core/c1.py", "api/a1.py"]

        names = {m.name for m in cluster_modules(_graph(edges))}
        assert names == {"core", "api", "tests"}, f"合并级联：{names}"


class TestModuleMetrics:
    def test_edge_counts_reported(self) -> None:
        """internal/external 边数是「这个分组像不像一个模块」的人工核对依据。"""
        modules = cluster_modules(
            _graph(
                {
                    "auth/login.py": ["auth/token.py", "shared/log.py"],
                    "auth/token.py": ["auth/store.py"],
                    "auth/store.py": [],
                    "shared/log.py": ["shared/fmt.py"],
                    "shared/fmt.py": ["shared/io.py"],
                    "shared/io.py": [],
                }
            )
        )
        auth = _by_name(modules)["auth"]
        assert auth.internal_edges == 2
        assert auth.external_edges == 1

    def test_module_names_are_unique(self) -> None:
        """重名模块让报告无法追溯——读者分不清哪个是哪个。

        命名规则是启发式的，唯一性靠 _ensure_unique_names 兜底。这里构造多组会撞到
        同一公共前缀且分支集相同的输入。
        """
        edges: dict[str, list[str]] = {}
        for group in range(4):
            # 每组都是 pkg/<group>/{api,core} 结构，公共前缀与分支集完全同形。
            edges[f"pkg/g{group}/api/x.py"] = [
                f"pkg/g{group}/core/x.py",
                f"pkg/g{group}/api/y.py",
            ]
            edges[f"pkg/g{group}/api/y.py"] = [f"pkg/g{group}/core/y.py"]
            edges[f"pkg/g{group}/core/x.py"] = [f"pkg/g{group}/core/y.py"]
            edges[f"pkg/g{group}/core/y.py"] = []

        names = [m.name for m in cluster_modules(_graph(edges))]
        assert len(names) == len(set(names)), f"模块重名：{names}"

    def test_every_node_lands_in_exactly_one_module(self) -> None:
        """分组必须是划分：不丢文件也不重复归属，否则报告的覆盖面失真。"""
        graph = _graph(
            {
                "a/one.py": ["a/two.py", "b/one.py"],
                "a/two.py": [],
                "b/one.py": ["b/two.py"],
                "b/two.py": [],
                "utils/x.py": ["utils/y.py"],
                "utils/y.py": [],
                "utils/p.py": ["utils/q.py"],
                "utils/q.py": [],
            },
            extra_nodes=("orphan.py",),
        )
        modules = cluster_modules(graph)
        assigned = [f for m in modules for f in m.files]
        assert sorted(assigned) == sorted(graph.nodes)
        assert len(assigned) == len(set(assigned))
