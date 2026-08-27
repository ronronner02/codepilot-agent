"""静态解析层的数据类型。

这些是 U3 到 U11 共用的骨架结构。设计约束：所有位置信息用仓库相对路径 + 行号，
不用绝对路径（KTD 的可追溯性要求报告里的引用能落到具体文件行）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SymbolKind(str, Enum):
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    INTERFACE = "interface"
    TYPE_ALIAS = "type_alias"


@dataclass(frozen=True)
class Symbol:
    """一个定义。start_line / end_line 是 1-based 闭区间，与编辑器显示一致。"""

    name: str
    kind: SymbolKind
    path: str
    start_line: int
    end_line: int
    parent: str | None = None
    """所属类名。方法有值，顶层函数为 None。"""

    @property
    def qualified_name(self) -> str:
        return f"{self.parent}.{self.name}" if self.parent else self.name


class ImportKind(str, Enum):
    MODULE = "module"
    """`import os` / `import def from './m'`"""
    FROM = "from"
    """`from pkg import x` / `import { a } from './m'`"""
    TYPE_ONLY = "type_only"
    """`import type { T } from './m'` —— 不产生运行时依赖，但产生编译期耦合。"""
    REEXPORT = "reexport"
    """`export * from './barrel'` —— 依赖会传递给 barrel 的消费者。"""


class ImportScope(str, Enum):
    """这条 import 在**导入期**是否执行。

    为什么需要这个区分（端到端实跑暴露的误报）：评审曾报出
    `_compat/v2.py -> params.py -> datastructures.py -> _compat/v2.py` 的循环依赖，
    但逐边核对后其中两条是函数作用域的延迟导入——导入期不存在这个环，作者正是用
    延迟导入打破它的。同一份报告的「依赖关系」一节反而正确地把那处延迟导入描述为
    「主动规避循环依赖的例证」，两处口径互相矛盾：模块子 Agent 读了源码，评审只看图。

    根因是依赖图不区分「导入期依赖」与「调用期依赖」，而环的成立与否恰恰取决于这个
    区分——Python 在模块加载时求值顶层 import，函数体内的 import 要等到调用才执行。
    """

    MODULE = "module"
    """顶层，或类体内。两者都在模块加载时执行，故都算导入期依赖。

    类体也算的理由：`class C: from m import x` 里的 import 在 class 语句求值时就执行，
    与顶层无实质差别（实测确认节点链为 `block < class_definition[body] < module`）。
    """
    DEFERRED = "deferred"
    """函数或方法体内。调用期才执行，导入期不构成依赖。"""
    TYPE_GUARDED = "type_guarded"
    """`if TYPE_CHECKING:` 块内。运行期永不执行，只服务类型检查器。

    与 DEFERRED 分开记而非合并：两者「导入期不成立」的结论相同，但成因不同——前者
    是运行时的延迟求值，后者根本不会在运行时执行。报告要解释「为什么这个环不成立」
    时，这两句话不一样。
    """

    @property
    def at_import_time(self) -> bool:
        """导入期是否执行。环检测据此判断一条边算不算。"""
        return self is ImportScope.MODULE


@dataclass(frozen=True)
class ImportRef:
    """一条 import 声明。target 是原始文本，尚未解析为仓库内路径。

    解析成实际文件路径是 U4 的职责——那需要 tsconfig 的路径别名、Python 的包
    结构等上下文，不属于单文件解析。
    """

    target: str
    kind: ImportKind
    path: str
    line: int
    names: tuple[str, ...] = ()
    """具名导入的名字。`from x import a, b` -> ('a', 'b')；模块导入为空。"""
    scope: ImportScope = ImportScope.MODULE
    """这条 import 在导入期是否执行。默认 MODULE 使既有构造点（含测试）语义不变。"""


@dataclass
class ParsedFile:
    path: str
    language: str
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[ImportRef] = field(default_factory=list)


@dataclass
class UnparsedFile:
    """未做符号级解析的文件。原因要能落到报告里（R11）。"""

    path: str
    reason: str


@dataclass
class ParseOutcome:
    parsed: list[ParsedFile] = field(default_factory=list)
    unparsed: list[UnparsedFile] = field(default_factory=list)

    @property
    def symbol_count(self) -> int:
        return sum(len(f.symbols) for f in self.parsed)


# ── U4：依赖图、模块聚类、入口点 ──────────────────────────────────────
#
# 以下类型是 U5 state、U6 Planner、U7 工具层、U11 Reviewer 的共同输入契约，
# 故与单文件解析结果同置一处。


class Granularity(str, Enum):
    FILE = "file"
    """正常情况：节点是文件。"""
    DIRECTORY = "directory"
    """降级情况：节点是目录。仓库节点数超上限时的退路，避免耗尽内存。"""


@dataclass(frozen=True)
class UnresolvedImport:
    """看起来指向仓库内、但没解析到文件的 import。

    与「外部依赖」区分开：第三方包解析不到是正常的，仓库内相对导入解析不到说明
    解析规则有缺口（缺扩展名、缺 tsconfig 别名、动态路径）。这类缺口要能落到
    报告里（R11），否则依赖图的残缺会被静默当成「本来就没有依赖」。
    """

    target: str
    path: str
    line: int
    reason: str


@dataclass
class DependencyGraph:
    """文件级（或降级后目录级）有向依赖图。

    edges 的方向是「导入方 -> 被导入方」。反向边按需算（reverse_edges），不双向
    存储——两份数据同步是 bug 来源，而图规模有上限，重算成本可忽略。

    **edges 含全部依赖，不区分导入期与调用期。** 这是有意的：延迟导入仍是真实的
    依赖关系，"谁依赖谁"的架构结论、中心度、聚类都应当算它。只有环的成立与否需要这个
    区分（见 cycles 与 call_time_cycles），所以区分信息单独放在 deferred_edges 里，
    而不是从 edges 里剔除。
    """

    nodes: tuple[str, ...] = ()
    edges: dict[str, frozenset[str]] = field(default_factory=dict)
    external: dict[str, frozenset[str]] = field(default_factory=dict)
    """仓库外依赖：原始 import 目标 -> 导入它的文件集合。"""
    unresolved: tuple[UnresolvedImport, ...] = ()
    cycles: tuple[tuple[str, ...], ...] = ()
    """**导入期成立**的环。环上每条边都由至少一条模块级 import 支撑。

    语义收窄自「图上的全部有向环」：延迟导入构成的环在导入期不存在（Python 在模块
    加载时只求值顶层 import），报成循环依赖是误报。那些环归入 call_time_cycles。
    """
    call_time_cycles: tuple[tuple[str, ...], ...] = ()
    """仅在**调用期**成立的环：环上至少有一条边只由延迟导入或类型保护导入支撑。

    通常是作者主动规避循环依赖的手段——正是那条延迟导入打破了导入期的环。所以它不作
    为发现产出，只在评审范围说明里计数（与「无引用文件」同一取舍：误报率高的信号只留
    计数）。
    """
    deferred_edges: dict[str, frozenset[str]] = field(default_factory=dict)
    """只由延迟/类型保护导入支撑的边，是 edges 的子集。

    保留它而非只存一个布尔标记：解释「为什么这个环在导入期不成立」时要能指出是哪条边
    延迟的，而这需要边一级的粒度。同一对文件间既有顶层导入又有延迟导入时不算在内——
    那条边在导入期确实存在。
    """
    granularity: Granularity = Granularity.FILE
    degraded_reason: str | None = None

    def reverse_edges(self) -> dict[str, frozenset[str]]:
        collected: dict[str, set[str]] = {node: set() for node in self.nodes}
        for source, targets in self.edges.items():
            for target in targets:
                collected.setdefault(target, set()).add(source)
        return {node: frozenset(sources) for node, sources in collected.items()}


@dataclass(frozen=True)
class Module:
    """一个模块分组。name 取成员的公共目录前缀，空前缀记为仓库根。"""

    name: str
    files: tuple[str, ...]
    internal_edges: int
    """组内边数。与 external_edges 一并构成「这个分组像不像一个模块」的依据。"""
    external_edges: int
    origin: str
    """分组来历：directory / split / merged。让聚类结果可解释，便于人工核对。"""


class EntryKind(str, Enum):
    MANIFEST = "manifest"
    """声明式入口：pyproject scripts、package.json main/bin/exports、setup.py。"""
    CONVENTION = "convention"
    """语言约定：__main__.py、main.py 之类。"""
    GRAPH_ROOT = "graph_root"
    """图上零入度节点。信号最弱——孤立文件也是零入度。"""


@dataclass(frozen=True)
class EntryPoint:
    path: str
    kind: EntryKind
    evidence: str
    """判定依据的原文（哪个字段、哪个文件），供报告落到可核验的位置。"""
