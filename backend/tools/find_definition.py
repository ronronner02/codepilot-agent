"""查符号定义工具。

数据源是 U3 的符号表，不重新解析——工具看到的符号表必须与报告依据的一致，否则报告里
的引用无法核验（R7）。

支持三种查询形态，因为模型不总知道符号的完整归属：
  `helper`          裸名，返回所有同名符号
  `Service.method`  限定名，精确到类内方法
  `path:name`       带文件路径，用于同名符号很多时收窄
"""

from __future__ import annotations

from backend.tools.context import ToolContext
from backend.tools.results import DefinitionResult, SymbolLocation

# 返回的定义数上限。同名符号在大仓库里可能有几十个（`__init__`、`main`、`get`），
# 全部返回没有信息量，反而挤掉后续工具调用的空间。
DEFAULT_MAX_LOCATIONS = 12


def find_definition(
    ctx: ToolContext, name: str, max_locations: int = DEFAULT_MAX_LOCATIONS
) -> DefinitionResult:
    """按名字查符号定义。找不到时返回空结果，由 render 明确说明未找到。

    不返回 ToolError：「没找到这个符号」是正常查询结果而非工具故障，两者混同会让
    Agent 无法区分「符号不存在」和「工具坏了」。
    """
    query = name.strip()
    if not query:
        return DefinitionResult(query=name)

    path_filter = ""
    if ":" in query:
        path_filter, _, query = query.rpartition(":")
        path_filter = path_filter.strip()
        query = query.strip()

    matches: list[SymbolLocation] = []
    for symbol in ctx.symbols():
        if path_filter and path_filter not in symbol.path:
            continue
        # 裸名与限定名都接受：模型不总知道方法属于哪个类。
        if symbol.name != query and symbol.qualified_name != query:
            continue
        matches.append(
            SymbolLocation(
                name=symbol.name,
                qualified_name=symbol.qualified_name,
                kind=symbol.kind.value,
                path=symbol.path,
                start_line=symbol.start_line,
                end_line=symbol.end_line,
            )
        )

    # 排序保证同输入同输出，也让 Agent 第二次调用看到相同顺序。
    matches.sort(key=lambda loc: (loc.path, loc.start_line))

    note = ""
    if len(matches) > max_locations:
        note = (
            f"共 {len(matches)} 处同名定义，仅返回前 {max_locations} 处；"
            f"用 `文件路径:{query}` 的形式可收窄范围"
        )
        matches = matches[:max_locations]

    return DefinitionResult(query=name, locations=tuple(matches), truncated_note=note)
