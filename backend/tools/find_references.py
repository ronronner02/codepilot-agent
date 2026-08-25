"""查引用工具。

**这里有一个必须说清的能力边界。** 解析层只提取定义与 import 声明，不提取调用点——
要判断「这个函数在哪些地方被调用」需要遍历所有表达式里的标识符引用，还要处理动态调用、
`getattr`、装饰器注册、框架的约定式发现。那是另一个量级的工作。

所以这个工具用的是**依赖图定界的文本检索**，不是真正的引用解析：

  1. 先用符号表找到该符号的定义文件。
  2. 从依赖图反向边取出「导入了该文件的文件」，这些是可能引用它的地方。
  3. 在这些文件加定义文件自身里做文本检索。

比全仓库 grep 精确得多（依赖图挡掉了绝大多数无关文件），但仍是文本层：同名的局部变量、
字符串里的同名文本、注释里的提及都会命中。返回里明说这一点——Agent 据此判断结果的性质，
而报告结论若基于此，读者也能知道精度边界。

不做全仓库 grep 兜底：找不到定义时若退化为全仓库检索，`get`、`run` 这类名字会返回上千
命中，既无信息量又挤掉上下文。
"""

from __future__ import annotations

import re

from backend.paths import PathEscapeError, resolve_within
from backend.tools.context import ToolContext
from backend.tools.results import ReferenceHit, ReferenceResult

DEFAULT_MAX_HITS = 30

# 检索文件数上限。反向边多的核心文件（被上百个文件导入）逐个读会很慢，
# 而前若干个已足够让 Agent 判断使用模式。
DEFAULT_MAX_FILES = 40

_METHOD_NOTE = (
    "检索方式：依赖图定界的文本匹配（先由符号表定位定义文件，再在导入它的文件中检索）。"
    "解析层不提取调用点，故同名局部变量、字符串与注释中的同名文本也可能命中。"
)


def find_references(
    ctx: ToolContext,
    name: str,
    max_hits: int = DEFAULT_MAX_HITS,
    max_files: int = DEFAULT_MAX_FILES,
) -> ReferenceResult:
    """查符号的引用点。返回里附检索方式说明，让调用方知道结果的精度性质。"""
    query = name.strip()
    if not query:
        return ReferenceResult(query=name, method_note=_METHOD_NOTE)

    # 裸名用于文本匹配——限定名（`Service.method`）在调用点通常写成 `obj.method`，
    # 拿限定名去匹配会一个都找不到。
    bare_name = query.rsplit(".", 1)[-1]

    defining_paths = {
        symbol.path
        for symbol in ctx.symbols()
        if symbol.name == bare_name or symbol.qualified_name == query
    }

    if not defining_paths:
        # 不退化为全仓库检索：`get`、`run` 这类名字会返回上千命中，无信息量。
        return ReferenceResult(
            query=name,
            searched_files=0,
            method_note=(
                f"符号表中没有名为 {bare_name} 的定义，未做检索。"
                f"{_METHOD_NOTE}"
            ),
        )

    reverse = ctx.graph.reverse_edges()
    candidates: list[str] = []
    for defining_path in sorted(defining_paths):
        # 定义文件自身也要搜：同文件内的调用同样是引用。
        candidates.append(defining_path)
        candidates.extend(sorted(reverse.get(defining_path, frozenset())))

    # 去重且保持顺序，让同输入同输出。
    ordered = list(dict.fromkeys(candidates))
    truncated_files = max(0, len(ordered) - max_files)
    ordered = ordered[:max_files]

    pattern = re.compile(rf"\b{re.escape(bare_name)}\b")
    hits: list[ReferenceHit] = []
    searched = 0

    for rel_path in ordered:
        try:
            absolute = resolve_within(ctx.workdir, rel_path)
        except PathEscapeError:
            continue
        try:
            text = absolute.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        searched += 1
        for line_number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                hits.append(ReferenceHit(path=rel_path, line=line_number, text=line))
                if len(hits) >= max_hits:
                    break
        if len(hits) >= max_hits:
            break

    notes: list[str] = []
    if len(hits) >= max_hits:
        notes.append(f"命中数达上限 {max_hits}，可能还有更多引用")
    if truncated_files:
        notes.append(f"另有 {truncated_files} 个导入该文件的文件未检索")

    return ReferenceResult(
        query=name,
        hits=tuple(hits),
        searched_files=searched,
        truncated_note="；".join(notes),
        method_note=_METHOD_NOTE,
    )
