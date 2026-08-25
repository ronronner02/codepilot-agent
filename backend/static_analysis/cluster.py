"""模块聚类。KTD11：目录结构为初始分组，再按 import 图的跨组边密度合并或拆分。

这是全计划最影响报告质量的一处（见计划的决策焦点）。聚类错了，报告的骨架整体偏移，
而这类错误在小仓库上看不出来，只在基准仓库实跑时暴露——所以本模块的阈值都是显式
参数，不是散落的魔法数字，便于在基准仓库上调参。

为什么不是纯社区发现（Louvain 之类）：那类算法优化模块度，产出的分组数学上内聚，
但常与作者心智模型不符——报告里说「模块 A 包含 auth/login.py 与 utils/date.py」
读起来别扭，即使这两个文件确实互相依赖。目录是作者的显式意图声明，应当作先验。

为什么不是纯目录：`utils/`、`common/`、`helpers/` 这类杂物袋在目录上是一个名字，
实际是几组互不相关的东西。它们在 import 图上表现为「组内没有连通性」，这正是拆分
的依据。

三步顺序（先拆后合，不可颠倒）：
  1. 按目录建初始分组。
  2. 拆：组内无连通关系的按连通分量拆开——只在组够大时做，避免把小目录打碎。
  3. 合：跨组耦合超阈值的合并——耦合定义见 _coupling。

先拆后合的理由：杂物袋若不先拆，它与多个模块都有强耦合，会在合并阶段把本该分开的
模块通过它连成一坨。拆开后各分量分别与真正相关的模块合并。

**本层不负责控制模块数量。** 基准仓库实测：fastapi 产出 205 个模块，其中约 150 个来自
`docs_src/*` 与 `tests/test_tutorial/*` 的独立教程目录。那些分组本身没错——作者确实
按目录把它们组织成独立单元——但数量对报告过多。挑哪些模块值得深挖是 Planner 的职责
（按规模与中心度定范围，扇出宽度另有上限），不是聚类的。在这一层压缩数量会丢掉真实
结构，且让「分组是否吻合作者意图」这个核对标准失去意义。
"""

from __future__ import annotations

from pathlib import Path

from backend.static_analysis.models import DependencyGraph, Module


def _directory_groups(nodes: tuple[str, ...]) -> dict[str, set[str]]:
    """按所在目录分组。仓库根下的文件归入 `<root>` 组。"""
    groups: dict[str, set[str]] = {}
    for node in nodes:
        parent = str(Path(node).parent).replace("\\", "/")
        key = "<root>" if parent in (".", "") else parent
        groups.setdefault(key, set()).add(node)
    return groups


def _undirected_neighbours(
    graph: DependencyGraph, scope: set[str]
) -> dict[str, set[str]]:
    """取 scope 内的无向邻接表。

    拆分判定用无向：`a.py` 导入 `b.py` 说明两者相关，方向不影响「是否属于同一模块」。
    """
    adjacency: dict[str, set[str]] = {node: set() for node in scope}
    for source in scope:
        for target in graph.edges.get(source, frozenset()):
            if target in scope:
                adjacency[source].add(target)
                adjacency[target].add(source)
    return adjacency


def _connected_components(adjacency: dict[str, set[str]]) -> list[set[str]]:
    """无向连通分量。BFS，按最小成员的字典序排序保证结果稳定。"""
    seen: set[str] = set()
    components: list[set[str]] = []
    for start in sorted(adjacency):
        if start in seen:
            continue
        component = {start}
        seen.add(start)
        queue = [start]
        while queue:
            node = queue.pop()
            for neighbour in adjacency[node]:
                if neighbour not in seen:
                    seen.add(neighbour)
                    component.add(neighbour)
                    queue.append(neighbour)
        components.append(component)
    return components


def _split_scattered_groups(
    graph: DependencyGraph,
    groups: dict[str, set[str]],
    min_size_to_split: int,
    min_component_size: int,
) -> list[tuple[str, set[str], str]]:
    """把「组内互不相关」的目录组按连通分量拆开。

    两道闸门防止过度拆分：
      - min_size_to_split：小目录不拆。3 个文件的目录即使互不 import，拆成 3 个
        模块也只是噪声。
      - min_component_size：拆出的碎片（通常是单文件分量）不独立成模块，回填到该
        目录最大的分量里。否则 `utils/` 里 10 个互不相关的工具函数会产出 10 个
        单文件「模块」，报告读起来是一张碎片清单。
    """
    result: list[tuple[str, set[str], str]] = []
    for name, members in sorted(groups.items()):
        if len(members) < min_size_to_split:
            result.append((name, members, "directory"))
            continue

        components = _connected_components(_undirected_neighbours(graph, members))
        if len(components) <= 1:
            result.append((name, members, "directory"))
            continue

        components.sort(key=len, reverse=True)
        keep = [c for c in components if len(c) >= min_component_size]
        scattered = [c for c in components if len(c) < min_component_size]

        if not keep:
            # 全是碎片：该目录内毫无连通性（典型的纯工具目录）。整体保留为一个组，
            # 拆开只会产出一堆单文件模块。
            result.append((name, members, "directory"))
            continue

        # 碎片数量超过有连通结构的部分时，不做回填——整体保留为一个组。
        #
        # 基准仓库实测教训：fastapi 的 `tests/` 有 200 多个互不 import 的测试文件，
        # 外加一个 3 文件的小连通分量。无上限回填会把 200 个碎片全塞进那个分量，
        # 产出一个「207 个文件、9 条内部边」的组——它被标为 split（似乎经过分析），
        # 实际是个假模块：既不是作者的目录单元，也不是图上的内聚单元。
        #
        # 整体保留则至少是诚实的：「这个目录是一批彼此独立的文件」，与 origin
        # 标记的 directory 一致，人工核对时不会被误导。
        scattered_size = sum(len(f) for f in scattered)
        keep_size = sum(len(c) for c in keep)
        if scattered_size > keep_size:
            result.append((name, members, "directory"))
            continue

        # 碎片回填到最大分量。
        for fragment in scattered:
            keep[0] |= fragment

        if len(keep) == 1:
            result.append((name, keep[0], "directory"))
        else:
            for i, component in enumerate(keep, start=1):
                result.append((f"{name}#{i}", component, "split"))
    return result


def _cross_pairs(graph: DependencyGraph, left: set[str], right: set[str]) -> int:
    """两组间有连接的「文件对」数量，方向折叠。

    数对而非数边：a 与 b 互相 import 是两条边，但只说明这一对文件相关一次。按边数
    计会让双向依赖的组对获得双倍权重，而双向依赖并不比单向更能说明「本属同一模块」。
    """
    connected: set[tuple[str, str]] = set()
    for source in left:
        for target in graph.edges.get(source, frozenset()) & right:
            connected.add((source, target))
    for source in right:
        for target in graph.edges.get(source, frozenset()) & left:
            connected.add((target, source))
    return len(connected)


def _coupling(graph: DependencyGraph, left: set[str], right: set[str]) -> float:
    """两组的耦合密度：实际相连的文件对 / 可能的文件对（|left| * |right|）。

    值域 [0, 1]，可读作「两组之间的文件有多大比例实际相连」。

    为什么按「可能的对数」归一化，而不是除以较小组的规模：后者会让贪心合并雪球式
    级联——一旦合并出一个大 blob，分母仍取对面小组的规模，于是 blob 对任何小组都
    轻易超阈值，最终把整个仓库并成一个模块。实测在本仓库上就是这个结果（33 个文件
    并成 32 个一组），而这正是计划警告的「聚类偏移」失效形态。

    按可能对数归一化则自带抑制：组规模增大时分母是乘积（平方级增长），真实跨组边只
    线性增长，密度随之下降，大 blob 自然停止吸收。

    考虑过的替代：模块度增益（Louvain 的判据）。它对全局结构更敏感，但引入「随机图
    零模型」这层间接，阈值不好向人解释；而这里需要的是能在基准仓库上人工核对的判据。
    """
    possible = len(left) * len(right)
    if possible == 0:
        return 0.0
    return _cross_pairs(graph, left, right) / possible


def _merge_coupled_groups(
    graph: DependencyGraph,
    groups: list[tuple[str, set[str], str]],
    threshold: float,
    max_merge_rounds: int,
) -> list[tuple[str, set[str], str]]:
    """贪心合并耦合度超阈值的组对。

    每轮只合并当前耦合度最高的一对，然后重算——合并会改变其余组对的耦合度（分母变
    大），一轮内批量合并会基于过期的数值做决定。轮数设上限防止病态输入下的长时间
    循环（每轮至少减少一个组，故上限实际也由组数保证，这里是双重保险）。
    """
    working = [(name, set(members), origin) for name, members, origin in groups]

    for _ in range(max_merge_rounds):
        best: tuple[float, int, int] | None = None
        for i in range(len(working)):
            for j in range(i + 1, len(working)):
                score = _coupling(graph, working[i][1], working[j][1])
                if score >= threshold and (best is None or score > best[0]):
                    best = (score, i, j)
        if best is None:
            break

        _, i, j = best
        left_name, left_members, _ = working[i]
        right_name, right_members, _ = working[j]
        merged_members = left_members | right_members
        working = [g for k, g in enumerate(working) if k not in (i, j)]
        working.append((_merged_name(left_name, right_name, merged_members), merged_members, "merged"))

    return working


# 合并组名里最多列出几个子目录。超出则只给数量——名字再长就不可读了。
_NAME_BRANCH_LIMIT = 3


def _merged_name(left: str, right: str, members: set[str]) -> str:
    """合并后的组名：公共前缀 + 区分性的子目录列表。

    只取公共前缀会撞名。基准仓库实测：ghostfolio 的 `apps/api/src` 这一个名字对应了
    11 个互不相同的模块，`apps/client/src/app/components` 对应 7 个——深层嵌套的
    Angular/Nx 结构里，多次独立合并很容易收敛到同一个祖先目录。

    重名为什么不能接受：报告里出现 11 个同名模块，读者无法区分哪个是哪个，结论也就
    无法落到具体文件上（R7 的可追溯要求）。

    加编号（`#1`、`#2`）能去重但不携带信息。改为列出成员实际所在的子目录：
    `apps/api/src/app/{auth,portfolio}` 既唯一，也直接告诉读者这个模块包含什么。
    """
    prefix = _common_directory(members)
    if not prefix:
        return f"{left}+{right}"

    # 各成员在公共前缀之下的第一段目录名，即这次合并把哪些子目录聚到了一起。
    branches = sorted(
        {
            rest.split("/")[0]
            for member in members
            if (rest := Path(member).parent.as_posix()[len(prefix) :].lstrip("/"))
        }
    )
    if not branches:
        return prefix
    if len(branches) <= _NAME_BRANCH_LIMIT:
        return f"{prefix}/{{{','.join(branches)}}}"
    return f"{prefix}/{{{len(branches)} 个子目录}}"


def _ensure_unique_names(
    groups: list[tuple[str, set[str], str]],
) -> list[tuple[str, set[str], str]]:
    """保证模块名唯一。

    命名规则是启发式的，唯一性不能靠它碰运气——两次合并落在相同前缀且相同分支集时仍会
    撞名。这一步把唯一性变成结构保证：撞名的组追加其字典序最小成员的文件名作区分。

    为什么用成员文件名而非计数器：模块之间是严格划分（每个文件只属于一个模块），所以
    最小成员必然唯一，且它携带信息——读者能据此定位到具体文件，而 `#2` 不能。
    """
    by_name: dict[str, list[int]] = {}
    for index, (name, _, _) in enumerate(groups):
        by_name.setdefault(name, []).append(index)

    result = list(groups)
    for name, indices in by_name.items():
        if len(indices) == 1:
            continue
        for index in indices:
            _, members, origin = result[index]
            anchor = Path(min(members)).name if members else str(index)
            result[index] = (f"{name} [{anchor}]", members, origin)
    return result


def _common_directory(members: set[str]) -> str:
    """成员路径的公共目录前缀。无公共部分返回空串。"""
    if not members:
        return ""
    segment_lists = [Path(m).parent.as_posix().split("/") for m in members]
    common: list[str] = []
    for parts in zip(*segment_lists):
        first = parts[0]
        if all(p == first for p in parts) and first not in (".", ""):
            common.append(first)
        else:
            break
    return "/".join(common)


def cluster_modules(
    graph: DependencyGraph,
    merge_threshold: float = 0.4,
    min_size_to_split: int = 4,
    min_component_size: int = 2,
    max_merge_rounds: int = 200,
) -> list[Module]:
    """产出模块分组。

    参数即调参入口（KTD11 说明阈值要在基准仓库上调）：
      - merge_threshold：耦合密度达到多少才合并，值域 [0, 1]。0.4 表示两组间约四成
        的文件对实际相连——这个量级下更像是同一模块被目录切开，而非模块间的正常
        协作。调低会合并出更大的模块，调高会保留更多目录级分组。
      - min_size_to_split / min_component_size：拆分的两道闸门，见
        _split_scattered_groups。

    返回按模块名排序，使同一输入的输出稳定——报告与人工核对都需要可复现的顺序。
    """
    if not graph.nodes:
        return []

    groups = _directory_groups(graph.nodes)
    split = _split_scattered_groups(graph, groups, min_size_to_split, min_component_size)
    merged = _ensure_unique_names(
        _merge_coupled_groups(graph, split, merge_threshold, max_merge_rounds)
    )

    modules: list[Module] = []
    for name, members, origin in merged:
        internal = 0
        external = 0
        for source in members:
            targets = graph.edges.get(source, frozenset())
            internal += len(targets & members)
            external += len(targets - members)
        modules.append(
            Module(
                name=name or "<root>",
                files=tuple(sorted(members)),
                internal_edges=internal,
                external_edges=external,
                origin=origin,
            )
        )

    modules.sort(key=lambda m: m.name)
    return modules
