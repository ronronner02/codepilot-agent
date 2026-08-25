"""工具层的执行上下文。

**工具不依赖 LangGraph state。** KTD7 要求工具层单一实现，同时供内部管道与 MCP
server 调用，而 MCP 客户端不存在 state 的概念。所以工具需要的一切通过显式上下文传入。

这个约束也让工具可单独测试：构造一个 ToolContext 就能跑，不必启动图。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backend.static_analysis.models import DependencyGraph, ParseOutcome, Symbol


@dataclass(frozen=True)
class ToolContext:
    """工具执行所需的仓库上下文。

    parse_outcome 与 graph 是 U3、U4 的产出。工具不自己解析——重复解析既慢也可能与
    管道的结果不一致，而「工具看到的符号表」与「报告依据的符号表」不一致会让引用
    核验失效（R7 的可追溯要求）。
    """

    workdir: Path
    parse_outcome: ParseOutcome
    graph: DependencyGraph
    max_file_bytes: int = 1_048_576

    def symbols(self) -> list[Symbol]:
        return [s for f in self.parse_outcome.parsed for s in f.symbols]

    def parsed_paths(self) -> frozenset[str]:
        return frozenset(f.path for f in self.parse_outcome.parsed)
