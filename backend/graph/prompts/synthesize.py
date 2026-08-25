"""报告汇总的 prompt。

设计围绕一条：**结论必须带引用，而引用会被校验。** prompt 里说清这一点不是威慑，是让
模型知道「给不出引用的话就别写」——写了也会被校验拒掉，白费 token。

模型拿到的是结构骨架加各模块分析，不再读文件：它的任务是综合而非发现。让它调工具重新
探索会重复子 Agent 已做的工作，且成本乘以模块数。

输出用 JSON，字段与 report schema 一一对应。自由文本无法校验——「哪句是结论、哪个是
它的引用」在自由文本里分不开，而校验的前提是两者可分离。
"""

from __future__ import annotations

import json

SYSTEM_PROMPT = """你在汇总一份代码仓库的架构分析报告。输入是静态分析产出的结构骨架，加上若干模块的独立分析。

**每条结论必须附带 citations（引用的文件路径，可选行号）。** 这是硬要求，不是格式建议：
- 引用会被程序校验——路径必须真实存在于仓库，行号必须在文件实际行数内。编造的路径与行号会被拒绝，那条结论随之丢弃。
- 因此：写不出引用的话就不要写。「本项目采用分层架构、代码结构清晰」这类放到任何仓库都成立的表述给不出引用，不要出现在报告里。
- 引用要指向支撑该结论的具体位置。说「认证逻辑集中在这三处」就给出三个路径。

按以下五节组织，每节是一组带引用的结论：

1. module_breakdown（模块划分）——这个仓库分成哪几块，各自承担什么
2. dependencies（依赖关系）——模块之间怎么依赖，有无循环或反向依赖
3. entrypoints（入口点）——程序从哪里开始执行，依据是什么
4. key_flows（关键流程）——一两条主要的执行路径，经过哪些文件
5. tech_stack（技术栈）——用了什么框架与库，从哪里看出来的

另外给一个 summary 字段：两三句话的总体印象。它不需要引用，也不会被当作结论校验。

只输出 JSON：
{
  "summary": "...",
  "module_breakdown": [{"text": "...", "citations": [{"path": "...", "line": 12, "end_line": 30}]}],
  "dependencies": [...],
  "entrypoints": [...],
  "key_flows": [...],
  "tech_stack": [...]
}

citations 里的 line 与 end_line 可省略——模块级结论未必落在某一行，只给 path 是合法的。

summary 与所有 text 字段用中文。文件路径、符号名、框架与库的名字保留原文，不要翻译。"""


def build_synthesis_message(
    repo: str,
    skeleton: dict[str, object],
    module_analyses: list[dict[str, str]],
    failed_modules: list[dict[str, str]],
) -> str:
    """构造汇总输入。

    带上失败模块清单：报告要标注缺失（R11、AE4），而模型需要知道哪些模块没有分析，
    才不会把「没提到」写成「不存在」。
    """
    payload: dict[str, object] = {
        "repo": repo,
        "skeleton": skeleton,
        "module_analyses": module_analyses,
    }
    if failed_modules:
        payload["modules_without_analysis"] = failed_modules
        payload["note"] = (
            "上列模块没有分析结果，不要凭空描述它们；报告的缺失部分会单独标注。"
        )
    return json.dumps(payload, ensure_ascii=False, indent=1)
