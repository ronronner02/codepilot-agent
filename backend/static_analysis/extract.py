"""capture -> Symbol / ImportRef 的映射。U3 的核心逻辑。

这一层决定符号表和依赖图的输入质量。query 决定「能捕获什么」，这里决定「捕获到的
东西怎么解释」。两者都错一处，下游的聚类和报告就整体偏移。

分工边界：query 只标出名字节点与语句边界（见 queries/*.scm 的说明），本模块承担
三件 query 表达不了的事——父节点链上的方法判定、定义的行范围取法、以及一条 import
语句内部的字段解读。
"""

from __future__ import annotations

import tree_sitter as ts

from backend.static_analysis.models import ImportKind, ImportRef, Symbol, SymbolKind
from backend.static_analysis.parser import node_text

# capture 名到 SymbolKind。两种语言共用一套 capture 命名，故本表也共用。
# def_function 可能被升级为 METHOD——Python 的类内函数与 function_definition 同型，
# 只有父节点链能区分（见 _enclosing_class）。
_CAPTURE_KIND = {
    "def_function": SymbolKind.FUNCTION,
    "def_method": SymbolKind.METHOD,
    "def_class": SymbolKind.CLASS,
    "def_interface": SymbolKind.INTERFACE,
    "def_type_alias": SymbolKind.TYPE_ALIAS,
}

# 包裹在真实定义外层、但属于「定义的一部分」的节点类型。
#
# 行范围为什么要包含这些包裹层：Symbol 的行范围是 U9 切块与 U7 读取符号定义的取值
# 依据。`@property` 和 `export` 都携带语义——前者决定这个函数是属性还是方法，后者
# 决定它是不是模块对外接口。范围切掉它们，下游拿到的代码片段就丢了这层信息，报告
# 里「这个函数是导出的」这类结论也就无从核验。代价是行范围比 `def` 行更早开始，
# 这在编辑器跳转场景略显宽，但可追溯性优先于跳转精度。
_DEFINITION_WRAPPERS = {
    "decorated_definition",  # Python: @decorator 包裹
    "export_statement",  # TS: export 包裹
    "lexical_declaration",  # TS: const/let（箭头函数场景）
    "variable_declaration",  # TS: var
}

_CLASS_NODES = {"class_definition", "class_declaration", "abstract_class_declaration"}

# 往上找类时的边界：遇到函数体就停。嵌套函数内定义的类，其方法不属于外层类。
_FUNCTION_NODES = {
    "function_definition",
    "function_declaration",
    "generator_function_declaration",
    "method_definition",
    "arrow_function",
    "function_expression",
}


def _definition_node(name_node: ts.Node) -> ts.Node:
    """从名字节点回溯到「定义的起始」节点，用于取行范围。

    名字节点的 start_point 只是名字所在行，Symbol 需要整个定义的范围。名字节点的
    父节点就是定义节点（function_definition / class_declaration / ...），再往上若是
    _DEFINITION_WRAPPERS 里的包裹层则继续上溯。
    """
    node = name_node.parent
    if node is None:  # 名字节点总有父节点，防御性兜底
        return name_node

    while node.parent is not None and node.parent.type in _DEFINITION_WRAPPERS:
        parent = node.parent
        # 一条语句声明多个变量时（`const a = () => 1, b = () => 2`）不能上溯到
        # lexical_declaration——那会让两个符号拿到同一个行范围，U9 切块时产出重复
        # 片段。只有独生子才安全上溯。
        if parent.type in {"lexical_declaration", "variable_declaration"}:
            declarators = [c for c in parent.named_children if c.type == "variable_declarator"]
            if len(declarators) != 1:
                break
        node = parent
    return node


def _enclosing_class(name_node: ts.Node, source: bytes) -> str | None:
    """往上找所属类名。方法返回类名，顶层定义返回 None。

    这个判定影响 Symbol.parent 与 qualified_name，而 qualified_name 是 U7 查符号
    定义工具的查找键——同名方法分属不同类时，不区分会让工具返回错误位置。

    起点是 `name_node.parent.parent`，跳过符号自身的定义节点：名字节点的父节点
    总是该符号自己的定义（类名的父是 class_definition，方法名的父是
    method_definition，箭头函数名的父是 variable_declarator），从它起走会让顶层类
    把自己当成 parent，产出 `Settings.Settings` 这种自指的 qualified_name。
    """
    node = name_node.parent.parent if name_node.parent is not None else None
    while node is not None:
        if node.type in _CLASS_NODES:
            class_name = node.child_by_field_name("name")
            return node_text(source, class_name) if class_name is not None else None
        if node.type in _FUNCTION_NODES:
            # 走到函数体说明这个定义嵌在别的函数里（闭包内的局部函数或局部类）。
            # 此时即使更外层还有类，它也不是那个类的方法。
            return None
        node = node.parent
    return None


def extract_symbols(
    captures: dict[str, list[ts.Node]], source: bytes, rel_path: str
) -> list[Symbol]:
    """把 query 的 capture 转成 Symbol 列表。"""
    symbols: list[Symbol] = []
    seen: set[tuple[str, str, int]] = set()

    for capture_name, kind in _CAPTURE_KIND.items():
        for name_node in captures.get(capture_name, []):
            name = node_text(source, name_node)
            definition = _definition_node(name_node)
            parent = _enclosing_class(name_node, source)

            # Python 的类内 function_definition 在 query 层与顶层函数同型，
            # 到这里才能升级为 METHOD。TS 的 method_definition 已是 METHOD，
            # 重复赋值无害。
            actual_kind = SymbolKind.METHOD if parent and kind is SymbolKind.FUNCTION else kind

            # tree-sitter 的 point 是 0-based，Symbol 约定 1-based（与编辑器一致）。
            start_line = definition.start_point[0] + 1
            end_line = definition.end_point[0] + 1

            # 去重键取 (path, name, start_line)：同一位置的同名符号只留一个。
            # 正常情况下 query 的各 capture 互不重叠，这里是防御——query 迭代时
            # 新增一条规则很容易与既有规则在某个语法形态上重合。
            key = (rel_path, name, start_line)
            if key in seen:
                continue
            seen.add(key)

            symbols.append(
                Symbol(
                    name=name,
                    kind=actual_kind,
                    path=rel_path,
                    start_line=start_line,
                    end_line=end_line,
                    parent=parent,
                )
            )

    symbols.sort(key=lambda s: (s.start_line, s.name))
    return symbols


def _string_value(node: ts.Node, source: bytes) -> str:
    """取字符串字面量的内容，剥掉引号。

    走 string_fragment 子节点而非对文本做 strip：单引号、双引号、反引号三种形态
    在语法树里都归一为 string > string_fragment，由 grammar 负责识别边界。用
    strip("\\"'`") 则会误伤内容本身以引号字符开头或结尾的路径。
    """
    for child in node.named_children:
        if child.type == "string_fragment":
            return node_text(source, child)
    return node_text(source, node).strip("\"'`")


def _dotted_target(node: ts.Node, source: bytes) -> str:
    """取 import 目标的文本。aliased_import 要取 name 字段，丢掉 `as` 别名。

    `import os.path as p` 的目标是 os.path，不是 p——别名只在本文件作用域有意义，
    依赖图关心的是被导入的模块。
    """
    if node.type == "aliased_import":
        name = node.child_by_field_name("name")
        return node_text(source, name) if name is not None else node_text(source, node)
    return node_text(source, node)


def _python_module_import(statement: ts.Node, source: bytes, rel_path: str) -> list[ImportRef]:
    """`import os` / `import os.path as p` / `import os, sys`。

    一条语句可含多个目标，故 children_by_field_name（复数）而非
    child_by_field_name——后者只返回第一个，`import os, sys` 会丢掉 sys。
    """
    line = statement.start_point[0] + 1
    return [
        ImportRef(
            target=_dotted_target(target, source),
            kind=ImportKind.MODULE,
            path=rel_path,
            line=line,
        )
        for target in statement.children_by_field_name("name")
    ]


def _python_from_import(statement: ts.Node, source: bytes, rel_path: str) -> list[ImportRef]:
    """`from x import y` / `from . import y` / `from ..pkg.deep import y`。

    module_name 的文本直接含相对导入的点（relative_import 节点的文本就是 `..pkg.deep`），
    不做处理即保留层级——点数决定 U4 归一化到哪个目录，丢了就无法解析。
    """
    module = statement.child_by_field_name("module_name")
    if module is None:
        return []

    names = [_dotted_target(n, source) for n in statement.children_by_field_name("name")]

    # `from x import *` 的 wildcard_import 不在 name 字段下（实测确认），需单独查。
    # 记为 `*` 而不是丢弃：U4 判断 barrel 传递与符号归属时，「导入了全部」与
    # 「未记录任何名字」是两种不同的输入。
    if any(child.type == "wildcard_import" for child in statement.named_children):
        names.append("*")

    return [
        ImportRef(
            target=node_text(source, module),
            kind=ImportKind.FROM,
            path=rel_path,
            line=statement.start_point[0] + 1,
            names=tuple(names),
        )
    ]


def _ts_clause_names(clause: ts.Node, source: bytes) -> tuple[str, ...]:
    """取 import 子句里的具名导入。默认导入与命名空间导入不产生具名条目。"""
    names: list[str] = []
    for child in clause.named_children:
        if child.type != "named_imports":
            continue
        for specifier in child.named_children:
            if specifier.type != "import_specifier":
                continue
            name = specifier.child_by_field_name("name")
            if name is not None:
                names.append(node_text(source, name))
    return tuple(names)


def _ts_import(statement: ts.Node, source: bytes, rel_path: str) -> list[ImportRef]:
    """TS 的 import 语句。分类依据是子句形态与 type 关键字。"""
    module = statement.child_by_field_name("source")
    if module is None:
        return []

    # `import type { T } from 'x'` 的 type 关键字是 import_statement 的直接子节点。
    # inline 形态（`import { type T } from 'x'`）的 type 嵌在 import_specifier 内部，
    # 不会命中这里——它是逐名字的 type-only，整条语句仍产生运行时依赖，分类为 FROM
    # 是正确的。
    type_only = any(child.type == "type" for child in statement.children)

    clause = next((c for c in statement.named_children if c.type == "import_clause"), None)
    names = _ts_clause_names(clause, source) if clause is not None else ()

    if type_only:
        kind = ImportKind.TYPE_ONLY
    elif names:
        # 具名导入存在即为 FROM，含 `import def, { a } from 'x'` 的混合形态。
        kind = ImportKind.FROM
    else:
        # 默认导入、命名空间导入、以及无子句的副作用导入（`import './polyfill'`）。
        kind = ImportKind.MODULE

    return [
        ImportRef(
            target=_string_value(module, source),
            kind=kind,
            path=rel_path,
            line=statement.start_point[0] + 1,
            names=names,
        )
    ]


def _ts_reexport(statement: ts.Node, source: bytes, rel_path: str) -> list[ImportRef]:
    """`export * from './b'` / `export { a } from './b'`。

    无 source 字段的 export（`export const x = 1`）不是依赖边，返回空。
    """
    module = statement.child_by_field_name("source")
    if module is None:
        return []

    clause = next((c for c in statement.named_children if c.type == "export_clause"), None)
    if clause is None:
        # 星号再导出。记为 `*`，与具名再导出区分——U4 传递 barrel 依赖时，
        # `export *` 会把目标的全部符号带进来，具名再导出只带列出的那几个。
        names: tuple[str, ...] = ("*",)
    else:
        collected = []
        for specifier in clause.named_children:
            if specifier.type != "export_specifier":
                continue
            name = specifier.child_by_field_name("name")
            if name is not None:
                collected.append(node_text(source, name))
        names = tuple(collected)

    return [
        ImportRef(
            target=_string_value(module, source),
            kind=ImportKind.REEXPORT,
            path=rel_path,
            line=statement.start_point[0] + 1,
            names=names,
        )
    ]


# capture 名到处理函数。Python 与 TS 的 capture 名不重叠，故一张表覆盖两种语言，
# 调用方无需知道当前是哪种语言。
_IMPORT_HANDLERS = {
    "import_module": _python_module_import,
    "import_from": _python_from_import,
    "import_statement": _ts_import,
    "reexport_statement": _ts_reexport,
}


def extract_imports(
    captures: dict[str, list[ts.Node]], source: bytes, rel_path: str
) -> list[ImportRef]:
    """把 query 的 capture 转成 ImportRef 列表。

    `ImportRef.target` 存原始文本，不做路径解析——解析成仓库内文件是 U4 的事
    （需要 tsconfig 别名、Python 包结构等上下文）。这里只负责把语法结构读对。
    """
    imports: list[ImportRef] = []
    for capture_name, handler in _IMPORT_HANDLERS.items():
        for statement in captures.get(capture_name, []):
            imports.extend(handler(statement, source, rel_path))

    imports.sort(key=lambda i: (i.line, i.target))
    return imports
