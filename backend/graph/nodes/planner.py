"""Planner 节点。决定深挖哪些模块、挖到什么程度。

分工（对应本单元的核心决定）：**规则排序，模型只做取舍与解释。**

规则算得出的用规则：中心度与规模是确定量，排序可复现、可向人核对。让模型重排会让
「凭什么这个模块排第三」无法回答，而 R8 要求决策依据可在产出中体现。

模型只做规则算不出的事：剔除结构重要但语义空洞的模块（31 个文件全是类型别名的模块
中心度很高却没什么可分析的），以及给出人能读的理由。

**规则排序必须能独立成立。** 模型返回非法或调用失败时降级为纯规则结果，而不是让整
次分析失败——深挖范围略不理想远好于没有报告。这也是本单元的测试场景之一。

截断由代码执行：扇出宽度上限是配置决定的硬约束（KTD16），不交给模型。模型只在上限内
表达「这个不值得挖」。两处都截会让实际宽度取决于先后顺序，难以解释。
"""

from __future__ import annotations

import json
import logging

from backend.config import Settings
from backend.graph.prompts.planner import SYSTEM_PROMPT, build_user_message
from backend.graph.state import AnalysisState, ModulePlan
from backend.providers.llm import LLMProvider
from backend.static_analysis.models import DependencyGraph, Module

logger = logging.getLogger("codepilot.planner")

# 规则得分的两项权重。和为 1，便于把得分读作加权平均。
#
# 中心度重于规模的理由：一个被广泛依赖的小模块（如核心路由）比一个无人依赖的大模块
# 更值得先看懂。规模只表示「有多少内容」，中心度表示「有多重要」。
DEFAULT_CENTRALITY_WEIGHT = 0.65
DEFAULT_SIZE_WEIGHT = 0.35

# 送给模型评估的候选数上限。
#
# 取扇出上限的两倍：模型要能剔除一部分，候选就得比最终名额多；但也不必把全部两百多个
# 模块都送去——排在很后面的模块即使被判为「值得挖」也进不了名额，评估它们纯属浪费。
CANDIDATE_MULTIPLIER = 2


def _normalize(values: dict[str, float]) -> dict[str, float]:
    """线性缩放到 [0, 1]。理由同 select_files：两个信号量纲差几个数量级，
    不归一化直接加权等于只用量纲大的那个。
    """
    if not values:
        return {}
    lowest = min(values.values())
    highest = max(values.values())
    span = highest - lowest
    if span <= 0:
        return {key: 0.0 for key in values}
    return {key: (value - lowest) / span for key, value in values.items()}


def rank_modules(
    modules: list[Module],
    centrality: dict[str, float],
    centrality_weight: float = DEFAULT_CENTRALITY_WEIGHT,
    size_weight: float = DEFAULT_SIZE_WEIGHT,
) -> list[tuple[Module, float]]:
    """按「中心度 + 规模」给模块排序，返回 (模块, 得分) 降序列表。

    模块的中心度取成员文件中心度之和，不取平均：一个模块由十个中等重要的文件组成，
    其整体重要性高于一个只有单个中等文件的模块。取平均会抹掉规模带来的影响，而那正是
    另一个信号要表达的——两者会互相抵消。

    同分按模块名排序，保证同输入同输出。
    """
    if not modules:
        return []

    centrality_totals = {
        module.name: sum(centrality.get(path, 0.0) for path in module.files)
        for module in modules
    }
    size_totals = {module.name: float(len(module.files)) for module in modules}

    centrality_scores = _normalize(centrality_totals)
    size_scores = _normalize(size_totals)

    total_weight = centrality_weight + size_weight
    if total_weight <= 0:
        centrality_weight, size_weight, total_weight = 1.0, 0.0, 1.0

    scored = [
        (
            module,
            (
                centrality_weight * centrality_scores.get(module.name, 0.0)
                + size_weight * size_scores.get(module.name, 0.0)
            )
            / total_weight,
        )
        for module in modules
    ]
    scored.sort(key=lambda item: (-item[1], item[0].name))
    return scored


def _candidate_payload(module: Module, score: float, rank: int) -> dict[str, object]:
    """给模型的候选描述。

    只给名字与文件名单——模型看不到代码内容，prompt 里也明确要求它不要臆测。给出
    rule_score 让它知道上游排序依据，据此判断「高分是因为真核心还是因为被到处导入的
    类型定义」。
    """
    return {
        "name": module.name,
        "rank": rank,
        "rule_score": round(score, 4),
        "file_count": len(module.files),
        "internal_edges": module.internal_edges,
        "external_edges": module.external_edges,
        # 文件名单截断：大模块的完整名单会让 prompt 膨胀，而判断语义空洞看前若干个
        # 文件名就够（类型定义模块的文件名高度同质）。
        "files": list(module.files[:12]),
        "files_truncated": max(0, len(module.files) - 12),
    }


def _rule_only_plans(
    ranked: list[tuple[Module, float]], limit: int, note: str
) -> tuple[list[ModulePlan], str]:
    """纯规则计划。模型不可用时的降级路径，也是模型结果的比较基线。"""
    plans = [
        ModulePlan(
            module=module,
            priority=index + 1,
            reason=(
                f"按中心度与规模排序位列第 {index + 1}"
                f"（{len(module.files)} 个文件，内部边 {module.internal_edges}）"
            ),
            rule_score=score,
            depth="deep" if index < 3 else "standard",
        )
        for index, (module, score) in enumerate(ranked[:limit])
    ]
    rationale = (
        f"按依赖图中心度（{DEFAULT_CENTRALITY_WEIGHT:.0%}）与模块规模"
        f"（{DEFAULT_SIZE_WEIGHT:.0%}）排序，取前 {len(plans)} 个（扇出上限 {limit}）。{note}"
    )
    return plans, rationale


def _parse_decisions(raw: str) -> dict[str, dict[str, object]]:
    """解析模型返回，按模块名索引。解析不出返回空字典，由调用方降级。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}

    items = payload.get("decisions") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return {}

    parsed: dict[str, dict[str, object]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str):
            continue
        parsed[name] = {
            "keep": bool(item.get("keep", True)),
            "depth": str(item.get("depth", "standard")).lower(),
            "reason": str(item.get("reason", "")).strip(),
        }
    return parsed


async def plan_modules(
    state: AnalysisState, settings: Settings, provider: LLMProvider | None = None
) -> dict[str, object]:
    """产出深挖计划。返回可直接并入 state 的字段。"""
    modules = state.get("modules") or []
    centrality = state.get("centrality") or {}
    limit = settings.max_fanout_width

    ranked = rank_modules(modules, centrality)
    if not ranked:
        return {
            "planned_modules": [],
            "plan_rationale": "聚类未产出任何模块，无深挖范围可定",
        }

    if provider is None:
        plans, rationale = _rule_only_plans(
            ranked, limit, "未配置 LLM provider，仅用规则排序，未做语义取舍。"
        )
        return {"planned_modules": plans, "plan_rationale": rationale}

    candidates = [
        _candidate_payload(module, score, index + 1)
        for index, (module, score) in enumerate(ranked[: limit * CANDIDATE_MULTIPLIER])
    ]

    try:
        response = await provider.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_message(candidates)},
            ],
            tier="pro",
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001 — 降级为规则排序，不让整次分析失败
        logger.warning("Planner 调用失败 %s: %s", type(exc).__name__, exc)
        plans, rationale = _rule_only_plans(
            ranked, limit, f"模型调用失败（{type(exc).__name__}），已降级为纯规则排序。"
        )
        return {"planned_modules": plans, "plan_rationale": rationale}

    decisions = _parse_decisions(content)
    if not decisions:
        plans, rationale = _rule_only_plans(
            ranked, limit, "模型返回无法解析为决定列表，已降级为纯规则排序。"
        )
        return {"planned_modules": plans, "plan_rationale": rationale}

    # 只认输入里真实存在的模块名，杜绝幻觉模块进入扇出——扇出会按模块名去读文件，
    # 幻觉名会让子 Agent 拿到空文件列表，表现为「这个模块分析不出内容」而非报错。
    # 只在送去评估过的候选集内选，不往排序更后面取。
    #
    # 实测教训：fastapi 的 24 个候选里模型剔掉 17 个，只剩 7 个。若继续往第 25 位之后
    # 取来填满 12 个名额，填进来的正是模型刚判定为无价值的同类模块（更多 docs_src 教程
    # 示例）——它们从未被评估，走漏答兜底路径混入，把语义取舍的效果整个抵消掉。
    #
    # 名额不足就产出更短的计划。7 个挑对的模块好过 12 个里有 5 个已知无价值的。
    evaluated = ranked[: limit * CANDIDATE_MULTIPLIER]
    valid_names = {module.name for module, _ in evaluated}
    dropped: list[str] = []
    selected: list[ModulePlan] = []

    for index, (module, score) in enumerate(evaluated):
        if len(selected) >= limit:
            break
        decision = decisions.get(module.name)
        if decision is None:
            # 模型漏答：保留并用规则理由，不因漏答丢掉高分模块。
            selected.append(
                ModulePlan(
                    module=module,
                    priority=len(selected) + 1,
                    reason=f"按中心度与规模排序位列第 {index + 1}（模型未给出评估）",
                    rule_score=score,
                    depth="deep" if len(selected) < 3 else "standard",
                )
            )
            continue

        if not decision["keep"]:
            dropped.append(f"{module.name}（{decision['reason'] or '未给出理由'}）")
            continue

        depth = decision["depth"] if decision["depth"] in ("deep", "standard") else "standard"
        selected.append(
            ModulePlan(
                module=module,
                priority=len(selected) + 1,
                reason=str(decision["reason"]) or f"按排序位列第 {index + 1}",
                rule_score=score,
                depth=str(depth),
            )
        )

    hallucinated = [name for name in decisions if name not in valid_names]

    rationale = (
        f"按依赖图中心度（{DEFAULT_CENTRALITY_WEIGHT:.0%}）与模块规模"
        f"（{DEFAULT_SIZE_WEIGHT:.0%}）对 {len(modules)} 个模块排序，"
        f"取前 {len(candidates)} 个交模型做语义取舍，最终选定 {len(selected)} 个"
        f"（扇出上限 {limit}）。"
    )
    if dropped:
        rationale += f"剔除 {len(dropped)} 个语义空洞模块：{'；'.join(dropped[:5])}。"
    if len(selected) < limit:
        # 名额未用满要说明原因，否则读者会以为仓库只有这么几个模块。
        rationale += (
            f"选定数少于上限，因候选集内有 {len(dropped)} 个被判为不值得深挖；"
            f"未扩大候选范围——排序更后的模块得分更低，语义价值不会更高。"
        )
    if hallucinated:
        # 幻觉模块名要留痕：它说明模型在编造，而不只是判断不准。
        rationale += f"忽略 {len(hallucinated)} 个不存在于骨架的模块名：{', '.join(hallucinated[:3])}。"

    return {"planned_modules": selected, "plan_rationale": rationale}
