"""代码切块。按 AST 边界切分（KTD6），不按固定字符数。

固定长度切分会把函数腰斩，检索命中半个函数体对回答代码问题几乎无用——这是 KTD6 的理由。

**切块粒度的选择：方法各自成块，而非整个类一块。** 一个 500 行的类若是一块，检索「认证
怎么做的」会命中整个类，其中大部分内容与查询无关，相似度被稀释。方法各自成块则每块聚焦
一件事。类级上下文不丢：类声明与类属性单独成一块（类头块），方法块的元数据带 `parent`。

**超长函数按语句边界二次切分，带重叠。** 语句边界来自语法树（body block 的直接子节点），
不在字符中间断开。重叠是必要的：切分点两侧的语句常有数据依赖，不带重叠时后半块会以一个
来历不明的变量开头。

**密钥形态文件不产生任何切块（KTD17）。** 索引是持久化落盘的，把密钥写进去会让问答链路
有机会检索出来。这与 Reviewer 扫描这些文件不冲突——那边只报告不落盘。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tree_sitter as ts

from backend.review.security import is_secret_shaped_file
from backend.static_analysis.parser import ParsedTree, node_text, parse_tree

# 单块的行数上限。超出则按语句边界二次切分。
#
# 取 120 行的依据：多数函数在此之下，能整块保留；超出的通常是需要拆的长函数，而 120 行
# 的块在 embedding 的上下文窗口内仍是合理输入。
DEFAULT_MAX_CHUNK_LINES = 120

# 二次切分的重叠行数。
#
# 取 8 行：足够带上切分点前的变量赋值与循环头，让后半块可读；再多则重复内容占比过高，
# 检索时同一段代码会在多个块里命中。
DEFAULT_OVERLAP_LINES = 8

# 块的最小行数。低于此值的碎片合并进相邻块。
#
# 单行块（一个 `import` 或一个属性声明）单独向量化没有检索价值，还会稀释结果列表。
MIN_CHUNK_LINES = 2

_FUNCTION_NODES = frozenset(
    {
        "function_definition",
        "function_declaration",
        "generator_function_declaration",
        "method_definition",
    }
)

# 箭头函数与函数表达式的载体节点。
#
# **不能只收 _FUNCTION_NODES。** `arrow_function` 自己不带名字，名字在外层的
# variable_declarator（`const Panel = () => ...`）或 public_field_definition
# （`class C { handle = () => ... }`）上。漏掉这两类的后果是 React 组件完全不进索引
# ——它们绝大多数是箭头函数，而这正是计划测试场景点名要覆盖的形态。
_FUNCTION_VALUE_NODES = frozenset({"arrow_function", "function_expression"})
_FUNCTION_CARRIER_NODES = frozenset({"variable_declarator", "public_field_definition"})

_CLASS_NODES = frozenset(
    {"class_definition", "class_declaration", "abstract_class_declaration"}
)

# 包裹在定义外层但属于定义一部分的节点。与 extract.py 的 _DEFINITION_WRAPPERS 同源：
# 行范围要含装饰器与 export，理由见那里的说明。
_WRAPPERS = frozenset(
    {"decorated_definition", "export_statement", "lexical_declaration", "variable_declaration"}
)


@dataclass(frozen=True)
class Chunk:
    """一个可检索的代码块。

    元数据是检索结果可追溯的前提：回答代码问题时要能说「这段在 x.py 第 n 行」，
    而 path 用仓库相对路径（绝对路径在容器内外不一致，也会泄露主机目录结构）。
    """

    path: str
    start_line: int
    end_line: int
    content: str
    symbol: str = ""
    """所属符号的限定名。模块级代码为空。"""
    parent: str = ""
    """所属类名。方法块有值。"""
    kind: str = "code"
    """`function` / `method` / `class_header` / `module` / `function_part`。"""
    part_index: int = 0
    """二次切分后的序号，从 1 起。未切分的块为 0。"""

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1

    def identifier(self) -> str:
        """块的稳定标识，用作向量库的主键。

        含行号：同名符号在文件里可能出现多次（条件定义、重载），只用符号名会撞键。
        """
        suffix = f"#{self.part_index}" if self.part_index else ""
        return f"{self.path}:{self.start_line}-{self.end_line}{suffix}"


def _definition_node(name_node: ts.Node) -> ts.Node:
    node = name_node.parent
    if node is None:
        return name_node
    while node.parent is not None and node.parent.type in _WRAPPERS:
        parent = node.parent
        if parent.type in {"lexical_declaration", "variable_declaration"}:
            declarators = [
                c for c in parent.named_children if c.type == "variable_declarator"
            ]
            if len(declarators) != 1:
                break
        node = parent
    return node


def _lines_of(source_lines: list[str], start: int, end: int) -> str:
    return "\n".join(source_lines[start - 1 : end])


def _split_long_body(
    node: ts.Node,
    source_lines: list[str],
    max_lines: int,
    overlap: int,
) -> list[tuple[int, int]]:
    """把超长定义按语句边界切成多段，返回行范围列表。

    语句边界取 body block 的直接子节点起始行。在字符中间断开会产生语法不完整的片段，
    而那种片段对回答代码问题没有价值——KTD6 的理由同样适用于二次切分。

    找不到 body 或语句太少时退化为按行硬切：那是兜底，不是常态（超长而无内部语句结构的
    定义极少见，通常是长字符串常量）。
    """
    start = node.start_point[0] + 1
    end = node.end_point[0] + 1

    body = node.child_by_field_name("body")
    boundaries = [child.start_point[0] + 1 for child in body.named_children] if body else []
    # 语句边界至少要有两个才谈得上切分。
    if len(boundaries) < 2:
        return _split_by_lines(start, end, max_lines, overlap)

    ranges: list[tuple[int, int]] = []
    segment_start = start
    for boundary in boundaries:
        if boundary - segment_start + 1 < max_lines:
            continue
        # 在这条语句之前切断，让语句本身完整落进下一段。
        ranges.append((segment_start, boundary - 1))
        segment_start = max(start, boundary - overlap)

    if segment_start <= end:
        ranges.append((segment_start, end))

    # 只切出一段说明没有合适的边界，退化为按行切。
    return ranges if len(ranges) > 1 else _split_by_lines(start, end, max_lines, overlap)


def _split_by_lines(
    start: int, end: int, max_lines: int, overlap: int
) -> list[tuple[int, int]]:
    """按行硬切。仅在找不到语句边界时使用。"""
    ranges: list[tuple[int, int]] = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + max_lines - 1, end)
        ranges.append((cursor, stop))
        if stop >= end:
            break
        cursor = max(start, stop - overlap + 1)
    return ranges


def _enclosing_class_name(node: ts.Node, source: bytes) -> str:
    """往上找所属类名。判定规则与 extract.py 的 _enclosing_class 一致。"""
    current = node.parent
    while current is not None:
        if current.type in _CLASS_NODES:
            name = current.child_by_field_name("name")
            return node_text(source, name) if name is not None else ""
        if current.type in _FUNCTION_NODES:
            return ""
        current = current.parent
    return ""


def _is_function_carrier(node: ts.Node) -> bool:
    """节点是否是「带名字的箭头函数/函数表达式」载体。"""
    if node.type not in _FUNCTION_CARRIER_NODES:
        return False
    value = node.child_by_field_name("value")
    return value is not None and value.type in _FUNCTION_VALUE_NODES


def _collect_definitions(root: ts.Node) -> list[ts.Node]:
    """收集全部函数与类定义节点，按出现顺序。

    含箭头函数载体（见 _FUNCTION_CARRIER_NODES 的说明）。
    """
    found: list[ts.Node] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in _FUNCTION_NODES or node.type in _CLASS_NODES or _is_function_carrier(node):
            found.append(node)
        stack.extend(reversed(node.children))
    found.sort(key=lambda n: (n.start_point[0], n.start_byte))
    return found


def _chunk_definition(
    node: ts.Node,
    parsed: ParsedTree,
    source_lines: list[str],
    max_lines: int,
    overlap: int,
) -> list[Chunk]:
    """把一个函数或方法定义切成块。超长则二次切分。"""
    name_node = node.child_by_field_name("name")
    symbol = node_text(parsed.source, name_node) if name_node is not None else "<匿名>"
    parent = _enclosing_class_name(node, parsed.source)
    kind = "method" if parent or node.type == "method_definition" else "function"

    outer = _definition_node(name_node) if name_node is not None else node
    start = outer.start_point[0] + 1
    end = outer.end_point[0] + 1

    if end - start + 1 <= max_lines:
        return [
            Chunk(
                path=parsed.path,
                start_line=start,
                end_line=end,
                content=_lines_of(source_lines, start, end),
                symbol=f"{parent}.{symbol}" if parent else symbol,
                parent=parent,
                kind=kind,
            )
        ]

    ranges = _split_long_body(node, source_lines, max_lines, overlap)
    return [
        Chunk(
            path=parsed.path,
            start_line=begin,
            end_line=stop,
            content=_lines_of(source_lines, begin, stop),
            symbol=f"{parent}.{symbol}" if parent else symbol,
            parent=parent,
            kind="function_part",
            part_index=index,
        )
        for index, (begin, stop) in enumerate(ranges, start=1)
    ]


def _class_header_chunk(
    node: ts.Node,
    parsed: ParsedTree,
    source_lines: list[str],
    member_starts: list[int],
) -> Chunk | None:
    """类声明到第一个方法之前的部分。

    单独成块的理由：类属性、字段声明、装饰器（`@dataclass`、`@Component`）承载了这个类
    「是什么」的信息，而方法块只讲「做什么」。丢掉类头会让检索「有哪些数据结构」这类
    查询找不到落点。

    **member_starts 只含方法，不含属性声明。** 把属性也当成 member 会让类头切在第一个
    属性之前，只剩装饰器与 `class X:` 两行——恰好丢掉了这个块存在的理由。
    """
    name_node = node.child_by_field_name("name")
    class_name = node_text(parsed.source, name_node) if name_node is not None else "<匿名类>"
    outer = _definition_node(name_node) if name_node is not None else node
    start = outer.start_point[0] + 1

    first_member = min((s for s in member_starts if s > start), default=None)
    end = (first_member - 1) if first_member is not None else outer.end_point[0] + 1

    if end - start + 1 < MIN_CHUNK_LINES:
        # 只有一行类声明、没有类级属性——没有独立检索价值。
        return None

    return Chunk(
        path=parsed.path,
        start_line=start,
        end_line=end,
        content=_lines_of(source_lines, start, end),
        symbol=class_name,
        kind="class_header",
    )


def chunk_file(
    repo_root: Path,
    rel_path: str,
    max_file_bytes: int,
    max_chunk_lines: int = DEFAULT_MAX_CHUNK_LINES,
    overlap_lines: int = DEFAULT_OVERLAP_LINES,
) -> list[Chunk]:
    """把单个文件切成块。不可解析或属密钥形态时返回空列表。

    密钥形态的判断在最前面：KTD17 要求这类文件不产生任何切块，而它们的扩展名多数本就
    不可解析，但 `.env.ts` 这类混合命名存在，早退出比依赖解析失败更可靠。
    """
    if is_secret_shaped_file(rel_path):
        return []

    parsed = parse_tree(repo_root, rel_path, max_file_bytes)
    if not isinstance(parsed, ParsedTree):
        return []

    source_lines = parsed.source.decode("utf-8", errors="replace").splitlines()
    if not source_lines:
        return []

    definitions = _collect_definitions(parsed.tree.root_node)
    chunks: list[Chunk] = []
    covered: set[int] = set()

    for node in definitions:
        if node.type in _CLASS_NODES:
            body = node.child_by_field_name("body")
            # 只把方法算作 member：属性声明属于类头的一部分，见 _class_header_chunk。
            member_starts = (
                [
                    child.start_point[0] + 1
                    for child in body.named_children
                    if child.type in _FUNCTION_NODES
                    or _is_function_carrier(child)
                    or (
                        child.type == "decorated_definition"
                        and any(g.type in _FUNCTION_NODES for g in child.named_children)
                    )
                ]
                if body
                else []
            )
            header = _class_header_chunk(node, parsed, source_lines, member_starts)
            if header is not None:
                chunks.append(header)
                covered.update(range(header.start_line, header.end_line + 1))
            continue

        # 嵌套函数已被外层函数的块覆盖，跳过以免同一段代码进两个块。
        if node.start_point[0] + 1 in covered:
            continue

        produced = _chunk_definition(node, parsed, source_lines, max_chunk_lines, overlap_lines)
        chunks.extend(produced)
        for chunk in produced:
            covered.update(range(chunk.start_line, chunk.end_line + 1))

    chunks.extend(
        _module_level_chunks(parsed, source_lines, covered, max_chunk_lines, overlap_lines)
    )
    chunks.sort(key=lambda c: (c.start_line, c.part_index))
    return chunks


def _module_level_chunks(
    parsed: ParsedTree,
    source_lines: list[str],
    covered: set[int],
    max_chunk_lines: int,
    overlap_lines: int,
) -> list[Chunk]:
    """定义之外的模块级代码。

    为什么要它：import 段与模块常量常是查询的落点（「这个项目用了什么库」、「配置项在
    哪」）。只切定义会让这些内容完全不可检索。

    连续的未覆盖行归为一块；块太小则丢弃（单个 import 无独立检索价值）。
    """
    total = len(source_lines)
    chunks: list[Chunk] = []
    cursor = 1

    while cursor <= total:
        if cursor in covered or not source_lines[cursor - 1].strip():
            cursor += 1
            continue

        start = cursor
        while cursor <= total and cursor not in covered:
            cursor += 1
        end = cursor - 1

        # 去掉尾部空行，避免块以空白结尾。
        while end > start and not source_lines[end - 1].strip():
            end -= 1

        if end - start + 1 < MIN_CHUNK_LINES:
            continue

        for index, (begin, stop) in enumerate(
            _split_by_lines(start, end, max_chunk_lines, overlap_lines), start=1
        ):
            chunks.append(
                Chunk(
                    path=parsed.path,
                    start_line=begin,
                    end_line=stop,
                    content=_lines_of(source_lines, begin, stop),
                    kind="module",
                    part_index=index if stop - begin + 1 >= max_chunk_lines else 0,
                )
            )

    return chunks


def chunk_repo(
    repo_root: Path,
    rel_paths: list[str],
    max_file_bytes: int,
    max_chunk_lines: int = DEFAULT_MAX_CHUNK_LINES,
    overlap_lines: int = DEFAULT_OVERLAP_LINES,
) -> list[Chunk]:
    """切整个仓库。不可解析与密钥形态的文件自动跳过。"""
    chunks: list[Chunk] = []
    for rel_path in rel_paths:
        chunks.extend(
            chunk_file(repo_root, rel_path, max_file_bytes, max_chunk_lines, overlap_lines)
        )
    return chunks
