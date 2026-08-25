"""Planner：规则排序 + 模型语义取舍。

分工的验证重点不同：
  规则排序部分断言具体顺序——它是确定量，可复现。
  模型取舍部分只断言「取舍被正确解释」，不断言模型判断质量（那由基准仓库实跑核对）。

贯穿本文件的三条约束：
  幻觉模块名不得进入扇出——扇出会按名字读文件，幻觉名会让子 Agent 拿到空列表。
  截断由代码执行，不交给模型——两处都截会让实际宽度取决于先后顺序。
  模型失败必须降级为规则结果，而非整次失败。
"""

from __future__ import annotations

import json

import pytest

from backend.graph.nodes.planner import plan_modules, rank_modules
from backend.graph.state import ModulePlan
from backend.static_analysis.models import Module
from tests.support import make_settings


class _FakeProvider:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[dict[str, object]] = []

    async def chat(self, messages, tier="flash", tools=None, response_format=None):  # type: ignore[no-untyped-def]
        self.requests.append({"messages": messages, "tier": tier})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome

        class _Message:
            content = outcome

        class _Choice:
            message = _Message()

        class _Response:
            choices = [_Choice()]

        return _Response()


def _module(name: str, count: int, internal: int = 0) -> Module:
    return Module(
        name=name,
        files=tuple(f"{name}/f{i}.py" for i in range(count)),
        internal_edges=internal,
        external_edges=0,
        origin="directory",
    )


def _state(modules: list[Module], centrality: dict[str, float] | None = None) -> dict:
    if centrality is None:
        centrality = {f: 1.0 / len(modules) for m in modules for f in m.files}
    return {"modules": modules, "centrality": centrality}


def _decisions(*items: dict[str, object]) -> str:
    return json.dumps({"decisions": list(items)}, ensure_ascii=False)


def _keep_all(modules: list[Module]) -> str:
    return _decisions(
        *[
            {"name": m.name, "keep": True, "depth": "standard", "reason": f"{m.name} 值得看"}
            for m in modules
        ]
    )


class TestRuleRanking:
    def test_higher_centrality_ranks_first(self) -> None:
        modules = [_module("core", 3), _module("edge", 3)]
        centrality = {f: 0.3 for f in modules[0].files} | {f: 0.01 for f in modules[1].files}
        ranked = rank_modules(modules, centrality)
        assert [m.name for m, _ in ranked] == ["core", "edge"]

    def test_module_centrality_sums_not_averages(self) -> None:
        """十个中等重要文件组成的模块，整体重要性高于单个中等文件的模块。

        取平均会抹掉规模的影响，而规模正是另一个信号要表达的——两者会互相抵消。
        """
        big = _module("big", 10)
        small = _module("small", 1)
        centrality = {f: 0.1 for f in (*big.files, *small.files)}
        ranked = rank_modules([big, small], centrality)
        assert ranked[0][0].name == "big"

    def test_size_contributes_to_score(self) -> None:
        modules = [_module("large", 20), _module("tiny", 1)]
        centrality = {f: 0.05 for m in modules for f in m.files}
        ranked = rank_modules(modules, centrality)
        assert ranked[0][0].name == "large"

    def test_ties_break_by_name_for_determinism(self) -> None:
        modules = [_module("zeta", 2), _module("alpha", 2)]
        centrality = {f: 0.1 for m in modules for f in m.files}
        assert [m.name for m, _ in rank_modules(modules, centrality)] == ["alpha", "zeta"]

    def test_empty_modules_returns_empty(self) -> None:
        assert rank_modules([], {}) == []

    def test_single_module_does_not_degenerate(self) -> None:
        """归一化在单元素时分母为 0，须不崩且给出合法得分。"""
        ranked = rank_modules([_module("only", 3)], {"only/f0.py": 0.5})
        assert len(ranked) == 1
        assert 0.0 <= ranked[0][1] <= 1.0


class TestNoHallucinatedModules:
    async def test_only_real_modules_reach_the_plan(self) -> None:
        """幻觉模块名会让子 Agent 拿到空文件列表，表现为「分析不出内容」而非报错。"""
        modules = [_module("real", 3)]
        provider = _FakeProvider(
            [
                _decisions(
                    {"name": "real", "keep": True, "depth": "deep", "reason": "真实模块"},
                    {"name": "imaginary", "keep": True, "depth": "deep", "reason": "编造的"},
                )
            ]
        )
        result = await plan_modules(_state(modules), make_settings(), provider)  # type: ignore[arg-type]
        names = {p.module.name for p in result["planned_modules"]}  # type: ignore[union-attr]
        assert names == {"real"}

    async def test_hallucinated_names_recorded_in_rationale(self) -> None:
        """幻觉要留痕：它说明模型在编造，而不只是判断不准。"""
        modules = [_module("real", 3)]
        provider = _FakeProvider(
            [
                _decisions(
                    {"name": "real", "keep": True, "depth": "deep", "reason": "真实"},
                    {"name": "ghost", "keep": True, "depth": "deep", "reason": "编造"},
                )
            ]
        )
        result = await plan_modules(_state(modules), make_settings(), provider)  # type: ignore[arg-type]
        assert "ghost" in str(result["plan_rationale"])
        assert "不存在于骨架" in str(result["plan_rationale"])


class TestTruncation:
    async def test_truncated_to_fanout_limit(self) -> None:
        modules = [_module(f"m{i:02d}", 3) for i in range(20)]
        provider = _FakeProvider([_keep_all(modules)])
        result = await plan_modules(
            _state(modules), make_settings(max_fanout_width=5), provider  # type: ignore[arg-type]
        )
        assert len(result["planned_modules"]) == 5  # type: ignore[arg-type]

    async def test_truncation_keeps_high_priority(self) -> None:
        modules = [_module("top", 10), *[_module(f"low{i}", 1) for i in range(10)]]
        centrality = {f: 0.5 for f in modules[0].files} | {
            f: 0.001 for m in modules[1:] for f in m.files
        }
        provider = _FakeProvider([_keep_all(modules)])
        result = await plan_modules(
            {"modules": modules, "centrality": centrality},
            make_settings(max_fanout_width=2),
            provider,  # type: ignore[arg-type]
        )
        assert result["planned_modules"][0].module.name == "top"  # type: ignore[index]

    async def test_priorities_are_sequential_from_one(self) -> None:
        modules = [_module(f"m{i}", 3) for i in range(4)]
        provider = _FakeProvider([_keep_all(modules)])
        result = await plan_modules(
            _state(modules), make_settings(max_fanout_width=3), provider  # type: ignore[arg-type]
        )
        assert [p.priority for p in result["planned_modules"]] == [1, 2, 3]  # type: ignore[union-attr]

    async def test_rationale_states_the_limit(self) -> None:
        modules = [_module(f"m{i}", 3) for i in range(6)]
        provider = _FakeProvider([_keep_all(modules)])
        result = await plan_modules(
            _state(modules), make_settings(max_fanout_width=2), provider  # type: ignore[arg-type]
        )
        assert "扇出上限 2" in str(result["plan_rationale"])


class TestSemanticFiltering:
    async def test_dropped_module_excluded_with_reason(self) -> None:
        modules = [_module("types", 30), _module("service", 8)]
        provider = _FakeProvider(
            [
                _decisions(
                    {"name": "types", "keep": False, "reason": "全是类型别名，深挖产不出洞察"},
                    {"name": "service", "keep": True, "depth": "deep", "reason": "承担业务逻辑"},
                )
            ]
        )
        result = await plan_modules(_state(modules), make_settings(), provider)  # type: ignore[arg-type]
        names = {p.module.name for p in result["planned_modules"]}  # type: ignore[union-attr]
        assert names == {"service"}
        assert "类型别名" in str(result["plan_rationale"])

    async def test_depth_from_model_honored(self) -> None:
        modules = [_module("core", 5)]
        provider = _FakeProvider(
            [_decisions({"name": "core", "keep": True, "depth": "deep", "reason": "核心"})]
        )
        result = await plan_modules(_state(modules), make_settings(), provider)  # type: ignore[arg-type]
        assert result["planned_modules"][0].depth == "deep"  # type: ignore[index]

    async def test_invalid_depth_falls_back_to_standard(self) -> None:
        modules = [_module("core", 5)]
        provider = _FakeProvider(
            [_decisions({"name": "core", "keep": True, "depth": "exhaustive", "reason": "核心"})]
        )
        result = await plan_modules(_state(modules), make_settings(), provider)  # type: ignore[arg-type]
        assert result["planned_modules"][0].depth == "standard"  # type: ignore[index]

    async def test_every_plan_has_non_empty_reason(self) -> None:
        modules = [_module(f"m{i}", 3) for i in range(3)]
        provider = _FakeProvider([_keep_all(modules)])
        result = await plan_modules(_state(modules), make_settings(), provider)  # type: ignore[arg-type]
        assert all(p.reason for p in result["planned_modules"])  # type: ignore[union-attr]

    async def test_slots_not_filled_from_beyond_candidate_set(self) -> None:
        """名额不足时不往排序更后面取——那些模块从未被评估。

        实测教训：fastapi 的 24 个候选里模型剔掉 17 个只剩 7 个，继续往第 25 位之后取
        会把模型刚判定为无价值的同类模块（更多 docs_src 教程）填进来，走漏答兜底路径
        混入，把语义取舍的效果整个抵消。
        """
        modules = [_module(f"m{i:02d}", 5) for i in range(20)]
        # 候选集是前 8 个（上限 4 × 2）。全部剔除，名额应为空而非从第 9 个起补。
        provider = _FakeProvider(
            [
                _decisions(
                    *[
                        {"name": f"m{i:02d}", "keep": False, "reason": "空洞"}
                        for i in range(8)
                    ]
                )
            ]
        )
        result = await plan_modules(
            _state(modules), make_settings(max_fanout_width=4), provider  # type: ignore[arg-type]
        )
        assert result["planned_modules"] == []
        assert "不值得深挖" in str(result["plan_rationale"])

    async def test_short_plan_explains_why(self) -> None:
        """名额未用满要说明原因，否则读者会以为仓库只有这么几个模块。"""
        modules = [_module(f"m{i}", 5) for i in range(8)]
        provider = _FakeProvider(
            [
                _decisions(
                    {"name": "m0", "keep": True, "depth": "deep", "reason": "核心"},
                    *[{"name": f"m{i}", "keep": False, "reason": "空洞"} for i in range(1, 8)],
                )
            ]
        )
        result = await plan_modules(
            _state(modules), make_settings(max_fanout_width=4), provider  # type: ignore[arg-type]
        )
        assert len(result["planned_modules"]) == 1  # type: ignore[arg-type]
        assert "选定数少于上限" in str(result["plan_rationale"])
        assert "未扩大候选范围" in str(result["plan_rationale"])

    async def test_omitted_module_kept_with_rule_reason(self) -> None:
        """模型漏答不应丢掉高分模块——漏答不等于「不值得挖」。"""
        modules = [_module("answered", 5), _module("omitted", 5)]
        provider = _FakeProvider(
            [_decisions({"name": "answered", "keep": True, "depth": "deep", "reason": "答了"})]
        )
        result = await plan_modules(_state(modules), make_settings(), provider)  # type: ignore[arg-type]
        names = {p.module.name for p in result["planned_modules"]}  # type: ignore[union-attr]
        assert names == {"answered", "omitted"}
        omitted = next(
            p for p in result["planned_modules"] if p.module.name == "omitted"  # type: ignore[union-attr]
        )
        assert "模型未给出评估" in omitted.reason


class TestFallback:
    async def test_call_failure_falls_back_to_rules(self) -> None:
        """深挖范围略不理想远好于没有报告。"""
        modules = [_module("a", 5), _module("b", 3)]
        provider = _FakeProvider([RuntimeError("网关 503")])
        result = await plan_modules(_state(modules), make_settings(), provider)  # type: ignore[arg-type]
        assert len(result["planned_modules"]) == 2  # type: ignore[arg-type]
        assert "降级为纯规则排序" in str(result["plan_rationale"])
        assert "RuntimeError" in str(result["plan_rationale"])

    async def test_unparseable_reply_falls_back_to_rules(self) -> None:
        modules = [_module("a", 5)]
        provider = _FakeProvider(["这不是 JSON"])
        result = await plan_modules(_state(modules), make_settings(), provider)  # type: ignore[arg-type]
        assert len(result["planned_modules"]) == 1  # type: ignore[arg-type]
        assert "无法解析" in str(result["plan_rationale"])

    async def test_no_provider_uses_rules_and_discloses(self) -> None:
        modules = [_module("a", 5)]
        result = await plan_modules(_state(modules), make_settings(), None)
        assert len(result["planned_modules"]) == 1  # type: ignore[arg-type]
        assert "未配置 LLM" in str(result["plan_rationale"])

    async def test_fallback_still_respects_limit(self) -> None:
        modules = [_module(f"m{i}", 3) for i in range(10)]
        provider = _FakeProvider([RuntimeError("失败")])
        result = await plan_modules(
            _state(modules), make_settings(max_fanout_width=4), provider  # type: ignore[arg-type]
        )
        assert len(result["planned_modules"]) == 4  # type: ignore[arg-type]

    async def test_no_modules_yields_empty_plan(self) -> None:
        result = await plan_modules({"modules": [], "centrality": {}}, make_settings(), None)
        assert result["planned_modules"] == []
        assert "未产出任何模块" in str(result["plan_rationale"])


class TestRequestShape:
    async def test_uses_pro_tier(self) -> None:
        modules = [_module("a", 3)]
        provider = _FakeProvider([_keep_all(modules)])
        await plan_modules(_state(modules), make_settings(), provider)  # type: ignore[arg-type]
        assert provider.requests[0]["tier"] == "pro"

    async def test_candidate_set_larger_than_limit(self) -> None:
        """模型要能剔除一部分，候选就得比最终名额多。"""
        modules = [_module(f"m{i:02d}", 3) for i in range(20)]
        provider = _FakeProvider([_keep_all(modules)])
        await plan_modules(
            _state(modules), make_settings(max_fanout_width=4), provider  # type: ignore[arg-type]
        )
        payload = json.loads(provider.requests[0]["messages"][1]["content"])  # type: ignore[index]
        assert len(payload["candidates"]) == 8

    async def test_prompt_forbids_reordering(self) -> None:
        """排序是算出来的，让模型重排会让顺序不可复现也无法解释。"""
        modules = [_module("a", 3)]
        provider = _FakeProvider([_keep_all(modules)])
        await plan_modules(_state(modules), make_settings(), provider)  # type: ignore[arg-type]
        system = provider.requests[0]["messages"][0]["content"]  # type: ignore[index]
        assert "不是重新排序" in system

    async def test_prompt_requires_chinese_reason(self) -> None:
        """实测 planner 返回过英文理由——模型不总跟随输入语言，须显式要求。

        报告是交付物，中英混杂不可接受。
        """
        modules = [_module("a", 3)]
        provider = _FakeProvider([_keep_all(modules)])
        await plan_modules(_state(modules), make_settings(), provider)  # type: ignore[arg-type]
        system = provider.requests[0]["messages"][0]["content"]  # type: ignore[index]
        assert "reason 用中文" in system

    async def test_file_names_truncated_in_payload(self) -> None:
        """大模块的完整名单会让 prompt 膨胀，判断语义空洞看前若干个文件名就够。"""
        modules = [_module("big", 40)]
        provider = _FakeProvider([_keep_all(modules)])
        await plan_modules(_state(modules), make_settings(), provider)  # type: ignore[arg-type]
        payload = json.loads(provider.requests[0]["messages"][1]["content"])  # type: ignore[index]
        candidate = payload["candidates"][0]
        assert len(candidate["files"]) == 12
        assert candidate["files_truncated"] == 28


class TestExplainability:
    async def test_rule_score_preserved_alongside_model_reason(self) -> None:
        """得分是算出来的，理由是模型给的。混在一起会让读者无法判断哪部分可复现。"""
        modules = [_module("a", 5)]
        provider = _FakeProvider(
            [_decisions({"name": "a", "keep": True, "depth": "deep", "reason": "模型理由"})]
        )
        result = await plan_modules(_state(modules), make_settings(), provider)  # type: ignore[arg-type]
        plan = result["planned_modules"][0]  # type: ignore[index]
        assert plan.reason == "模型理由"
        assert 0.0 <= plan.rule_score <= 1.0

    async def test_rationale_states_weights(self) -> None:
        """R8：决策依据要可在产出中体现，权重是依据的一部分。"""
        modules = [_module("a", 3)]
        provider = _FakeProvider([_keep_all(modules)])
        result = await plan_modules(_state(modules), make_settings(), provider)  # type: ignore[arg-type]
        rationale = str(result["plan_rationale"])
        assert "中心度" in rationale
        assert "规模" in rationale
        assert "%" in rationale
