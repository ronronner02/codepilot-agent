"""结构类检查。纯图计算，不调 LLM。

为什么这一类完全不用 LLM：四项判据都能由依赖图确定地算出，而 LLM 在这里只会引入不可
复现的判断。「这里有循环依赖」是事实，不是意见。

四项检查与各自的确定依据：

  循环依赖    graph.cycles，U4 的迭代式 DFS 已算出
  层级违规    两个模块间的支配方向被少数反向边违反
  超大模块    模块文件数超阈值
  无引用文件  图上零入度且非入口点

**符号级死代码做不到，这里只做文件级。** R15 提到「死代码」，理想判据是「定义了但无处
引用的符号」。但 U3 的解析层只提取定义与 import 声明，不提取调用点——判断一个函数有没
有被调用需要遍历所有表达式里的标识符引用，那是另一个量级的工作（还要处理动态调用、
getattr、装饰器注册、框架的约定式发现）。

所以这里降级为文件级：零入度且不是入口点的文件。它的误报来源是明确的——测试文件、
脚本、插件式加载的模块都可能零入度而并非死代码。因此这项的严重度定为 LOW，且发现里
写明这是「疑似」而非结论。宁可标低也不要给出一条读者会当成事实的错判（Reviewer 的
标准是求准不求全）。
"""

from __future__ import annotations

from backend.review.models import (
    CategoryOutcome,
    CheckStatus,
    Finding,
    FindingCategory,
    Severity,
)
from backend.static_analysis.models import DependencyGraph, EntryPoint, Granularity, Module

# 模块文件数上限。超过即报「超大模块」。
#
# 取 40 的依据：一个模块要能被一段话讲清楚，40 个文件已经超出人一次能把握的范围。
# 这是启发式阈值，作为参数暴露以便在基准仓库上调。
DEFAULT_LARGE_MODULE_FILES = 40

# 支配方向的判定比例。反向边占比低于此值时，那些反向边被视为层级违规。
#
# 取 0.2 的含义：A->B 有 10 条边而 B->A 只有 1 条（占比 0.09）时，那 1 条是可疑的
# 反向依赖；若两方向各 5 条，则这两个模块本就是双向协作关系，不构成「违规」。
DEFAULT_DOMINANCE_RATIO = 0.2

# 参与层级违规判定的最小边数。边太少时比例不具统计意义——1 条对 0 条的比例是 0，
# 但那只说明这两个模块几乎没关系。
MIN_EDGES_FOR_DOMINANCE = 4


def check_cycles(graph: DependencyGraph) -> list[Finding]:
    """循环依赖。环由 U4 算出，这里只负责转成发现。

    严重度按环的长度分：两文件互相导入常是有意的（类型与实现分置），而跨越 4 个以上
    文件的环通常是分层失控，重构成本也高得多。
    """
    findings: list[Finding] = []
    for cycle in graph.cycles:
        members = " -> ".join(cycle) + f" -> {cycle[0]}"
        severity = Severity.HIGH if len(cycle) >= 4 else Severity.MEDIUM
        findings.append(
            Finding(
                category=FindingCategory.STRUCTURAL,
                kind="circular_dependency",
                path=cycle[0],
                line=0,
                message=f"{len(cycle)} 个文件构成循环依赖",
                evidence=f"依赖图上的有向环：{members}",
                severity=severity,
                related_paths=tuple(cycle[1:]),
            )
        )
    return findings


def check_layering(
    graph: DependencyGraph,
    modules: list[Module],
    dominance_ratio: float = DEFAULT_DOMINANCE_RATIO,
) -> list[Finding]:
    """层级违规：被支配方向违反的少数反向边。

    为什么用「支配方向」而不是预设的层级名（api -> service -> repository）：那要求项目
    遵循某套命名约定，对不遵循的项目会全部误判，而判据本身也不可核验（凭什么说 api 该
    在 service 之上）。支配方向从图本身算出——若 20 条边从 A 到 B、1 条从 B 到 A，那 1
    条就是可疑的，无需知道 A 和 B 各自叫什么。

    这个判据的局限：它只能发现「大多数情况下方向一致，个别例外」的形态。两个模块本就
    双向依赖时不会命中——那种情况由循环依赖检查覆盖。
    """
    module_of: dict[str, str] = {}
    for module in modules:
        for path in module.files:
            module_of[path] = module.name

    # 统计模块对之间双向的边。键为有序对，避免 (A,B) 与 (B,A) 分别累计。
    pair_edges: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for source, targets in graph.edges.items():
        source_module = module_of.get(source)
        if source_module is None:
            continue
        for target in targets:
            target_module = module_of.get(target)
            if target_module is None or target_module == source_module:
                continue
            key = tuple(sorted((source_module, target_module)))
            pair_edges.setdefault((key[0], key[1]), []).append((source, target))

    findings: list[Finding] = []
    for (left, right), edges in sorted(pair_edges.items()):
        forward = [(s, t) for s, t in edges if module_of[s] == left]
        backward = [(s, t) for s, t in edges if module_of[s] == right]
        total = len(forward) + len(backward)
        if total < MIN_EDGES_FOR_DOMINANCE:
            continue

        # 少数派方向即违规候选。两方向相当时都不报。
        minority, majority_name, minority_name = (
            (backward, left, right) if len(forward) > len(backward) else (forward, right, left)
        )
        if not minority or len(minority) / total >= dominance_ratio:
            continue

        for source, target in sorted(minority):
            findings.append(
                Finding(
                    category=FindingCategory.STRUCTURAL,
                    kind="layering_violation",
                    path=source,
                    line=0,
                    message=f"{minority_name} 反向依赖 {majority_name}，与主导方向相反",
                    evidence=(
                        f"两模块间共 {total} 条边，其中 {len(minority)} 条方向为 "
                        f"{minority_name} -> {majority_name}，占比 "
                        f"{len(minority) / total:.0%}；导入目标 {target}"
                    ),
                    severity=Severity.MEDIUM,
                    related_paths=(target,),
                )
            )
    return findings


def check_large_modules(
    modules: list[Module], max_files: int = DEFAULT_LARGE_MODULE_FILES
) -> list[Finding]:
    """超大模块。按文件数判定，阈值可调。"""
    findings: list[Finding] = []
    for module in sorted(modules, key=lambda m: -len(m.files)):
        if len(module.files) <= max_files:
            continue
        findings.append(
            Finding(
                category=FindingCategory.STRUCTURAL,
                kind="oversized_module",
                path=module.files[0],
                line=0,
                message=f"模块 {module.name} 含 {len(module.files)} 个文件，超出 {max_files}",
                evidence=(
                    f"模块内部边 {module.internal_edges}、外部边 {module.external_edges}；"
                    f"分组来历 {module.origin}"
                ),
                severity=Severity.LOW,
                related_paths=module.files[1:],
            )
        )
    return findings


def count_unreferenced_files(
    graph: DependencyGraph, entrypoints: list[EntryPoint]
) -> int:
    """数零入度且非入口点的文件。**只做聚合统计，不产出逐条发现。**

    为什么不逐条报（基准仓库实测结论）：这个信号的误报率接近 100%。

      fastapi   958 个零入度文件，其中 956 是测试、docs_src 示例与脚本
      ghostfolio 146 个，即使排除测试目录后剩下的 113 个也全是 jest.config.ts、
                 prisma/seed.mts、*.stories.ts、test-setup.ts 这类由外部工具
                 （jest、prisma、storybook、vite）调用的文件

    这些文件天然没有仓库内导入方——它们的调用方在仓库之外。逐条报出会产出近千条
    发现，把真正有价值的循环依赖与层级违规彻底淹没，而这正是 Reviewer 最该避免的
    形态（宁可只报 5 条真问题）。加目录名过滤也救不回来：过滤后精确率仍接近零。

    R15 列出的「死代码」因此未作为发现产出。要真正做到需要符号级引用分析（定义了但
    无处调用的函数），而当前解析层只提取定义与 import 声明、不提取调用点。这是能力
    边界，不是遗漏——见模块 docstring。

    保留计数是因为它本身是有用的规模信息：「近千个文件无仓库内导入方」这一句话对
    读者有意义，近千条发现没有。
    """
    entry_paths = {e.path for e in entrypoints}
    reverse = graph.reverse_edges()
    return sum(
        1
        for node in graph.nodes
        if node not in entry_paths and not reverse.get(node, frozenset())
    )


def run_structural_checks(
    graph: DependencyGraph,
    modules: list[Module],
    entrypoints: list[EntryPoint],
    max_module_files: int = DEFAULT_LARGE_MODULE_FILES,
    dominance_ratio: float = DEFAULT_DOMINANCE_RATIO,
) -> CategoryOutcome:
    """跑全部结构类检查，产出一份 CategoryOutcome。

    图降级为目录级时跳过而非硬跑：此时节点是目录，文件级的判据（零入度文件、层级
    违规）会得出关于目录的结论却按文件的措辞表述，读者无从分辨。按 R17 记为 SKIPPED
    并说明原因，比给出误导性结论好。
    """
    if not graph.nodes:
        return CategoryOutcome(
            category=FindingCategory.STRUCTURAL,
            status=CheckStatus.SKIPPED,
            scope="无",
            reason="依赖图为空，无可检查对象",
        )

    if graph.granularity is not Granularity.FILE:
        return CategoryOutcome(
            category=FindingCategory.STRUCTURAL,
            status=CheckStatus.SKIPPED,
            scope=f"{len(graph.nodes)} 个目录级节点",
            reason=(
                f"依赖图已降级为{graph.granularity.value}级粒度"
                f"（{graph.degraded_reason or '原因未记录'}），"
                "文件级判据在此粒度上会产出措辞与实际对象不符的结论"
            ),
        )

    findings = [
        *check_cycles(graph),
        *check_layering(graph, modules, dominance_ratio),
        *check_large_modules(modules, max_module_files),
    ]
    findings.sort(key=lambda f: (f.kind, f.path, f.line))

    unreferenced = count_unreferenced_files(graph, entrypoints)

    return CategoryOutcome(
        category=FindingCategory.STRUCTURAL,
        status=CheckStatus.EXECUTED,
        scope=(
            f"{len(graph.nodes)} 个文件、{len(modules)} 个模块；"
            f"检查项：循环依赖、层级违规、超大模块（>{max_module_files} 文件）。"
            f"另有 {unreferenced} 个文件无仓库内导入方（多为测试、配置与工具链入口，"
            f"其调用方在仓库之外），因误报率过高未逐条列出；"
            f"符号级死代码需调用点分析，当前解析层不支持"
        ),
        findings=findings,
    )
