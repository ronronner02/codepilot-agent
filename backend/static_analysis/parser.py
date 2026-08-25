"""tree-sitter 驱动层。加载 grammar、跑 query、把 capture 转成 Symbol / ImportRef。

驱动是机械的；query 与 capture 到数据类型的映射是核心逻辑（见 queries/ 下的说明）。

tree-sitter 0.26 的 API：`ts.Query(language, source)` 构造，
`ts.QueryCursor(query).captures(node)` 执行，返回 `dict[capture_name, list[Node]]`。
`Language.query()` 在这个版本已移除。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tree_sitter as ts
import tree_sitter_language_pack as pack

from backend.paths import PathEscapeError, resolve_within
from backend.static_analysis.models import (
    ImportRef,
    ParsedFile,
    ParseOutcome,
    Symbol,
    UnparsedFile,
)

# 扩展名到 grammar 名。`.d.ts` 的 suffix 是 `.ts`，走同一个 grammar——声明文件的
# 语法是 TS 的子集，能正常解析。
#
# JavaScript（.js/.mjs/.cjs/.jsx）不在此表内，因为符号级解析的语言范围限定为 Python
# 与 TypeScript。它们仍计入文件树与规模统计，符号级则走「未支持的扩展名」路径显式
# 标注原因（R11 要求未解析范围可落到报告里）。
#
# 这不只是范围声明，也避开一个会静默吞文件的陷阱：javascript grammar 里不存在
# interface_declaration / type_alias_declaration / type_identifier，用 typescript.scm
# 去构造 Query 会抛 QueryError（实测确认，不是静默返回空结果）。若把 .js 映射到
# javascript grammar，异常会落进 parse_file 的兜底分支，未解析原因被写成「解析失败」
# ——把「不在语言范围内」误报成「解析出错」。
SUFFIX_TO_GRAMMAR = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
}

_QUERY_DIR = Path(__file__).parent / "queries"

# grammar 名到 query 文件。tsx 与 typescript 共用一份——TSX 的语法是 TS 的超集，
# 实测两者对 TS 专有节点均编译通过。javascript 不在此表内，理由见 SUFFIX_TO_GRAMMAR。
_GRAMMAR_TO_QUERY_FILE = {
    "python": "python.scm",
    "typescript": "typescript.scm",
    "tsx": "typescript.scm",
}

_query_cache: dict[str, ts.Query] = {}


def load_query(grammar: str) -> ts.Query:
    """加载并缓存 query。Query 构造有开销，每个 grammar 只做一次。"""
    if grammar not in _query_cache:
        source = (_QUERY_DIR / _GRAMMAR_TO_QUERY_FILE[grammar]).read_text(
            encoding="utf-8"
        )
        _query_cache[grammar] = ts.Query(pack.get_language(grammar), source)
    return _query_cache[grammar]


def node_text(source: bytes, node: ts.Node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


@dataclass(frozen=True)
class ParsedTree:
    """已建好的语法树及其来源。

    评审层（U11）需要在树上做结构性判断——查字段是否缺失、查祖先链上有无 try——
    那些判据 tree-sitter query 表达不了，得直接走树。把「校验路径、限体积、读取、
    建树」这一段抽出来共用，是为了让路径校验（KTD14）仍然只有一处实现：评审层若自己
    读文件，就多了一条绕过校验的路径。
    """

    tree: ts.Tree
    source: bytes
    grammar: str
    path: str


def parse_tree(
    repo_root: Path, rel_path: str, max_bytes: int
) -> ParsedTree | UnparsedFile:
    """校验路径、限体积、读取并建树。失败一律返回 UnparsedFile 而非抛异常。"""
    suffix = Path(rel_path).suffix.lower()
    grammar = SUFFIX_TO_GRAMMAR.get(suffix)
    if grammar is None:
        return UnparsedFile(path=rel_path, reason=f"未支持符号级解析的扩展名：{suffix}")

    try:
        absolute = resolve_within(repo_root, rel_path)
    except PathEscapeError:
        return UnparsedFile(path=rel_path, reason="路径校验未通过（符号链接或逃逸）")

    try:
        size = absolute.stat().st_size
    except OSError as exc:
        return UnparsedFile(path=rel_path, reason=f"无法读取文件信息：{exc}")
    if size > max_bytes:
        return UnparsedFile(
            path=rel_path, reason=f"文件体积 {size} 字节超出上限 {max_bytes}"
        )

    try:
        source = absolute.read_bytes()
    except OSError as exc:
        return UnparsedFile(path=rel_path, reason=f"读取失败：{exc}")

    try:
        tree = pack.get_parser(grammar).parse(source)
    except Exception as exc:  # noqa: BLE001 — grammar 层异常类型不稳定，全部降级
        return UnparsedFile(path=rel_path, reason=f"解析失败：{type(exc).__name__}: {exc}")

    return ParsedTree(tree=tree, source=source, grammar=grammar, path=rel_path)


def parse_file(
    repo_root: Path, rel_path: str, max_bytes: int
) -> ParsedFile | UnparsedFile:
    """解析单个文件。任何失败都返回 UnparsedFile 而非抛异常——单文件失败不应
    中断整次解析（R10 的机制基础）。
    """
    built = parse_tree(repo_root, rel_path, max_bytes)
    if isinstance(built, UnparsedFile):
        return built

    try:
        captures = ts.QueryCursor(load_query(built.grammar)).captures(built.tree.root_node)
    except Exception as exc:  # noqa: BLE001 — grammar 层异常类型不稳定，全部降级
        return UnparsedFile(path=rel_path, reason=f"解析失败：{type(exc).__name__}: {exc}")

    from backend.static_analysis.extract import extract_imports, extract_symbols

    return ParsedFile(
        path=rel_path,
        language=built.grammar,
        symbols=extract_symbols(captures, built.source, rel_path),
        imports=extract_imports(captures, built.source, rel_path),
    )


def parse_repo(
    repo_root: Path, rel_paths: list[str], max_bytes: int
) -> ParseOutcome:
    outcome = ParseOutcome()
    for rel_path in rel_paths:
        result = parse_file(repo_root, rel_path, max_bytes)
        if isinstance(result, ParsedFile):
            outcome.parsed.append(result)
        else:
            outcome.unparsed.append(result)
    return outcome
