"""模块子 Agent 的 ReAct 循环。

用假 provider 驱动：要验证的是「循环怎么终止、失败怎么转数据、工具结果怎么回灌」，
不是模型的分析质量（后者由基准仓库实跑核对）。

三条来自计划的约束是本文件的重点：
  节点只返回数据，不做落盘副作用（checkpoint 恢复会重跑函数头）。
  单模块失败转成 NodeFailure，不抛异常——实测确认抛异常会中断整图（R10、AE4）。
  轮次上限与打转检测都要能终止循环，且超限标记为部分完成而非失败。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.graph.nodes.module_agent import DEFAULT_MAX_ROUNDS, analyze_module
from backend.graph.state import ModuleTask
from backend.static_analysis.graph import build_graph
from backend.static_analysis.models import Module
from backend.static_analysis.parser import parse_repo
from tests.support import make_settings

MAX_BYTES = 1_048_576


class _ToolCall:
    def __init__(self, name: str, arguments: dict[str, object], call_id: str = "c1") -> None:
        self.id = call_id
        self.type = "function"

        class _Function:
            pass

        self.function = _Function()
        self.function.name = name  # type: ignore[attr-defined]
        self.function.arguments = json.dumps(arguments)  # type: ignore[attr-defined]


class _Turn:
    """一次模型返回：要么带 tool_calls，要么给最终文本。"""

    def __init__(self, content: str = "", tool_calls: list[_ToolCall] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeProvider:
    """按队列返回预设轮次。

    不带 tools 的调用（收敛总结）优先取队列里的下一个纯文本轮次——真实模型在没有工具
    可用时只会给最终答案，而按队列顺序取会拿到一个 tool 轮次，让「收敛」测出空内容。
    """

    def __init__(self, turns: list[object]) -> None:
        self.turns = list(turns)
        self.requests: list[dict[str, object]] = []

    async def chat(self, messages, tier="flash", tools=None, response_format=None):  # type: ignore[no-untyped-def]
        self.requests.append({"messages": list(messages), "tier": tier, "tools": tools})

        if tools is None:
            turn = self._take_summary_turn()
        else:
            turn = self.turns.pop(0) if self.turns else _Turn(content="兜底总结")

        if isinstance(turn, Exception):
            raise turn

        class _Choice:
            message = turn

        class _Response:
            choices = [_Choice()]

        return _Response()

    def _take_summary_turn(self) -> object:
        """取队列里第一个纯文本轮次或异常，跳过剩余的 tool 轮次。"""
        for index, candidate in enumerate(self.turns):
            if isinstance(candidate, Exception) or not getattr(candidate, "tool_calls", None):
                return self.turns.pop(index)
        return _Turn(content="兜底总结")


@pytest.fixture
def task(tmp_path: Path) -> ModuleTask:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "core.py").write_text(
        "def helper(v):\n    return v * 2\n\n\nclass Service:\n    def run(self):\n        return helper(1)\n",
        encoding="utf-8",
    )
    (root / "pkg" / "api.py").write_text(
        "from pkg.core import helper\n\n\ndef handle():\n    return helper(3)\n", encoding="utf-8"
    )

    files = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]
    outcome = parse_repo(root, files, MAX_BYTES)
    graph = build_graph(root, outcome.parsed, max_nodes=5000)

    return ModuleTask(
        module=Module(
            name="pkg",
            files=("pkg/api.py", "pkg/core.py"),
            internal_edges=1,
            external_edges=0,
            origin="directory",
        ),
        workdir=str(root),
        neighbour_edges={},
        symbol_index={"pkg/core.py": ("helper", "Service.run")},
        depth="deep",
        selection_reason="核心模块",
        parse_outcome=outcome,
        graph=graph,
    )


class TestNormalCompletion:
    async def test_direct_answer_without_tools(self, task: ModuleTask) -> None:
        provider = _FakeProvider([_Turn(content="pkg/core.py 提供 helper 与 Service。")])
        result = await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        analyses = result["module_analyses"]
        assert len(analyses) == 1
        assert analyses[0].module_name == "pkg"
        assert analyses[0].limitation == ""

    async def test_tool_then_answer(self, task: ModuleTask) -> None:
        provider = _FakeProvider(
            [
                _Turn(tool_calls=[_ToolCall("read_file", {"path": "pkg/core.py"})]),
                _Turn(content="helper 定义在 pkg/core.py 第 1 行。"),
            ]
        )
        result = await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        analysis = result["module_analyses"][0]
        assert analysis.rounds_used == 2
        assert analysis.tool_calls
        assert "read_file" in analysis.tool_calls[0]

    async def test_tool_result_fed_back_to_model(self, task: ModuleTask) -> None:
        """工具结果必须回灌进对话，否则模型看不到自己查到了什么。"""
        provider = _FakeProvider(
            [
                _Turn(tool_calls=[_ToolCall("read_file", {"path": "pkg/core.py"})]),
                _Turn(content="完成"),
            ]
        )
        await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        second_request = provider.requests[1]["messages"]
        tool_messages = [m for m in second_request if m.get("role") == "tool"]  # type: ignore[union-attr]
        assert tool_messages
        assert "def helper" in tool_messages[0]["content"]

    async def test_uses_flash_tier(self, task: ModuleTask) -> None:
        """扇出是高频低判断，用 flash 档（KTD3）。"""
        provider = _FakeProvider([_Turn(content="完成")])
        await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        assert provider.requests[0]["tier"] == "flash"

    async def test_tools_passed_on_react_turns(self, task: ModuleTask) -> None:
        provider = _FakeProvider([_Turn(content="完成")])
        await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        assert provider.requests[0]["tools"], "ReAct 轮次必须带工具 schema"


class TestCitationExtraction:
    async def test_real_paths_extracted(self, task: ModuleTask) -> None:
        provider = _FakeProvider(
            [_Turn(content="入口在 pkg/api.py，核心逻辑在 pkg/core.py。")]
        )
        result = await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        assert set(result["module_analyses"][0].cited_paths) == {"pkg/api.py", "pkg/core.py"}

    async def test_hallucinated_paths_dropped(self, task: ModuleTask) -> None:
        """R7 要求结论可追溯，而模型可能写出不存在的路径。"""
        provider = _FakeProvider([_Turn(content="逻辑在 pkg/imaginary.py 与 pkg/core.py。")])
        result = await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        cited = result["module_analyses"][0].cited_paths
        assert "pkg/core.py" in cited
        assert "pkg/imaginary.py" not in cited

    async def test_bare_filename_recognised(self, task: ModuleTask) -> None:
        """模型常写裸文件名。只做全路径匹配时引用提取会返回 0。

        实测教训：fastapi/_compat 模块的分析引用了三个文件，但写的是 `shared.py`
        这类裸名，全路径匹配一个都没认出——而引用数为 0 意味着 R7 的可追溯性对该模块
        完全没产出，下游校验会拒掉它的每一条结论。
        """
        provider = _FakeProvider(
            [_Turn(content="`core.py` 提供 helper，`api.py` 导入它。")]
        )
        result = await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        cited = set(result["module_analyses"][0].cited_paths)
        assert cited == {"pkg/core.py", "pkg/api.py"}

    async def test_bare_filename_substring_not_matched(self, task: ModuleTask) -> None:
        """`legacy_api.py` 不该让 `api.py` 命中——用词边界避免子串巧合。"""
        provider = _FakeProvider([_Turn(content="改动集中在 legacy_api.py 里。")])
        result = await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        assert "pkg/api.py" not in result["module_analyses"][0].cited_paths

    async def test_prompt_requires_full_paths(self, task: ModuleTask) -> None:
        """上游要求全路径，避免裸名传播到 synthesize 的 citations——校验器会把裸名
        判为「路径不在文件树中」并拒掉整条结论。
        """
        provider = _FakeProvider([_Turn(content="完成")])
        await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        system = provider.requests[0]["messages"][0]["content"]  # type: ignore[index]
        assert "仓库相对路径" in system
        assert "裸文件名" in system


class TestRoundLimit:
    async def test_exhausted_rounds_marked_partial_not_failed(self, task: ModuleTask) -> None:
        """已收集的工具结果足以产出部分分析，标记部分完成比丢弃更有价值。

        用互不重叠的查询耗轮次——重叠读取会被重叠检测提前拦下，测不到轮次上限。
        """
        turns: list[object] = [
            _Turn(tool_calls=[_ToolCall("find_definition", {"name": f"sym_{i}"})])
            for i in range(DEFAULT_MAX_ROUNDS)
        ]
        turns.append(_Turn(content="基于已读内容的部分分析"))
        provider = _FakeProvider(turns)

        result = await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        assert "module_analyses" in result
        analysis = result["module_analyses"][0]
        assert "轮次上限" in analysis.limitation
        assert analysis.summary

    async def test_repeated_calls_trigger_early_convergence(self, task: ModuleTask) -> None:
        """打转的循环不会因为多给几轮而好转，早停下来用已有信息产出分析更划算。"""
        same = {"name": "helper"}
        provider = _FakeProvider(
            [
                _Turn(tool_calls=[_ToolCall("find_definition", same)]),
                _Turn(tool_calls=[_ToolCall("find_definition", same)]),
                _Turn(tool_calls=[_ToolCall("find_definition", same)]),
                _Turn(content="提前收敛的分析"),
            ]
        )
        result = await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        analysis = result["module_analyses"][0]
        assert "重复或大幅重叠" in analysis.limitation
        assert analysis.rounds_used < DEFAULT_MAX_ROUNDS

    async def test_overlapping_ranges_treated_as_repeat(self, task: ModuleTask) -> None:
        """精确签名匹配挡不住包含关系的范围。

        实测教训：fastapi#1 依次读 applications.py 的 700-1100 与 900-1100，后者完全
        包含在前者里。签名不同所以旧检测放过，近似重复的内容堆进上下文导致输出崩溃。
        """
        provider = _FakeProvider(
            [
                _Turn(
                    tool_calls=[
                        _ToolCall("read_file", {"path": "pkg/core.py", "start_line": 1, "end_line": 7})
                    ]
                ),
                _Turn(
                    tool_calls=[
                        _ToolCall("read_file", {"path": "pkg/core.py", "start_line": 3, "end_line": 7})
                    ]
                ),
                _Turn(content="收敛"),
            ]
        )
        result = await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        assert "重复或大幅重叠" in result["module_analyses"][0].limitation

    async def test_disjoint_ranges_not_treated_as_repeat(self, task: ModuleTask) -> None:
        """连续往下读是正常行为，不该被当成打转。"""
        provider = _FakeProvider(
            [
                _Turn(
                    tool_calls=[
                        _ToolCall("read_file", {"path": "pkg/core.py", "start_line": 1, "end_line": 3})
                    ]
                ),
                _Turn(
                    tool_calls=[
                        _ToolCall("read_file", {"path": "pkg/core.py", "start_line": 4, "end_line": 7})
                    ]
                ),
                _Turn(content="正常完成的分析"),
            ]
        )
        result = await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        assert result["module_analyses"][0].limitation == ""

    async def test_cumulative_output_budget_converges(self, task: ModuleTask) -> None:
        """单次行数上限不约束累积量。实测密集代码累积到一定量后输出会退化。"""
        # 行长按真实代码取（约 80 字符）。测试数据用 `x = 1` 这种短行的话，1200 行
        # 也只有 20KB 左右，触不到预算——而真实代码的密集程度正是预算要防的。
        filler = "    result_value = compute_something(argument_one, argument_two)  # 说明"
        big = "\n".join(f"{filler}  # {i}" for i in range(4000))
        (Path(task["workdir"]) / "pkg" / "big.py").write_text(big, encoding="utf-8")

        turns: list[object] = [
            _Turn(
                tool_calls=[
                    _ToolCall(
                        "read_file",
                        {"path": "pkg/big.py", "start_line": 1 + i * 200, "end_line": 200 + i * 200},
                    )
                ]
            )
            for i in range(6)
        ]
        turns.append(_Turn(content="预算触顶后的收敛分析"))
        provider = _FakeProvider(turns)

        result = await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        analysis = result["module_analyses"][0]
        assert "累积达" in analysis.limitation
        assert "上限" in analysis.limitation

    async def test_forced_summary_omits_tools(self, task: ModuleTask) -> None:
        """收敛时留着 tools 参数模型会继续调工具，循环就停不下来。"""
        same = {"path": "pkg/core.py"}
        provider = _FakeProvider(
            [
                _Turn(tool_calls=[_ToolCall("read_file", same)]),
                _Turn(tool_calls=[_ToolCall("read_file", same)]),
                _Turn(tool_calls=[_ToolCall("read_file", same)]),
                _Turn(content="收敛"),
            ]
        )
        await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        assert provider.requests[-1]["tools"] is None


class TestFailureIsolation:
    async def test_call_failure_becomes_node_failure(self, task: ModuleTask) -> None:
        """实测确认节点抛异常会中断整图，所以失败必须转成数据（R10、AE4）。"""
        provider = _FakeProvider([RuntimeError("网关 503")])
        result = await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        assert "module_analyses" not in result
        failures = result["module_failures"]
        assert len(failures) == 1
        assert failures[0].scope == "pkg"
        assert "RuntimeError" in failures[0].error

    async def test_failure_carries_module_name_for_report(self, task: ModuleTask) -> None:
        """AE4 要求报告标注该模块缺失及原因，所以失败记录必须能定位到模块。"""
        provider = _FakeProvider([RuntimeError("失败")])
        result = await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        assert result["module_failures"][0].scope == "pkg"
        assert result["module_failures"][0].node == "module_agent"

    async def test_empty_answer_recorded_as_failure(self, task: ModuleTask) -> None:
        provider = _FakeProvider([_Turn(content="")])
        result = await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        assert "空内容" in result["module_failures"][0].error

    async def test_convergence_failure_recorded(self, task: ModuleTask) -> None:
        same = {"path": "pkg/core.py"}
        provider = _FakeProvider(
            [
                _Turn(tool_calls=[_ToolCall("read_file", same)]),
                _Turn(tool_calls=[_ToolCall("read_file", same)]),
                RuntimeError("收敛时失败"),
            ]
        )
        result = await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        assert "收敛总结失败" in result["module_failures"][0].error


class TestDegenerateOutput:
    """交付废文本比报告失败更糟：读者要花时间才发现那段文字没有信息。

    实测 flash 档在长上下文下产出过 `WriterWriterWriter…` 与 `(, (, (, …`。
    """

    async def test_repeated_substring_rejected(self, task: ModuleTask) -> None:
        garbage = "分析如下：" + "Writer" * 80
        provider = _FakeProvider([_Turn(content=garbage)])
        result = await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        assert "module_analyses" not in result
        assert "输出退化" in result["module_failures"][0].error

    async def test_low_diversity_rejected(self, task: ModuleTask) -> None:
        provider = _FakeProvider([_Turn(content="结果：" + "(, " * 150)])
        result = await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        assert "输出退化" in result["module_failures"][0].error

    async def test_normal_analysis_not_rejected(self, task: ModuleTask) -> None:
        """正常分析里也会有重复词，判据不能宽到误伤。"""
        summary = (
            "pkg/core.py 定义 helper 与 Service。helper 做数值变换，Service.run 调用它。\n"
            "pkg/api.py 从 pkg/core.py 导入 helper，在 handle 中使用。\n"
            "模块对外只暴露这两个符号，没有其他依赖。"
        )
        provider = _FakeProvider([_Turn(content=summary)])
        result = await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        assert "module_analyses" in result

    async def test_structured_list_not_rejected(self, task: ModuleTask) -> None:
        """结构化列表里重复的前缀是正常写法。

        只数出现次数会误杀：`- 文件：` 重复 10 次在 400 字符里只占 10%，
        而崩溃时重复内容会占到 60% 以上。判据必须是「重复主导尾部」。
        """
        summary = "模块文件清单：\n" + "\n".join(
            f"- 文件：pkg/module_{i}.py 负责第 {i} 类数据的转换与校验逻辑" for i in range(12)
        )
        provider = _FakeProvider([_Turn(content=summary)])
        result = await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        assert "module_analyses" in result, "结构化列表被误判为退化输出"

    async def test_short_output_not_judged_degenerate(self, task: ModuleTask) -> None:
        """短输出样本不足，不做退化判定——否则一句话的合法回答会被误杀。"""
        provider = _FakeProvider([_Turn(content="pkg 模块只有 pkg/core.py 一个实现文件。")])
        result = await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        assert "module_analyses" in result

    async def test_thinking_markers_stripped(self, task: ModuleTask) -> None:
        """实测正文里漏出过 `</think>` 与 `<｜DSML｜tool_cistrong>`，它们不该进报告。"""
        provider = _FakeProvider(
            [
                _Turn(
                    content="<think>先看核心文件</think>\npkg/core.py 定义了 helper。\n<｜DSML｜x"
                )
            ]
        )
        result = await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        summary = result["module_analyses"][0].summary
        assert "</think>" not in summary
        assert "<think>" not in summary
        assert "DSML" not in summary
        assert "pkg/core.py 定义了 helper" in summary


class TestToolErrorTolerance:
    async def test_bad_tool_name_does_not_break_loop(self, task: ModuleTask) -> None:
        """模型编造工具名时返回错误让它纠正，而非中断循环。"""
        provider = _FakeProvider(
            [
                _Turn(tool_calls=[_ToolCall("grep_repo", {"pattern": "x"})]),
                _Turn(content="改用别的方式完成了分析"),
            ]
        )
        result = await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        assert "module_analyses" in result
        tool_messages = [
            m for m in provider.requests[1]["messages"] if m.get("role") == "tool"  # type: ignore[union-attr]
        ]
        assert "未知工具" in tool_messages[0]["content"]

    async def test_malformed_arguments_tolerated(self, task: ModuleTask) -> None:
        call = _ToolCall("read_file", {})
        call.function.arguments = "{不是合法 JSON"  # type: ignore[attr-defined]
        provider = _FakeProvider([_Turn(tool_calls=[call]), _Turn(content="完成")])
        result = await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        assert "module_analyses" in result

    async def test_multiple_tool_calls_in_one_turn(self, task: ModuleTask) -> None:
        provider = _FakeProvider(
            [
                _Turn(
                    tool_calls=[
                        _ToolCall("read_file", {"path": "pkg/core.py"}, "a"),
                        _ToolCall("find_definition", {"name": "helper"}, "b"),
                    ]
                ),
                _Turn(content="完成"),
            ]
        )
        await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        tool_messages = [
            m for m in provider.requests[1]["messages"] if m.get("role") == "tool"  # type: ignore[union-attr]
        ]
        assert len(tool_messages) == 2


class TestShardScopedTools:
    async def test_find_definition_works_on_shard_slice(self, task: ModuleTask) -> None:
        """分片带模块级解析切片，所以本模块符号查得到——symbol_index 的名字列表不够，
        find_definition 需要 Symbol 对象才能给出行号。
        """
        provider = _FakeProvider(
            [
                _Turn(tool_calls=[_ToolCall("find_definition", {"name": "helper"})]),
                _Turn(content="完成"),
            ]
        )
        await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        tool_messages = [
            m for m in provider.requests[1]["messages"] if m.get("role") == "tool"  # type: ignore[union-attr]
        ]
        assert "pkg/core.py:1" in tool_messages[0]["content"]

    async def test_task_message_carries_planner_context(self, task: ModuleTask) -> None:
        """带上挑选理由与深度，分析才不会偏离 Planner 的判断。"""
        provider = _FakeProvider([_Turn(content="完成")])
        await analyze_module(task, make_settings(), provider)  # type: ignore[arg-type]
        user_message = provider.requests[0]["messages"][1]["content"]  # type: ignore[index]
        assert "核心模块" in user_message
        assert "深度分析" in user_message


class TestFanoutIntegration:
    """接线验证：单测覆盖单个 Agent，但「多份结果经 reducer 汇聚」是图结构问题。

    用真实的 analyze_module 接进图，而非 stub 节点——stub 验证不了真实节点的返回形状
    是否与 reducer 字段匹配。
    """

    def _graph_with_real_agent(self, settings, provider_factory):  # type: ignore[no-untyped-def]
        from backend.graph.builder import build_analysis_graph
        from backend.graph.stub_nodes import make_stub_nodes
        from dataclasses import replace

        stubs = make_stub_nodes(module_count=3)

        def real_module_agent(state):  # type: ignore[no-untyped-def]
            import asyncio

            module_name = state["module"].name
            return asyncio.run(
                analyze_module(state, settings, provider_factory(module_name))
            )

        return build_analysis_graph(replace(stubs, module_agent=real_module_agent))

    async def test_three_modules_aggregate_without_loss(self, tmp_path: Path) -> None:
        settings = make_settings()

        def factory(module_name: str) -> _FakeProvider:
            return _FakeProvider([_Turn(content=f"{module_name} 的分析")])

        graph = self._graph_with_real_agent(settings, factory)
        result = graph.invoke(
            {"repo_url": "u"}, {"configurable": {"thread_id": "fanout"}}
        )
        analyses = result["module_analyses"]
        assert len(analyses) == 3
        assert {a.module_name for a in analyses} == {
            "stub_module_0",
            "stub_module_1",
            "stub_module_2",
        }

    async def test_each_result_matches_its_own_module(self, tmp_path: Path) -> None:
        """汇聚不能串台：每份分析必须对应正确的模块。"""
        settings = make_settings()

        def factory(module_name: str) -> _FakeProvider:
            return _FakeProvider([_Turn(content=f"这是 {module_name} 独有的结论")])

        graph = self._graph_with_real_agent(settings, factory)
        result = graph.invoke({"repo_url": "u"}, {"configurable": {"thread_id": "match"}})
        for analysis in result["module_analyses"]:
            assert analysis.module_name in analysis.summary

    async def test_one_failure_others_complete(self, tmp_path: Path) -> None:
        """AE4：其余模块完成分析，报告产出并标注该模块缺失及原因。"""
        settings = make_settings()

        def factory(module_name: str) -> _FakeProvider:
            if module_name == "stub_module_1":
                return _FakeProvider([RuntimeError("这个模块失败了")])
            return _FakeProvider([_Turn(content=f"{module_name} 完成")])

        graph = self._graph_with_real_agent(settings, factory)
        result = graph.invoke({"repo_url": "u"}, {"configurable": {"thread_id": "partial"}})

        assert len(result["module_analyses"]) == 2
        assert {a.module_name for a in result["module_analyses"]} == {
            "stub_module_0",
            "stub_module_2",
        }
        failures = result["module_failures"]
        assert len(failures) == 1
        assert failures[0].scope == "stub_module_1"
        # 报告仍要产出——这是 AE4 的核心。
        assert result["report"]
