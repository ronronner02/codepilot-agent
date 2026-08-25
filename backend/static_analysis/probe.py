"""语法树查看工具。写 query 时用它确认节点类型与字段名。

    python -m backend.static_analysis.probe <文件路径> [最大深度]
    python -m backend.static_analysis.probe --code "def f(): pass" python

字段名（`name:`、`source:` 之类）在 query 里是精确匹配的关键——凭猜写 query 通常
匹配不到任何东西，而且不报错，只是静默返回空结果。

**本模块有意不过 KTD14 的路径校验。** 它是开发者手动调用的查看工具，路径由操作者
从自己的 shell 传入——不存在不可信输入边界，操作者本身就是信任边界。加校验会妨碍
它的用途（写 query 时需要能查看任意位置的文件）。KTD14 约束的是解析层、工具层与
MCP 工具三处，probe 不在管道内，也不被它们调用。
"""

from __future__ import annotations

import sys
from pathlib import Path

import tree_sitter as ts
import tree_sitter_language_pack as pack

from backend.static_analysis.parser import SUFFIX_TO_GRAMMAR


def dump(node: ts.Node, source: bytes, depth: int = 0, max_depth: int = 4) -> None:
    if depth > max_depth:
        return
    text = source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
    snippet = text.split("\n")[0][:44]
    # 字段名要从父节点问，节点自己不知道自己叫什么字段。
    field = ""
    if node.parent is not None:
        for i in range(node.parent.child_count):
            if node.parent.child(i) == node:
                fname = node.parent.field_name_for_child(i)
                if fname:
                    field = f"{fname}: "
                break
    marker = "" if node.is_named else "  (anonymous)"
    print(f"{'  ' * depth}{field}({node.type}){marker}  L{node.start_point[0] + 1}  {snippet!r}")
    for child in node.children:
        dump(child, source, depth + 1, max_depth)


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1

    if args[0] == "--code":
        if len(args) < 3:
            print("用法：--code \"<代码>\" <grammar>")
            return 1
        source = args[1].encode("utf-8")
        grammar = args[2]
        max_depth = int(args[3]) if len(args) > 3 else 4
    else:
        path = Path(args[0])
        if not path.is_file():
            print(f"文件不存在：{path}")
            return 1
        grammar = SUFFIX_TO_GRAMMAR.get(path.suffix.lower(), "")
        if not grammar:
            print(f"未支持的扩展名：{path.suffix}")
            return 1
        source = path.read_bytes()
        max_depth = int(args[1]) if len(args) > 1 else 4

    tree = pack.get_parser(grammar).parse(source)
    print(f"grammar={grammar}  max_depth={max_depth}\n")
    dump(tree.root_node, source, max_depth=max_depth)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
