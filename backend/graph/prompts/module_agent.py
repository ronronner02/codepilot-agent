"""模块子 Agent 的 prompt。

设计要点集中在一条：**逼出可追溯的结论。** R7 要求报告的每个结论能落到文件路径，而
这个约束必须在子 Agent 这一层就生效——汇总节点只能校验引用是否存在，无法把一句「本
模块结构清晰」补救成有依据的结论。

所以 prompt 明确要求每条结论附带它依据的文件路径，并禁止那类放到任何仓库都成立的
表述。这是计划的硬底线：「本项目采用分层架构、代码结构清晰」这类句子不算结论。
"""

from __future__ import annotations

SYSTEM_PROMPT = """你在分析一个代码模块，产出这个模块的架构说明。你有工具可以读文件、查符号定义、查引用、列目录结构。

工作方式：先用工具了解模块，再给结论。不要凭文件名猜测实现——你可以读代码。

**每条结论必须能落到具体文件路径。** 这是硬要求：
- 可以写「认证逻辑集中在 auth/jwt.strategy.ts 与 auth/api-key.strategy.ts，两者都实现 PassportStrategy 接口」
- 不可以写「本模块结构清晰、职责划分合理」——这种句子放到任何仓库都成立，不构成结论
- 说「这个模块负责 X」时，要指出是哪个文件的哪部分让你这么判断

**路径一律写完整的仓库相对路径**，例如 `fastapi/_compat/shared.py`，不要只写 `shared.py`。
下游会按路径核验引用是否真实存在，裸文件名核验不过，那条结论会被丢弃。

分析要覆盖：
1. 这个模块承担什么职责，依据是什么
2. 内部怎么组织的（关键文件各自做什么，它们之间的关系）
3. 它与模块外部如何交互（对外暴露什么，依赖外部什么）
4. 实现上值得注意的地方（设计取舍、复杂点、可能的问题）

工具使用注意：
- 读文件时给出行范围，别一次读整个大文件
- 工具返回里若有「截断」提示，说明还有内容没看到，据此决定是否继续读
- find_references 是文本层匹配，同名局部变量也会命中，判断时留意

分析完成后直接输出最终说明，不要再调用工具。输出用中文。"""


def build_task_message(
    module_name: str,
    files: list[str],
    depth: str,
    selection_reason: str,
    neighbour_edges: dict[str, tuple[str, ...]],
    symbol_index: dict[str, tuple[str, ...]],
) -> str:
    """构造任务描述。

    带上 Planner 的挑选理由：让子 Agent 知道这个模块为什么被选中，分析才不会偏离
    Planner 的判断。带上邻接边与符号名单：这是「模块如何与外界交互」的现成依据，
    省掉子 Agent 用工具重新发现的轮次。
    """
    depth_hint = (
        "深度分析：读关键文件的主体实现，把设计取舍讲清楚。"
        if depth == "deep"
        else "标准分析：了解职责与组织方式即可，不必逐个文件细读。"
    )

    lines = [
        f"模块：{module_name}",
        f"分析深度：{depth_hint}",
        f"这个模块被选中的理由：{selection_reason}",
        "",
        f"成员文件（{len(files)} 个）：",
        *[f"  {path}" for path in files[:40]],
    ]
    if len(files) > 40:
        lines.append(f"  …另有 {len(files) - 40} 个文件，可用 list_structure 查看")

    if neighbour_edges:
        lines.extend(["", "指向模块外的依赖（来自依赖图）："])
        for path, targets in list(neighbour_edges.items())[:20]:
            lines.append(f"  {path} -> {', '.join(targets[:6])}")

    if symbol_index:
        lines.extend(["", "成员文件的符号名单（来自静态解析）："])
        for path, names in list(symbol_index.items())[:20]:
            shown = ", ".join(names[:10])
            more = f"…另 {len(names) - 10} 个" if len(names) > 10 else ""
            lines.append(f"  {path}: {shown}{more}")

    return "\n".join(lines)
