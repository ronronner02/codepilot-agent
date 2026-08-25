"""LLM JSON 返回的提取与修复。

三处调用（Planner 取舍、评审判断、报告汇总）都要求模型返回 JSON，也都撞上同一类问题：
返回是 JSON **加上别的东西**。原先各处用同一段「掐头去尾」逻辑——去掉首行的 ```json，
再截到最后一个 ``` ——它只覆盖「整个返回恰好是一个代码块」这一种形态。

实测（网关上的 pro 档，DEA-Net 汇总，重复采样约 1/8 失败）确认两种它扛不住的返回：

  多个代码块   模型先给一份字段不全的 JSON，紧接着写「上面遗漏了必要字段，完整报告如下」，
               再给一份完整的。掐头去尾把「块1 + 说明文字 + 块2」当成一个整体交给
               json.loads，报 `Extra data`。
  内层裸引号   summary 里写了 `"网络定义 + 训练脚本 + 推理入口"`，双引号没转义，JSON
               字符串提前闭合，整段语法就破了。报错位置在第 2 行，与真正的问题同处，
               但现有逻辑对它无能为力。

对应两件事：**枚举全部候选**而不是赌一个，以及对候选做一次针对裸引号的修复。

枚举而非「取第一个能解析的」是因为上面第一种形态里先出现的那块恰好是残缺的——取第一个
会选中字段最少的那份，报告只剩一句 summary。取舍标准由调用方给（汇总挑结论最多的），
这里只负责把候选给全。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger("codepilot.json_reply")

# 代码块。语言标记可有可无；末尾允许缺失闭合标记（输出被截断时就是这样）。
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)(?:```|\Z)", re.DOTALL)

_OPENERS = {"{": "}", "[": "]"}
_CLOSERS = {"}", "]"}

# JSON 里字符串的闭合引号后面只可能出现这些字符（或空白、或文本结束）。
_STRUCTURAL_AFTER_QUOTE = frozenset(",}]:")


def _fenced_blocks(text: str) -> list[str]:
    return [match.group(1) for match in _FENCE_RE.finditer(text)]


def _balanced_spans(text: str) -> list[str]:
    """按括号配对切出顶层的 {...} 与 [...] 区段。

    比正则可靠：嵌套结构用正则切不准。计数时跳过字符串内部的括号，否则一个出现在
    文字里的 `}` 就会提前收尾。

    这一步顺带解决「JSON 后面跟了一段说明文字」——那种返回整段解析不了，但切出来的
    区段本身是合法的。
    """
    spans: list[str] = []
    stack: list[str] = []
    start = -1
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char in _OPENERS:
            if not stack:
                start = index
            stack.append(_OPENERS[char])
        elif char in _CLOSERS:
            if stack and stack[-1] == char:
                stack.pop()
                if not stack and start >= 0:
                    spans.append(text[start : index + 1])
                    start = -1
            else:
                # 不配对的闭括号：此前的计数已经不可信，从这里重新开始。
                stack.clear()
                start = -1

    return spans


def escape_inner_quotes(text: str) -> str:
    """给字符串内部的裸引号补上转义。

    判据是「这个引号后面跟的是不是结构字符」：字符串的闭合引号后面只可能是 `,` `}`
    `]` `:` 或空白，跟着别的字符说明它出现在句子中间，是文字而非语法。

    `"形成"网络定义 + 训练脚本"的结构"` 里，`网` 与 `的` 分别跟在两个引号后面，两处
    都判为文字并转义，语法随之恢复。

    **best-effort，不保证。** 恰好停在逗号前的引号（`"他说"好", 然后"`）判不出来，会
    被当成闭合引号。所以修复结果仍要过一次 json.loads，失败就换下一个候选——这里的
    定位是多一次机会，不是保证成功。
    """
    out: list[str] = []
    in_string = False
    escaped = False
    length = len(text)

    for index, char in enumerate(text):
        if not in_string:
            out.append(char)
            if char == '"':
                in_string = True
            continue

        if escaped:
            out.append(char)
            escaped = False
            continue
        if char == "\\":
            out.append(char)
            escaped = True
            continue
        if char != '"':
            out.append(char)
            continue

        # 往后找第一个非空白字符，判断这个引号是闭合还是文字。
        probe = index + 1
        while probe < length and text[probe] in " \t\r\n":
            probe += 1
        if probe >= length or text[probe] in _STRUCTURAL_AFTER_QUOTE:
            out.append(char)
            in_string = False
        else:
            out.append('\\"')

    return "".join(out)


def _candidate_texts(raw: str) -> list[str]:
    """候选文本，按优先级排列，去重。

    代码块在前：模型用它标记「这才是要给的数据」，比整段原文更可能干净。整段原文垫底，
    覆盖「返回本就是裸 JSON」的常见情形。
    """
    candidates = [*_fenced_blocks(raw), *_balanced_spans(raw), raw]

    seen: set[str] = set()
    ordered: list[str] = []
    for text in candidates:
        stripped = text.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        ordered.append(stripped)
    return ordered


def iter_json_values(raw: str) -> Iterator[Any]:
    """产出返回里所有能解析出来的 JSON 值。

    每个候选先直接解析，失败再试一次裸引号修复。调用方按自己的标准挑——需要哪种形状
    （对象还是数组）、哪份内容更完整，只有调用方知道。
    """
    for text in _candidate_texts(raw):
        try:
            yield json.loads(text)
            continue
        except json.JSONDecodeError:
            pass

        repaired = escape_inner_quotes(text)
        if repaired == text:
            continue
        try:
            yield json.loads(repaired)
        except json.JSONDecodeError:
            continue


def log_parse_failure(node: str, raw: str, head: int = 400) -> None:
    """记下解析失败时的返回原文开头。

    没有这一条，「无法解析」在日志里只是一个结论：定位上面那两种形态时不得不另写脚本
    重复采样去撞。原文开头足以区分「模型加了前言」、「引号没转义」与「压根不是 JSON」。
    """
    snippet = raw[:head].replace("\n", "\\n")
    logger.warning(
        "%s 无法从返回中解析出 JSON（长度 %d），开头 %d 字符：%s",
        node,
        len(raw),
        head,
        snippet,
    )
