"""工具返回的统一形态。

两类消费方对格式的要求不同：ReAct 循环要文本（塞进对话），MCP 客户端要结构化数据。
所以工具返回 dataclass，由 `render()` 转成给模型看的文本——同一份实现服务两边，避免
两套逻辑漂移（KTD7）。

**截断必须可见。** 静默截断会让 Agent 以为看到了全部内容，据此得出的结论无从判断
可靠性。每个可能截断的返回都带 `truncated_note`，非空即说明有内容未返回。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolError:
    """工具执行失败。

    与「查到了但结果为空」严格区分：前者是路径不存在、越界这类问题，后者是正常的
    「没找到」。测试场景明确要求不存在的路径返回明确错误而非空字符串——空结果会让
    Agent 以为文件是空的，继续往下推断。
    """

    message: str
    hint: str = ""

    def render(self) -> str:
        return f"错误：{self.message}" + (f"\n提示：{self.hint}" if self.hint else "")


@dataclass(frozen=True)
class FileSlice:
    path: str
    start_line: int
    end_line: int
    total_lines: int
    content: str
    truncated_note: str = ""

    def render(self) -> str:
        header = f"{self.path} 第 {self.start_line}-{self.end_line} 行（共 {self.total_lines} 行）"
        body = "\n".join(
            f"{self.start_line + i:>5}| {line}"
            for i, line in enumerate(self.content.splitlines())
        )
        parts = [header, body]
        if self.truncated_note:
            parts.append(f"（{self.truncated_note}）")
        return "\n".join(parts)


@dataclass(frozen=True)
class SymbolLocation:
    name: str
    qualified_name: str
    kind: str
    path: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class DefinitionResult:
    query: str
    locations: tuple[SymbolLocation, ...] = ()
    truncated_note: str = ""

    def render(self) -> str:
        if not self.locations:
            # 明确说明未找到，而不是返回空串——空串会被模型读成「工具没工作」。
            return f"未找到名为 {self.query} 的符号定义。符号表中不存在该名称。"
        lines = [f"符号 {self.query} 的定义（{len(self.locations)} 处）："]
        lines.extend(
            f"  {loc.kind:11} {loc.qualified_name:38} {loc.path}:{loc.start_line}-{loc.end_line}"
            for loc in self.locations
        )
        if self.truncated_note:
            lines.append(f"（{self.truncated_note}）")
        return "\n".join(lines)


@dataclass(frozen=True)
class ReferenceHit:
    path: str
    line: int
    text: str


@dataclass(frozen=True)
class ReferenceResult:
    query: str
    hits: tuple[ReferenceHit, ...] = ()
    searched_files: int = 0
    truncated_note: str = ""
    method_note: str = ""
    """检索方式说明。引用查找的精度有限，说明清楚让读者知道结果的性质。"""

    def render(self) -> str:
        if not self.hits:
            return (
                f"未找到 {self.query} 的引用（已检索 {self.searched_files} 个文件）。"
                + (f"\n{self.method_note}" if self.method_note else "")
            )
        lines = [f"{self.query} 的引用（{len(self.hits)} 处，检索了 {self.searched_files} 个文件）："]
        lines.extend(f"  {hit.path}:{hit.line}  {hit.text.strip()[:100]}" for hit in self.hits)
        if self.method_note:
            lines.append(self.method_note)
        if self.truncated_note:
            lines.append(f"（{self.truncated_note}）")
        return "\n".join(lines)


@dataclass(frozen=True)
class StructureEntry:
    path: str
    is_dir: bool
    file_count: int = 0
    """目录条目的直接子文件数。文件条目为 0。"""


@dataclass
class StructureResult:
    root: str
    entries: list[StructureEntry] = field(default_factory=list)
    truncated_note: str = ""

    def render(self) -> str:
        if not self.entries:
            return f"{self.root} 下没有可列出的条目。"
        lines = [f"{self.root} 的结构（{len(self.entries)} 个条目）："]
        for entry in self.entries:
            if entry.is_dir:
                lines.append(f"  {entry.path}/  （{entry.file_count} 个文件）")
            else:
                lines.append(f"  {entry.path}")
        if self.truncated_note:
            lines.append(f"（{self.truncated_note}）")
        return "\n".join(lines)
