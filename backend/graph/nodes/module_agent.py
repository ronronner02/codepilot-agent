"""模块子 Agent。带工具的 ReAct 循环，并行扇出的执行单元。

三条来自计划的硬约束：

**节点只返回数据，不做落盘副作用。** LangGraph 的 checkpoint 写在 super-step 边界，
恢复时节点从函数头重跑——若这里写文件或写缓存，恢复会重复执行。落盘集中在汇聚节点。

**单模块失败不影响整体（R10、AE4）。** 实测确认节点抛异常会中断整图，所以异常在这里
转成 NodeFailure 数据。报告须标注该模块缺失及原因，故失败记录要带模块名与失败原因。

**轮次上限防打转。** ReAct 循环可能反复调同一个工具（读同一个文件、查同一个符号），
上限之外还按「重复调用」提前终止——前者防无限，后者防浪费：一个已经在打转的循环不会
因为多给几轮就产出更好的分析。

超限不等于失败：已经收集到的工具结果足以产出部分分析，标记为部分完成比丢弃更有价值
（测试场景明确要求「标记为部分完成」而非失败）。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from backend.config import Settings
from backend.graph.prompts.module_agent import SYSTEM_PROMPT, build_task_message
from backend.graph.state import ModuleAnalysis, ModuleTask, NodeFailure
from backend.providers.llm import LLMProvider
from backend.static_analysis.models import DependencyGraph, ParseOutcome
from backend.tools import TOOL_SCHEMAS, ToolContext, call_tool

logger = logging.getLogger("codepilot.module_agent")

# ReAct 循环的轮次上限。
#
# 取 8 的依据：观察到的典型路径是「列结构 -> 读 2-3 个关键文件 -> 查 1-2 个符号 ->
# 产出」，约 5-6 轮。给 8 留出余量，同时挡住打转。上限之外还有重复调用检测。
DEFAULT_MAX_ROUNDS = 8

# 同一工具加同一参数重复调用的容忍次数。
#
# 为什么要这条：模型偶尔会因为没读懂工具返回而重复调用同一个查询。这种循环不会因为
# 多给几轮而好转，早停下来用已有信息产出分析更划算。
MAX_REPEATED_CALLS = 2

# 工具输出的累积字符预算。
#
# **单次调用的行数上限不足以防上下文膨胀**（基准仓库实测教训）。8 轮 × 200 行看似有界，
# 但密集代码累积到一定量后 flash 档会出现重复崩溃——实测 fastapi#1 与 fastapi/_compat
# 两个 deep 档模块反复读大文件后，输出退化成 `WriterWriterWriter...` 这类废文本，而同一
# 模型在读多个小文件的 tests 模块上产出了质量很高的分析。区别是累积量，不是单次量。
#
# 取 60000 字符的依据：约 20K token，在 flash 档上下文里留足余量，也够读完 4-6 个中等
# 文件。超出即收敛——用已读内容产出分析，好过继续堆积到崩溃。
MAX_TOOL_OUTPUT_CHARS = 60_000

# 同文件读取范围的重叠判定阈值。
#
# 为什么需要它：实测 fastapi#1 依次读了 applications.py 的 700-1100 与 900-1100，
# 后者完全包含在前者里。精确签名匹配认为这是两次不同调用，于是重复内容堆进上下文——
# 而近似重复的内容正是重复崩溃的典型触发条件。
OVERLAP_RATIO_THRESHOLD = 0.6


def _tool_context(task: ModuleTask, settings: Settings) -> ToolContext:
    """构造工具上下文。

    分片里没有完整的 parse_outcome 与 graph（那会让每个 Send 的载荷随仓库规模膨胀），
    所以这里用分片带的符号名单与邻接边重建一个模块级的最小上下文。

    代价是子 Agent 的 find_definition 只能查到本模块的符号，find_references 的检索
    范围也限于本模块——跨模块查询查不到。这是分片设计的直接后果，且方向正确：子 Agent
    的职责是讲清这一个模块，跨模块关系由汇总节点从完整依赖图重建。
    """
    return ToolContext(
        workdir=Path(task["workdir"]),
        parse_outcome=task.get("parse_outcome") or ParseOutcome(),
        graph=task.get("graph") or DependencyGraph(),
        max_file_bytes=settings.max_file_bytes,
    )


def _render_tool_result(result: Any) -> str:
    render = getattr(result, "render", None)
    return render() if callable(render) else str(result)


# 推理模型的思维标记。网关不总会剥掉它们，漏进正文会让报告里出现半句思考过程。
_THINKING_MARKERS = ("</think>", "<think>", "<｜DSML｜", "</｜DSML｜")


def _strip_thinking(text: str) -> str:
    """去掉漏出的思维标记与其后的残句。

    实测确认：模块分析正文里出现了 `</think>` 与 `<｜DSML｜tool_cistrong>` 这类标记。
    它们是模型内部标记，不该进报告。
    """
    cleaned = text
    for marker in _THINKING_MARKERS:
        cleaned = cleaned.replace(marker, "\n")
    return "\n".join(line for line in cleaned.splitlines() if line.strip()).strip()


def _looks_degenerate(text: str, sample: int = 400) -> str:
    """检测重复崩溃。返回非空字符串表示判定为退化输出，内容是判定依据。

    为什么必须挡：实测 flash 档在长上下文下产出过 `WriterWriterWriter…` 与
    `(, (, (, (…` 这类文本。把它们当成模块分析交付，比如实报告该模块失败更糟——报告
    里会出现一段看似有内容实则无意义的文字，而读者要花时间才发现它没有信息。

    两个判据，任一命中即退化：
      单个短子串在尾部大量重复（`Writer` × 40）。
      字符多样性极低（`(, (, (, …` 的去重字符数占比很小）。
    """
    if len(text) < 200:
        return ""

    tail = text[-sample:]

    # 短子串重复。判据是「重复内容主导尾部」而非「出现多次」：结构化列表里
    # `- 文件：` 这类前缀重复 8 次是正常写法（400 字符里占 40 字符，10%），
    # 而 `Writer` × 40 占 240 字符（60%）才是崩溃。只数次数会误杀正常分析。
    for width in (4, 6, 8, 12):
        window = tail[:width]
        if not window.strip():
            continue
        occurrences = tail.count(window)
        if occurrences >= 8 and occurrences * width / len(tail) >= 0.4:
            return (
                f"尾部子串 {window!r} 重复 {occurrences} 次，"
                f"占尾部 {occurrences * width / len(tail):.0%}"
            )

    distinct_ratio = len(set(tail)) / len(tail)
    if distinct_ratio < 0.08:
        return f"尾部字符多样性过低（去重占比 {distinct_ratio:.1%}）"

    return ""


def _read_range(arguments: dict[str, Any]) -> tuple[str, int, int] | None:
    """从 read_file 参数里取 (路径, 起始行, 结束行)。非 read_file 调用返回 None。"""
    path = arguments.get("path")
    if not isinstance(path, str):
        return None
    start = arguments.get("start_line", 1)
    end = arguments.get("end_line")
    start_line = start if isinstance(start, int) else 1
    # 省略 end_line 时按单次上限估算覆盖范围。
    end_line = end if isinstance(end, int) else start_line + 199
    return path, start_line, max(end_line, start_line)


def _overlaps_previous(
    current: tuple[str, int, int], seen: list[tuple[str, int, int]]
) -> bool:
    """当前读取范围是否与同文件的既有范围大幅重叠。"""
    path, start, end = current
    span = end - start + 1
    for prev_path, prev_start, prev_end in seen:
        if prev_path != path:
            continue
        overlap = min(end, prev_end) - max(start, prev_start) + 1
        if overlap > 0 and overlap / span >= OVERLAP_RATIO_THRESHOLD:
            return True
    return False


async def analyze_module(
    task: ModuleTask, settings: Settings, provider: LLMProvider
) -> dict[str, Any]:
    """跑一个模块的 ReAct 循环。返回可并入 state 的字段。

    返回形状与 stub 节点一致（module_analyses / module_failures 都是带 reducer 的
    累积字段），扇出汇聚才能正确工作。
    """
    module = task["module"]
    ctx = _tool_context(task, settings)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_task_message(
                module_name=module.name,
                files=list(module.files),
                depth=task.get("depth", "standard"),
                selection_reason=task.get("selection_reason", ""),
                neighbour_edges=task.get("neighbour_edges") or {},
                symbol_index=task.get("symbol_index") or {},
            ),
        },
    ]

    call_counts: dict[str, int] = {}
    tool_trace: list[str] = []
    read_ranges: list[tuple[str, int, int]] = []
    output_chars = 0
    rounds_used = 0

    for round_index in range(1, DEFAULT_MAX_ROUNDS + 1):
        rounds_used = round_index
        try:
            response = await provider.chat(messages, tier="flash", tools=TOOL_SCHEMAS)
        except Exception as exc:  # noqa: BLE001 — 失败转数据，否则整图中断
            logger.warning("模块 %s 的调用失败 %s: %s", module.name, type(exc).__name__, exc)
            return {
                "module_failures": [
                    NodeFailure(
                        node="module_agent",
                        scope=module.name,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                ]
            }

        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None)

        if not tool_calls:
            # 模型给出最终答案，循环正常结束。
            summary = _strip_thinking(message.content or "")
            if not summary:
                return {
                    "module_failures": [
                        NodeFailure(
                            node="module_agent",
                            scope=module.name,
                            error="模型返回空内容且未调用工具",
                        )
                    ]
                }
            degenerate = _looks_degenerate(summary)
            if degenerate:
                return {
                    "module_failures": [
                        NodeFailure(
                            node="module_agent",
                            scope=module.name,
                            error=f"模型输出退化，未采用：{degenerate}",
                        )
                    ]
                }
            return {
                "module_analyses": [
                    _build_analysis(module.name, summary, ctx, tool_trace, rounds_used, "")
                ]
            }

        messages.append(
            {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )

        repeated = False
        for tool_call in tool_calls:
            name = tool_call.function.name
            try:
                arguments = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}

            signature = f"{name}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False)}"
            call_counts[signature] = call_counts.get(signature, 0) + 1
            if call_counts[signature] > MAX_REPEATED_CALLS:
                repeated = True

            # 重叠范围也算重复：精确签名匹配挡不住 700-1100 与 900-1100 这种包含关系，
            # 而近似重复的内容堆进上下文正是重复崩溃的触发条件。
            if name == "read_file" and isinstance(arguments, dict):
                current_range = _read_range(arguments)
                if current_range is not None:
                    if _overlaps_previous(current_range, read_ranges):
                        repeated = True
                    read_ranges.append(current_range)

            result = call_tool(ctx, name, arguments if isinstance(arguments, dict) else {})
            rendered = _render_tool_result(result)
            output_chars += len(rendered)
            tool_trace.append(signature)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": rendered,
                }
            )

        if repeated:
            # 打转了。用已有信息要一次总结，而不是继续耗轮次。
            return await _force_summary(
                module.name, messages, provider, ctx, tool_trace, rounds_used,
                "检测到重复或大幅重叠的工具调用，已提前收敛并基于已获取的信息产出分析",
            )

        if output_chars >= MAX_TOOL_OUTPUT_CHARS:
            # 累积上下文触顶。继续堆积会让输出退化成废文本（实测），不如现在收敛。
            return await _force_summary(
                module.name, messages, provider, ctx, tool_trace, rounds_used,
                f"工具输出累积达 {output_chars} 字符（上限 {MAX_TOOL_OUTPUT_CHARS}），"
                "已收敛并基于已读内容产出分析",
            )

    # 轮次用尽。已收集的工具结果足以产出部分分析，标记部分完成而非丢弃。
    return await _force_summary(
        module.name, messages, provider, ctx, tool_trace, rounds_used,
        f"达到 ReAct 轮次上限 {DEFAULT_MAX_ROUNDS}，基于已获取的信息产出分析",
    )


async def _force_summary(
    module_name: str,
    messages: list[dict[str, Any]],
    provider: LLMProvider,
    ctx: ToolContext,
    tool_trace: list[str],
    rounds_used: int,
    limitation: str,
) -> dict[str, Any]:
    """不带工具再要一次总结，逼出最终输出。

    去掉 tools 参数是关键：留着的话模型会继续调工具，循环就停不下来。
    """
    messages.append(
        {
            "role": "user",
            "content": "现在不要再调用工具，基于已获取的信息直接输出模块分析说明。",
        }
    )
    try:
        response = await provider.chat(messages, tier="flash")
        summary = _strip_thinking(response.choices[0].message.content or "")
    except Exception as exc:  # noqa: BLE001
        return {
            "module_failures": [
                NodeFailure(
                    node="module_agent",
                    scope=module_name,
                    error=f"收敛总结失败：{type(exc).__name__}: {exc}",
                )
            ]
        }

    if not summary:
        return {
            "module_failures": [
                NodeFailure(
                    node="module_agent", scope=module_name, error="收敛总结返回空内容"
                )
            ]
        }

    degenerate = _looks_degenerate(summary)
    if degenerate:
        # 交付废文本比报告失败更糟：读者要花时间才发现那段文字没有信息。
        return {
            "module_failures": [
                NodeFailure(
                    node="module_agent",
                    scope=module_name,
                    error=f"收敛后输出退化，未采用：{degenerate}",
                )
            ]
        }

    return {
        "module_analyses": [
            _build_analysis(module_name, summary, ctx, tool_trace, rounds_used, limitation)
        ]
    }


def _extract_cited_paths(summary: str, known: frozenset[str]) -> tuple[str, ...]:
    """从分析文本里认出被引用的仓库内文件。

    两种写法都要认（基准仓库实测教训）：

      全路径   `fastapi/_compat/shared.py`   直接子串匹配
      裸文件名 `shared.py`                    模型的常见写法

    只做全路径匹配时 `fastapi/_compat` 模块的引用提取返回 0——模型确实引用了三个文件，
    但写的是 `` `shared.py` `` 这类裸名。而引用数为 0 意味着 R7 的可追溯性对该模块完全
    没产出，下游校验会拒掉它的每一条结论。

    裸名匹配限定在 known 内且要求唯一：`known` 是本模块的成员文件（分片切片），同一
    模块内重名极少；真有重名则跳过，宁可漏掉也不要把引用指到错误的文件上。
    """
    cited = {path for path in known if path in summary}

    by_basename: dict[str, list[str]] = {}
    for path in known:
        by_basename.setdefault(path.rsplit("/", 1)[-1], []).append(path)

    for basename, candidates in by_basename.items():
        if len(candidates) != 1 or candidates[0] in cited:
            continue
        # 用词边界避免 `api.py` 命中 `legacy_api.py` 这类子串巧合。
        if re.search(rf"(?<![\w/]){re.escape(basename)}(?![\w])", summary):
            cited.add(candidates[0])

    return tuple(sorted(cited))


def _build_analysis(
    module_name: str,
    summary: str,
    ctx: ToolContext,
    tool_trace: list[str],
    rounds_used: int,
    limitation: str,
) -> ModuleAnalysis:
    """从分析文本里提取被引用的文件路径。

    只保留仓库内真实存在的路径：R7 要求结论可追溯，而模型可能写出不存在的路径。汇总
    节点（U8）会校验引用，但在这里先过一遍能让「哪些路径是有效引用」直接可用，也让
    幻觉路径不进入报告。
    """
    cited = _extract_cited_paths(summary, ctx.parsed_paths())
    return ModuleAnalysis(
        module_name=module_name,
        summary=summary,
        cited_paths=cited,
        tool_calls=tuple(tool_trace),
        rounds_used=rounds_used,
        limitation=limitation,
    )
