"""工具层。内部管道与 MCP server 的共同实现（KTD7）。

注册表在这里而非各调用方，是为了让「工具集有哪些、参数长什么样」只有一处定义。
MCP server（U14）与 ReAct 循环（U7）都从这里取 schema 与分发函数——两处各写一份会
漂移，而漂移的表现是「MCP 客户端调得通的工具在内部管道里参数名不对」。

schema 用 OpenAI 的 function calling 格式。MCP 的 inputSchema 也是 JSON Schema，
U14 转换时取 `parameters` 字段即可，不需要另写一套。
"""

from __future__ import annotations

from typing import Any, Callable

from backend.tools.context import ToolContext
from backend.tools.find_definition import find_definition
from backend.tools.find_references import find_references
from backend.tools.list_structure import list_structure
from backend.tools.read_file import read_file
from backend.tools.results import (
    DefinitionResult,
    FileSlice,
    ReferenceResult,
    StructureResult,
    ToolError,
)

ToolResult = FileSlice | DefinitionResult | ReferenceResult | StructureResult | ToolError

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "读取仓库内文件的指定行范围。行号 1-based。"
                "越界范围会收敛到有效区间，不报错。单次最多返回 200 行。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对仓库根的文件路径"},
                    "start_line": {"type": "integer", "description": "起始行，默认 1"},
                    "end_line": {"type": "integer", "description": "结束行，省略则读到上限"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_definition",
            "description": (
                "按名字查符号定义，返回文件路径与行号。"
                "支持裸名（helper）、限定名（Service.method）、带路径（src/a.py:helper）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "符号名"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_references",
            "description": (
                "查符号的引用点。检索范围由依赖图定界（导入了定义文件的那些文件）。"
                "注意这是文本层匹配，同名局部变量与注释也可能命中。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "符号名"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_structure",
            "description": "列出目录的直接子项（只一层）。默认仓库根。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对仓库根的目录路径，默认 ."},
                },
                "required": [],
            },
        },
    },
]

# 分发表。键与 schema 里的 name 一一对应——不一致会让模型调用一个不存在的工具，
# 而那种错误在运行时表现为「工具没反应」，离根因很远。
_DISPATCH: dict[str, Callable[..., ToolResult]] = {
    "read_file": read_file,
    "find_definition": find_definition,
    "find_references": find_references,
    "list_structure": list_structure,
}

TOOL_NAMES = frozenset(_DISPATCH)


def call_tool(ctx: ToolContext, name: str, arguments: dict[str, Any]) -> ToolResult:
    """按名字分发工具调用。

    未知工具名与参数错误都返回 ToolError 而非抛异常：这两种情况都由模型的输出引起，
    而模型出错不应中断整个 ReAct 循环——把错误作为工具结果返回，模型能据此纠正。
    """
    handler = _DISPATCH.get(name)
    if handler is None:
        return ToolError(
            message=f"未知工具：{name}",
            hint=f"可用工具：{', '.join(sorted(TOOL_NAMES))}",
        )

    try:
        return handler(ctx, **arguments)
    except TypeError as exc:
        # 参数名或个数不对。模型偶尔会编造参数名，返回错误让它重试。
        return ToolError(
            message=f"调用 {name} 的参数不正确：{exc}",
            hint=_parameter_hint(name),
        )
    except Exception as exc:  # noqa: BLE001 — 工具内部异常同样转数据，不中断循环
        return ToolError(message=f"工具 {name} 执行失败：{type(exc).__name__}: {exc}")


def _parameter_hint(name: str) -> str:
    for schema in TOOL_SCHEMAS:
        function = schema["function"]
        if function["name"] == name:
            properties = function["parameters"]["properties"]
            required = set(function["parameters"].get("required", []))
            parts = [
                f"{key}{'（必填）' if key in required else ''}"
                for key in properties
            ]
            return f"{name} 接受的参数：{', '.join(parts)}"
    return ""


__all__ = [
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "ToolContext",
    "ToolResult",
    "call_tool",
    "find_definition",
    "find_references",
    "list_structure",
    "read_file",
]
