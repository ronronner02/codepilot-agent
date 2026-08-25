"""错误处理缺陷的候选点定位。确定性部分，不调 LLM。

分工：本模块只回答「哪些位置值得看」，不回答「这是不是问题」。后者交给判断层——
按计划的设计，LLM 只做取舍不做发现，这样它无法凭空造出一个不存在的缺陷位置，
幻觉空间被压缩到「该不该报」这一个维度。

四类候选（R15）与各自的确定判据：

  裸 except        except_clause 缺 value 字段。实测确认：`except:` 无 value，
                   `except Exception:` 有 `value: (identifier)`。
  异常被吞         handler 块内只有 pass / ellipsis / 注释，且无 raise。
  IO 缺错误处理    IO 与网络调用不在任何 try 块的 body 内。
  资源未关闭       open() 的返回值被赋给变量，且不在 with 语句中。

前两类判据本身足够定性（CERTAIN），后两类必须看上下文（CONTEXTUAL）——一个函数让
异常向上传给调用方处理是完全正常的设计，把它一律报出来就是 Reviewer 最该避免的
「50 条泛泛之谈」。

为什么走树而不写 tree-sitter query：这些判据要查「字段是否缺失」与「祖先链上有无
try」，query 语法表达不了否定与祖先关系。query 适合「找出符合这个形状的节点」，
这里需要的是「找出缺少某部分的节点」。
"""

from __future__ import annotations

import tree_sitter as ts

from backend.review.models import Candidate, Confidence, FindingCategory
from backend.static_analysis.parser import ParsedTree, node_text

# 可能抛异常的 IO 与网络调用。
#
# **必须要求模块限定可见，不能只匹配方法名。** 实测教训：只按方法名匹配
# （收了 get/post/read/write/run/load 这类）在 fastapi 上产出 3593 个候选，每文件
# 3.2 个——`client.get(...)`、`dict.get(...)`、`list.append` 之类全被命中。那个量级
# 既是计划警告的「泛泛之谈」，也让判断层的 LLM 成本失控。
#
# 方法名本身说明不了它做不做 IO：`d.get(k)` 与 `requests.get(url)` 同名而语义无关。
# 所以只认「模块名可见」的调用。
#
# 已知局限：通过变量持有的 IO 对象（`session = requests.Session(); session.get(...)`）
# 匹配不到。这是有意的取舍——Python 的动态性让「这个变量是什么」静态不可解，宁可漏
# 也不要 3593 个候选（Reviewer 的标准是求准不求全）。
_PY_IO_QUALIFIED: dict[str, frozenset[str]] = {
    "os": frozenset({"remove", "unlink", "rmdir", "mkdir", "makedirs", "rename", "listdir"}),
    "shutil": frozenset({"copy", "copytree", "move", "rmtree"}),
    "requests": frozenset({"get", "post", "put", "delete", "patch", "head", "request"}),
    "httpx": frozenset({"get", "post", "put", "delete", "patch", "head", "request"}),
    "urllib": frozenset({"urlopen"}),
    "socket": frozenset({"connect", "bind", "send", "recv", "sendall"}),
    "subprocess": frozenset({"run", "call", "check_call", "check_output", "Popen"}),
    "json": frozenset({"load", "loads"}),
    "pickle": frozenset({"load", "loads"}),
    "yaml": frozenset({"load", "safe_load"}),
    "sqlite3": frozenset({"connect"}),
}

# 无需限定的内建 IO 调用——这些名字本身就唯一指向 IO。
_PY_IO_BUILTINS = frozenset({"open", "urlopen"})

_TS_IO_QUALIFIED: dict[str, frozenset[str]] = {
    "fs": frozenset({"readFile", "writeFile", "readFileSync", "writeFileSync", "unlink"}),
    "JSON": frozenset({"parse"}),
}

_TS_IO_BUILTINS = frozenset({"fetch"})

# 上下文片段的行数。取 3 行：足够看清这一句在做什么，又不至于让 prompt 膨胀。
_SNIPPET_RADIUS = 2

_PY_FUNCTION_NODES = frozenset({"function_definition"})
_TS_FUNCTION_NODES = frozenset(
    {"function_declaration", "method_definition", "arrow_function", "function_expression"}
)


def _snippet(source: bytes, node: ts.Node, radius: int = _SNIPPET_RADIUS) -> str:
    """取节点周围若干行，供判断层阅读。"""
    lines = source.decode("utf-8", errors="replace").splitlines()
    start = max(0, node.start_point[0] - radius)
    end = min(len(lines), node.end_point[0] + radius + 1)
    return "\n".join(lines[start:end])


def _enclosing_function(node: ts.Node, source: bytes, grammar: str) -> str:
    """往上找所在函数名。模块级返回空串。

    判断层需要它：异常在一个叫 `main` 的函数里被吞掉，与在 `cleanup` 里被吞掉，
    该不该管的结论可能相反。
    """
    function_nodes = _PY_FUNCTION_NODES if grammar == "python" else _TS_FUNCTION_NODES
    current = node.parent
    while current is not None:
        if current.type in function_nodes:
            name = current.child_by_field_name("name")
            return node_text(source, name) if name is not None else "<匿名函数>"
        current = current.parent
    return ""


def _walk(node: ts.Node) -> list[ts.Node]:
    """前序遍历全部节点。规模有上限保护——单文件体积已由 parse_tree 限过。"""
    collected = [node]
    index = 0
    while index < len(collected):
        collected.extend(collected[index].children)
        index += 1
    return collected


def _is_effectively_empty(block: ts.Node, source: bytes) -> bool:
    """块内是否只有占位语句。

    `pass`、`...`、注释都算占位。有 raise 则不算——`except X: raise CustomError` 是
    正常的异常转换，不是吞掉。
    """
    meaningful = [
        child
        for child in block.named_children
        if child.type not in ("comment", "pass_statement", "ellipsis")
    ]
    if not meaningful:
        return True
    # 只有 expression_statement 包着一个 ellipsis 时（TS 里 `catch {}` 已是空块，
    # Python 的 `...` 是 ellipsis 节点）也算空。
    if len(meaningful) == 1 and meaningful[0].type == "expression_statement":
        inner = meaningful[0].named_children
        if len(inner) == 1 and inner[0].type == "ellipsis":
            return True
    return False


def _inside_try_body(node: ts.Node) -> bool:
    """节点是否位于某个 try 块的 body 内（而非 handler 内）。

    要区分 body 与 handler：写在 except 块里的 IO 调用并没有被保护，它自己就可能
    抛异常。只看「祖先链上有 try_statement」会把这种情况误判为已处理。
    """
    current = node
    while current.parent is not None:
        parent = current.parent
        if parent.type == "try_statement":
            body = parent.child_by_field_name("body")
            if body is not None and body.id == current.id:
                return True
        current = parent
    return False


def _inside_with_statement(node: ts.Node, grammar: str) -> bool:
    """节点是否在 with（Python）或 using（TS 显式资源管理）语句内。"""
    wrapper = "with_statement" if grammar == "python" else "variable_declaration"
    current = node.parent
    while current is not None:
        if current.type == wrapper:
            return True
        if current.type in ("function_definition", "function_declaration"):
            break
        current = current.parent
    return False


def _call_parts(call: ts.Node, source: bytes) -> tuple[str, str]:
    """取调用的 (限定名, 方法名)。

    限定名是属性调用里点号左边的最后一段：`os.path.remove` -> `path`，
    `requests.get` -> `requests`。非属性调用时为空串。

    为什么要它：只有方法名无法判断这个调用做不做 IO（`d.get` 与 `requests.get` 同名
    而语义无关），必须看限定名。
    """
    function = call.child_by_field_name("function")
    if function is None:
        return "", ""
    if function.type in ("attribute", "member_expression"):
        obj = function.child_by_field_name("object")
        attribute = function.child_by_field_name("attribute") or function.child_by_field_name(
            "property"
        )
        qualifier = ""
        if obj is not None:
            # `os.path.remove` 的 object 是 `os.path`，取其最后一段。
            qualifier = node_text(source, obj).rsplit(".", 1)[-1]
        return qualifier, node_text(source, attribute) if attribute is not None else ""
    return "", node_text(source, function)


def _call_name(call: ts.Node, source: bytes) -> str:
    """取调用的函数名（不含限定）。"""
    return _call_parts(call, source)[1]


def _candidate(
    kind: str,
    parsed: ParsedTree,
    node: ts.Node,
    evidence: str,
    confidence: Confidence,
) -> Candidate:
    return Candidate(
        category=FindingCategory.ERROR_HANDLING,
        kind=kind,
        path=parsed.path,
        line=node.start_point[0] + 1,
        snippet=_snippet(parsed.source, node),
        detector_evidence=evidence,
        confidence=confidence,
        enclosing=_enclosing_function(node, parsed.source, parsed.grammar),
    )


def find_bare_except(parsed: ParsedTree, nodes: list[ts.Node]) -> list[Candidate]:
    """裸 `except:`。判据是 except_clause 缺 value 字段。

    Python 独有——TS 的 `catch` 本就不指定异常类型，那是语言设计而非缺陷。
    """
    if parsed.grammar != "python":
        return []
    return [
        _candidate(
            "bare_except",
            parsed,
            node,
            "except_clause 无 value 字段，即 `except:` 未指定异常类型，"
            "会连 KeyboardInterrupt 与 SystemExit 一并捕获",
            Confidence.CERTAIN,
        )
        for node in nodes
        if node.type == "except_clause" and node.child_by_field_name("value") is None
    ]


def find_swallowed_exceptions(parsed: ParsedTree, nodes: list[ts.Node]) -> list[Candidate]:
    """异常被吞：handler 块内只有占位语句。

    Python 的 except_clause 与 TS 的 catch_clause 结构不同（前者块是无名子节点，
    后者是 body 字段），分别取。
    """
    candidates: list[Candidate] = []
    for node in nodes:
        if parsed.grammar == "python" and node.type == "except_clause":
            block = next((c for c in node.named_children if c.type == "block"), None)
        elif node.type == "catch_clause":
            block = node.child_by_field_name("body")
        else:
            continue

        if block is None or not _is_effectively_empty(block, parsed.source):
            continue

        candidates.append(
            _candidate(
                "swallowed_exception",
                parsed,
                node,
                "异常处理块内只有占位语句（pass / ... / 注释），既未记录也未重抛，"
                "异常信息在此处完全丢失",
                Confidence.CERTAIN,
            )
        )
    return candidates


def find_unprotected_io(parsed: ParsedTree, nodes: list[ts.Node]) -> list[Candidate]:
    """IO 与网络调用不在 try 块的 body 内。

    CONTEXTUAL：让异常向上传给调用方处理是正常设计。这一类的价值在于把「可能失败的
    调用」摊到判断层面前，而不是断言它们有问题。
    """
    if parsed.grammar == "python":
        qualified, builtins, call_type = _PY_IO_QUALIFIED, _PY_IO_BUILTINS, "call"
    else:
        qualified, builtins, call_type = _TS_IO_QUALIFIED, _TS_IO_BUILTINS, "call_expression"

    candidates: list[Candidate] = []
    for node in nodes:
        if node.type != call_type:
            continue
        qualifier, method = _call_parts(node, parsed.source)

        if qualifier:
            if method not in qualified.get(qualifier, frozenset()):
                continue
            display = f"{qualifier}.{method}"
        else:
            if method not in builtins:
                continue
            display = method

        if _inside_try_body(node):
            continue

        candidates.append(
            _candidate(
                "unprotected_io",
                parsed,
                node,
                f"调用 {display}() 可能因外部原因失败，且不在任何 try 块的 body 内。"
                "注意：异常有意交给调用方处理时这是正常设计",
                Confidence.CONTEXTUAL,
            )
        )
    return candidates


def find_unclosed_resources(parsed: ParsedTree, nodes: list[ts.Node]) -> list[Candidate]:
    """open() 的结果被赋值且不在 with 语句内。

    只查 Python：TS 的文件句柄由 GC 与 Promise 管理，没有等价的确定判据。
    """
    if parsed.grammar != "python":
        return []

    candidates: list[Candidate] = []
    for node in nodes:
        if node.type != "assignment":
            continue
        value = node.child_by_field_name("right")
        if value is None or value.type != "call":
            continue
        if _call_name(value, parsed.source) != "open":
            continue
        if _inside_with_statement(node, parsed.grammar):
            continue
        candidates.append(
            _candidate(
                "unclosed_resource",
                parsed,
                node,
                "open() 的返回值被赋给变量且不在 with 语句中，"
                "异常路径上文件句柄可能不被关闭。需确认是否有显式 close()",
                Confidence.CONTEXTUAL,
            )
        )
    return candidates


def find_error_handling_candidates(parsed: ParsedTree) -> list[Candidate]:
    """跑全部错误处理检测器。遍历一次树，各检测器复用节点列表。"""
    nodes = _walk(parsed.tree.root_node)
    candidates = [
        *find_bare_except(parsed, nodes),
        *find_swallowed_exceptions(parsed, nodes),
        *find_unprotected_io(parsed, nodes),
        *find_unclosed_resources(parsed, nodes),
    ]
    # 同一位置可能被多个检测器命中（裸 except 且吞异常）。都保留——它们是不同的
    # 问题，判断层需要分别取舍；但排序要稳定。
    candidates.sort(key=lambda c: (c.line, c.kind))
    return candidates
