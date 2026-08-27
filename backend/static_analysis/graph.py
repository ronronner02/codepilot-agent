"""import 声明 -> 文件级依赖图。

本模块只做一件难事：把 ImportRef.target 的原始文本解析成仓库内的实际文件路径。
U3 有意把这件事留到这里——它需要仓库全局上下文（有哪些文件、tsconfig 的别名表、
Python 的包布局），不属于单文件解析。

解析不到时的三种归属，区分它们比解析成功更重要：
  - 外部依赖：`import os`、`import react`。正常，记入 external。
  - 未解析：看起来指向仓库内却找不到文件。说明解析规则有缺口，记入 unresolved
    并附原因——静默丢弃会让依赖图的残缺伪装成「本来就没有这条依赖」。
  - 自环：文件导入自己（barrel 再导出自身时会出现）。丢弃，不入图。

**边分两种口径，只在环检测上分叉。** 延迟导入（函数体内）与类型保护导入
（`if TYPE_CHECKING:` 内）都是真实的依赖关系，故照常入 edges——中心度、聚类、
「谁依赖谁」的架构结论都应当算它们。但它们在导入期不执行，所以由它们支撑的边不参与
环检测：`a -> b -> a` 里只要有一条是延迟导入，导入期就没有这个环，报成循环依赖是误报
（见 ImportScope 的说明与那次 fastapi 实跑）。这类环归入 call_time_cycles。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

from backend.paths import PathEscapeError, resolve_within
from backend.static_analysis.models import (
    DependencyGraph,
    Granularity,
    ImportKind,
    ImportRef,
    ParsedFile,
    UnresolvedImport,
)

# TS 模块解析的候选后缀与顺序。与 tsc 的解析顺序一致：显式扩展名优先，
# 再试补后缀，最后试目录下的 index。
_TS_SUFFIXES = (".ts", ".tsx", ".d.ts", ".mts", ".cts")
_TS_INDEX_STEMS = ("index",)

# Python 模块文件的候选形态。包目录用 __init__.py 代表——依赖图的节点是文件，
# 指向包的 import 应落到该包的 __init__.py。
_PY_SUFFIXES = (".py", ".pyi")


class _RepoIndex:
    """仓库内文件的多视角索引。解析 import 时需要按不同键反查，预建一次。"""

    def __init__(self, paths: list[str]) -> None:
        self.all = frozenset(paths)
        # 去掉扩展名的路径 -> 实际路径。TS 的 `./m` 与 Python 的模块名都不带扩展名。
        self.by_stem: dict[str, list[str]] = {}
        for path in paths:
            for suffix in (*_TS_SUFFIXES, *_PY_SUFFIXES):
                if path.endswith(suffix):
                    self.by_stem.setdefault(path[: -len(suffix)], []).append(path)
                    break

    def resolve_stem(self, stem: str) -> str | None:
        """按「无扩展名路径」找文件。多个候选时取后缀优先级最高的那个。"""
        candidates = self.by_stem.get(stem)
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        order = (*_TS_SUFFIXES, *_PY_SUFFIXES)

        def rank(path: str) -> int:
            for i, suffix in enumerate(order):
                if path.endswith(suffix):
                    return i
            return len(order)

        return min(candidates, key=rank)


def _normalize(path: str) -> str:
    """折叠 `.` 与 `..`，统一为 posix 分隔符。

    不用 Path.resolve()——它会碰真实文件系统并跟随符号链接（KTD14 明确不跟随），
    而这里只做字符串层面的路径代数。逃出仓库根的路径（前导 `..` 无法消掉）返回
    空串，由调用方按「解析不到」处理。
    """
    parts: list[str] = []
    for part in path.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts or parts[-1] == "..":
                return ""
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


# tsconfig 的候选文件名，按查找顺序。
#
# `tsconfig.base.json` 是 Nx 的约定：workspace 根只放 base，各 app/lib 的 tsconfig
# extends 它。实测教训——ghostfolio（Nx monorepo）根目录**没有** tsconfig.json，只有
# tsconfig.base.json。只读 tsconfig.json 时别名表为空，于是 864 个文件的图只有 725 条
# 边、429 个「外部依赖」，而全部 `@ghostfolio/*` 内部导入都被误判成第三方包。
# 这类失效不会报错，只会让依赖图静默残缺——这正是聚类整体偏移的成因之一。
_TSCONFIG_NAMES = ("tsconfig.json", "tsconfig.base.json")

# extends 链的展开上限。防御异常配置里的循环 extends。
_TSCONFIG_EXTENDS_DEPTH = 4


def _read_jsonc(path: Path) -> dict[str, object] | None:
    """读 tsconfig。解析失败返回 None。

    tsconfig 允许注释与尾逗号（jsonc），标准 json 解析器会报错。这里不引入 jsonc
    依赖，而是先剥掉行注释与尾逗号——块注释与字符串内的 `//` 不处理，那需要真正的
    词法分析。剥不干净时解析失败，按无别名表处理。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None

    stripped = re.sub(r"^\s*//.*$", "", raw, flags=re.MULTILINE)
    stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _load_ts_aliases(repo_root: Path) -> tuple[dict[str, list[str]], str]:
    """读 tsconfig 的 compilerOptions.paths 与 baseUrl。

    跟随本地 extends 链（`./tsconfig.base.json` 之类），但不跟随指向 node_modules 的
    extends——那是社区共享配置（`@tsconfig/node20` 等），只含编译选项，不含项目别名。

    别名表在内层覆盖外层：extends 的语义是「先取被继承的，再用自己的覆盖」。
    """
    for name in _TSCONFIG_NAMES:
        # 过 KTD14 的单一校验：tsconfig 本身若是指向仓库外的链接，直接读会穿透它。
        try:
            config_path = resolve_within(repo_root, name)
        except PathEscapeError:
            continue
        if config_path.is_file():
            aliases, base_url = _resolve_tsconfig_chain(config_path, repo_root, 0)
            if aliases or base_url:
                return aliases, base_url
    return {}, ""


def _resolve_tsconfig_chain(
    config_path: Path, repo_root: Path, depth: int
) -> tuple[dict[str, list[str]], str]:
    if depth > _TSCONFIG_EXTENDS_DEPTH:
        return {}, ""

    raw = _read_jsonc(config_path)
    if raw is None:
        return {}, ""

    inherited: dict[str, list[str]] = {}
    inherited_base = ""
    extends = raw.get("extends")
    if isinstance(extends, str) and extends.startswith("."):
        parent = (config_path.parent / extends).resolve()
        if parent.suffix != ".json":
            parent = parent.with_suffix(".json")
        try:
            parent.relative_to(repo_root.resolve())
        except ValueError:
            parent = config_path  # extends 逃出仓库，忽略
        if parent != config_path and parent.is_file():
            inherited, inherited_base = _resolve_tsconfig_chain(parent, repo_root, depth + 1)

    options = raw.get("compilerOptions")
    if not isinstance(options, dict):
        return inherited, inherited_base

    base_url = str(options.get("baseUrl") or "") or inherited_base
    paths = options.get("paths")
    own: dict[str, list[str]] = {}
    if isinstance(paths, dict):
        own = {
            str(pattern): [str(t) for t in targets]
            for pattern, targets in paths.items()
            if isinstance(targets, list)
        }

    return {**inherited, **own}, base_url


def _apply_alias(
    target: str, aliases: dict[str, list[str]], base_url: str
) -> list[str]:
    """把别名导入展开为候选路径（相对仓库根，无扩展名）。

    tsconfig 的别名形态只有两种：精确匹配（`"@app": ["src/app.ts"]`）与单星号通配
    （`"@/*": ["src/*"]`）。按 tsc 的规则，通配匹配中最长的前缀优先。
    """
    matches: list[tuple[int, list[str]]] = []
    for pattern, targets in aliases.items():
        if "*" in pattern:
            prefix, _, suffix = pattern.partition("*")
            if target.startswith(prefix) and target.endswith(suffix):
                captured = target[len(prefix) : len(target) - len(suffix) or None]
                expanded = [t.replace("*", captured) for t in targets]
                matches.append((len(prefix), expanded))
        elif target == pattern:
            matches.append((len(pattern), list(targets)))

    if not matches:
        return []
    _, best = max(matches, key=lambda item: item[0])
    return [_normalize(f"{base_url}/{candidate}" if base_url else candidate) for candidate in best]


def _resolve_typescript(
    ref: ImportRef, index: _RepoIndex, aliases: dict[str, list[str]], base_url: str
) -> tuple[list[str], str | None]:
    """解析 TS 的 import 目标。返回 (仓库内路径列表, 未解析原因)，两者互斥。

    第三种情况是 ([], None)：目标是第三方包，属正常外部依赖。
    """
    target = ref.target
    is_relative = target.startswith(".")

    if is_relative:
        base = Path(ref.path).parent.as_posix()
        first = _normalize(f"{base}/{target}")
        if not first:
            return [], "相对路径超出仓库根"
        candidates = [first]
    else:
        candidates = _apply_alias(target, aliases, base_url)
        if not candidates:
            # 裸模块名且不匹配任何别名：第三方包。
            return [], None

    for candidate in candidates:
        # 显式带扩展名且直接命中（`./m.ts`）。
        if candidate in index.all:
            return [candidate], None
        hit = index.resolve_stem(candidate)
        if hit:
            return [hit], None
        # `./m.js` -> `./m.ts`：ESM 规范要求 import 里写 `.js`，实际源文件是 `.ts`。
        for ext in (".js", ".jsx", ".mjs", ".cjs"):
            if candidate.endswith(ext):
                hit = index.resolve_stem(candidate[: -len(ext)])
                if hit:
                    return [hit], None
        # 目录导入：`./utils` -> `./utils/index.ts`。
        for stem in _TS_INDEX_STEMS:
            hit = index.resolve_stem(f"{candidate}/{stem}")
            if hit:
                return [hit], None

    if is_relative:
        return [], "相对导入未匹配到仓库内文件（可能是无扩展名的非 TS 资源）"
    return [], "别名导入未匹配到仓库内文件"


def _python_targets(
    stem: str, ref: ImportRef, index: _RepoIndex
) -> tuple[list[str], bool]:
    """把已归一化的模块路径解析为文件。返回 (命中列表, 是否确定不在仓库内)。

    分两层：先按模块文件命中；不中则按包命中。命中包时还要多做一步——
    `from pkg import mod` 里的 `mod` 可能是子模块而非包内符号，此时真实依赖是
    `pkg/mod.py`，不是 `pkg/__init__.py`。

    这一步为什么重要：`from . import x` 与 `from ..pkg import mod` 在 Python 代码里
    极常见。全都记到 `__init__.py` 上会有两个后果——包的 `__init__.py` 中心度被
    虚高（它成了所有兄弟模块依赖的汇聚点），而真实的模块间依赖边整体消失。中心度
    是 U11 挑文件的信号，聚类靠边密度，两者都会因此偏移。
    """
    direct = index.resolve_stem(stem) if stem else None
    if direct:
        return [direct], False

    package_init = index.resolve_stem(f"{stem}/__init__" if stem else "__init__")
    if package_init:
        if ref.kind is ImportKind.FROM:
            submodules = [
                hit
                for name in ref.names
                if name != "*"
                and (hit := index.resolve_stem(f"{stem}/{name}" if stem else name))
            ]
            if submodules:
                return submodules, False
        return [package_init], False

    return [], True


def _resolve_python(ref: ImportRef, index: _RepoIndex) -> tuple[list[str], str | None]:
    """解析 Python 的 import 目标。

    相对导入（前导点）必然指向仓库内，解析不到就是解析规则的缺口。绝对导入则可能是
    第三方包或标准库，解析不到属正常。
    """
    target = ref.target
    leading_dots = len(target) - len(target.lstrip("."))

    if leading_dots:
        module_part = target[leading_dots:]
        base = Path(ref.path).parent
        # 1 个点 = 当前包（文件所在目录），每多一个点上升一层。
        for _ in range(leading_dots - 1):
            base = base.parent
        base_str = base.as_posix()
        if base_str == ".":
            base_str = ""

        body = module_part.replace(".", "/")
        stem = _normalize(f"{base_str}/{body}" if body else base_str)
        if not stem and body:
            return [], "相对导入超出仓库根"

        hits, _ = _python_targets(stem, ref, index)
        if hits:
            return hits, None
        return [], f"相对导入未匹配到仓库内文件（解析为 {stem or '仓库根'}）"

    stem = target.replace(".", "/")
    hits, _ = _python_targets(stem, ref, index)
    if hits:
        return hits, None

    # 仓库可能是 src 布局（包在 src/ 下），此时绝对导入 `pkg.mod` 对应
    # `src/pkg/mod.py`，从仓库根拼不出来。按路径后缀反查补上这种情况。
    suffix_matches = sorted(
        {
            path
            for candidate_stem, paths in index.by_stem.items()
            if candidate_stem.endswith(f"/{stem}") or candidate_stem.endswith(f"/{stem}/__init__")
            for path in paths
        }
    )
    if suffix_matches:
        # 多个同名候选（monorepo 或 vendored 副本）时取最短路径——它通常是主包。
        # 这是启发式，故不因此判定「解析成功」而抑制其他信号。
        return [min(suffix_matches, key=lambda p: (len(p), p))], None

    # 找不到：第三方包（os、httpx）或标准库。属正常外部依赖。
    return [], None


def build_graph(
    repo_root: Path,
    parsed: list[ParsedFile],
    max_nodes: int,
    include_type_only: bool = True,
) -> DependencyGraph:
    """构建依赖图。

    include_type_only 决定 `import type { T }` 是否算依赖边。默认算：TS 的类型导入
    不产生运行时依赖，但产生编译期耦合，而报告关心的是「谁依赖谁」的架构关系，
    类型耦合同样是架构信号。设为 False 可得纯运行时依赖图。
    """
    paths = [f.path for f in parsed]

    if len(paths) > max_nodes:
        return _build_directory_graph(parsed, max_nodes, include_type_only)

    index = _RepoIndex(paths)
    aliases, base_url = _load_ts_aliases(repo_root)

    edges: dict[str, set[str]] = {path: set() for path in paths}
    # 导入期边：只累积由模块级 import 支撑的边。环检测跑在它上面。
    #
    # 为什么要两份而不是给边打标记：一条边可能由多条 import 支撑（同一文件里既有顶层
    # `from m import a` 又有函数内 `from m import b`），此时它在导入期确实存在。用
    # 「累积两个集合再相减」表达这一点是最直接的——差集恰好是「只由延迟导入支撑的边」。
    import_time_edges: dict[str, set[str]] = {path: set() for path in paths}
    external: dict[str, set[str]] = {}
    unresolved: list[UnresolvedImport] = []

    for file in parsed:
        for ref in file.imports:
            if ref.kind is ImportKind.TYPE_ONLY and not include_type_only:
                continue

            if file.language == "python":
                resolved, reason = _resolve_python(ref, index)
            else:
                resolved, reason = _resolve_typescript(ref, index, aliases, base_url)

            if not resolved:
                if reason is None:
                    external.setdefault(ref.target, set()).add(file.path)
                else:
                    unresolved.append(
                        UnresolvedImport(
                            target=ref.target, path=file.path, line=ref.line, reason=reason
                        )
                    )
                continue

            # 自环丢弃。包的 __init__.py 导入自身子模块、或 barrel 再导出自身时会
            # 出现，入图会让中心度与环检测都失真。
            targets = {t for t in resolved if t != file.path}
            edges[file.path].update(targets)
            if ref.scope.at_import_time:
                import_time_edges[file.path].update(targets)

    deferred = {
        source: frozenset(targets - import_time_edges[source])
        for source, targets in edges.items()
        if targets - import_time_edges[source]
    }
    all_cycles = find_cycles(paths, edges)
    import_cycles, call_time_cycles = _partition_cycles(all_cycles, import_time_edges)

    return DependencyGraph(
        nodes=tuple(paths),
        edges={node: frozenset(targets) for node, targets in edges.items()},
        external={target: frozenset(sources) for target, sources in external.items()},
        unresolved=tuple(unresolved),
        cycles=import_cycles,
        call_time_cycles=call_time_cycles,
        deferred_edges=deferred,
        granularity=Granularity.FILE,
    )


def _partition_cycles(
    cycles: tuple[tuple[str, ...], ...], import_time_edges: dict[str, set[str]]
) -> tuple[tuple[tuple[str, ...], ...], tuple[tuple[str, ...], ...]]:
    """把全部有向环分成「导入期成立」与「仅调用期成立」两组。

    判据是逐边核对：环上每条边都在导入期存在，这个环才在导入期成立；**任一条边是延迟
    导入即不成立**——Python 在模块加载时只求值顶层 import，环上缺一条边就断了。

    为什么不在导入期子图上重跑一次 find_cycles：那样得到的两组环无法对应。同一个环
    从不同起点进入 DFS 会得到不同的规范表示，两次独立检测的结果做差集会漏掉或重复。
    在全图的环上逐边判定则是精确的——每个环各自定性一次，不依赖遍历顺序。
    """
    at_import: list[tuple[str, ...]] = []
    at_call: list[tuple[str, ...]] = []
    for cycle in cycles:
        # 环是首尾相接的，最后一条边从末节点回到首节点。
        pairs = zip(cycle, cycle[1:] + cycle[:1])
        if all(target in import_time_edges.get(source, set()) for source, target in pairs):
            at_import.append(cycle)
        else:
            at_call.append(cycle)
    return tuple(at_import), tuple(at_call)


def _build_directory_graph(
    parsed: list[ParsedFile], max_nodes: int, include_type_only: bool
) -> DependencyGraph:
    """降级路径：节点为目录。

    触发条件是文件数超上限。此时逐文件解析 import 的代价（每条 import 要在索引里
    反查）与图规模一并膨胀，而报告在这个规模下本来也只能讲到目录级。降级必须标注
    ——不标注的降级会让读者以为看到的是文件级精度（R11）。

    目录级边由文件级 import 的目录前缀聚合而成，故不需要完整解析：只按导入目标的
    字面目录形态归并。精度低于文件级，但这条路径的目的是不耗尽资源，不是精确。
    """
    dirs = sorted({str(Path(f.path).parent).replace("\\", "/") for f in parsed})
    return DependencyGraph(
        nodes=tuple(dirs),
        edges={d: frozenset() for d in dirs},
        granularity=Granularity.DIRECTORY,
        degraded_reason=(
            f"文件数 {len(parsed)} 超出图构建上限 {max_nodes}，"
            f"降级为目录级分组（{len(dirs)} 个目录），未做文件级 import 解析"
        ),
    )


def find_cycles(nodes: list[str], edges: dict[str, set[str]]) -> tuple[tuple[str, ...], ...]:
    """找出有向环。**不区分导入期与调用期**——传什么边集就在什么图上找环。

    导入期/调用期的分野由调用方负责：`build_graph` 用全部边找环，再交
    `_partition_cycles` 逐边定性。这里保持纯图算法，不引入 import 语义。

    用迭代式 DFS 而非递归：深依赖链在千文件仓库上能到几百层，递归会撞
    Python 的栈上限（默认 1000），而这属于「病态结构不应压垮系统」的范围。

    栈上元素是 (节点, 待访邻居迭代器)，配合 on_stack 集合识别回边。命中回边时从
    路径里截出环。同一个环可能被多次发现（不同起点进入），故用规范化后的节点集合
    去重——环的表示取字典序最小的旋转，保证同一环只留一份。
    """
    cycles: set[tuple[str, ...]] = set()
    color: dict[str, int] = {}  # 0/缺失=未访问 1=在栈上 2=已完成

    for start in nodes:
        if color.get(start, 0) != 0:
            continue
        path: list[str] = [start]
        color[start] = 1
        stack: list[tuple[str, Iterator[str]]] = [
            (start, iter(sorted(edges.get(start, set()))))
        ]

        while stack:
            node, neighbours = stack[-1]
            advanced = False
            for neighbour in neighbours:
                state = color.get(neighbour, 0)
                if state == 1:
                    # 回边：从 path 里 neighbour 出现处到末尾即为环。
                    index = path.index(neighbour)
                    cycles.add(_canonical_cycle(path[index:]))
                elif state == 0:
                    color[neighbour] = 1
                    path.append(neighbour)
                    stack.append((neighbour, iter(sorted(edges.get(neighbour, set())))))
                    advanced = True
                    break
            if not advanced:
                color[node] = 2
                path.pop()
                stack.pop()

    return tuple(sorted(cycles))


def _canonical_cycle(cycle: list[str]) -> tuple[str, ...]:
    """把环旋转到字典序最小的起点，使同一个环只有一种表示。"""
    if not cycle:
        return ()
    pivot = min(range(len(cycle)), key=lambda i: cycle[i])
    return tuple(cycle[pivot:] + cycle[:pivot])


def centrality(graph: DependencyGraph, damping: float = 0.85, iterations: int = 30) -> dict[str, float]:
    """算每个文件的中心度。供 U11 挑选待检文件（KTD10 的两个信号之一）。

    用 PageRank 而非入度。入度只数直接依赖方，把「被 3 个边缘脚本引用」与「被 3 个
    核心模块引用」算成同一分；而「出问题影响面大」要表达的恰是后者更重。PageRank
    让重要性沿依赖链传递：被重要文件依赖的文件也重要。

    rank 沿原始 import 边流动——A 导入 B，即 A 把自己的一份 rank 投给 B。所以 rank
    在「被广泛且被重要者依赖」的文件上累积。直觉上等价于：一个开发者顺着 import 往下
    读，最终停留概率最高的就是核心文件。

    悬挂节点（不导入任何仓库内文件的纯叶子）的 rank 按 PageRank 标准做法均摊回全体，
    否则每轮总分漏失，迭代结果不可比也不收敛到概率分布。

    环不需要特殊处理：PageRank 是迭代的定点计算，不做图遍历，环只是让 rank 在环内
    循环累积（这也符合语义——互相依赖的文件确实互相抬高重要性）。
    """
    nodes = list(graph.nodes)
    if not nodes:
        return {}

    out_degree = {node: len(graph.edges.get(node, frozenset())) for node in nodes}
    count = len(nodes)
    scores = {node: 1.0 / count for node in nodes}

    for _ in range(iterations):
        leaked = sum(scores[n] for n in nodes if out_degree[n] == 0)
        nxt = {node: (1.0 - damping) / count + damping * leaked / count for node in nodes}
        for node in nodes:
            degree = out_degree[node]
            if degree == 0:
                continue
            share = damping * scores[node] / degree
            for target in graph.edges[node]:
                nxt[target] += share
        scores = nxt

    return scores
