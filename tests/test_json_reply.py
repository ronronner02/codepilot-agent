"""LLM JSON 返回的提取与修复。

测试用的形态来自实测样本（网关上的 pro 档，DEA-Net 汇总，重复采样约 1/8 失败），不是
臆想的边界情况：多个代码块与内层裸引号是原先「掐头去尾」逻辑扛不住的两类真实返回。

`iter_json_values` 产出全部候选而不挑选，所以这里断言的是「候选里有想要的那份」，
取舍标准由调用方决定（见 test_synthesize 里对「取结论最多」的断言）。
"""

from __future__ import annotations

import json

from backend.providers.json_reply import escape_inner_quotes, iter_json_values


def _values(raw: str) -> list[object]:
    return list(iter_json_values(raw))


class TestNormalForms:
    def test_bare_json(self) -> None:
        assert _values('{"a": 1}')[0] == {"a": 1}

    def test_fenced_json(self) -> None:
        assert _values('```json\n{"a": 1}\n```')[0] == {"a": 1}

    def test_fence_without_language_tag(self) -> None:
        assert _values('```\n{"a": 1}\n```')[0] == {"a": 1}

    def test_array_at_top_level(self) -> None:
        assert _values("[1, 2, 3]")[0] == [1, 2, 3]

    def test_preamble_before_json(self) -> None:
        """现有逻辑只处理以 ``` 开头的返回，前言在裸 JSON 前面时它整段解析失败。"""
        assert {"a": 1} in _values('好的，报告如下：\n{"a": 1}')

    def test_trailing_prose_after_json(self) -> None:
        assert {"a": 1} in _values('{"a": 1}\n\n以上就是完整报告。')

    def test_unparseable_yields_nothing(self) -> None:
        assert _values("这不是 JSON") == []

    def test_empty_yields_nothing(self) -> None:
        assert _values("") == []


class TestMultipleBlocks:
    """实测形态一：模型先给残缺的一份，说明一句，再给完整的一份。

    整段交给 json.loads 报 `Extra data`。两份都要产出——只取第一个会选中残缺那份，
    而调用方需要看到两份才能挑。
    """

    def test_both_blocks_yielded(self) -> None:
        raw = (
            '```json\n{"summary": "只有总体印象"}\n```\n\n'
            "上面遗漏了必要字段，完整报告如下：\n\n"
            '```json\n{"summary": "完整", "module_breakdown": [{"text": "t"}]}\n```'
        )
        values = _values(raw)
        assert {"summary": "只有总体印象"} in values
        assert any(
            isinstance(v, dict) and v.get("module_breakdown") for v in values
        ), "完整的那份必须出现在候选里"

    def test_unterminated_final_fence_still_parsed(self) -> None:
        """输出被截断时末尾没有闭合标记。"""
        assert {"a": 1} in _values('```json\n{"a": 1}\n')


class TestInnerQuoteRepair:
    """实测形态二：文本里写了英文双引号且未转义，JSON 字符串提前闭合。"""

    def test_bare_inner_quotes_repaired(self) -> None:
        raw = '{"summary": "属于"网络定义 + 训练脚本"的结构"}'
        assert _values(raw)[0] == {"summary": '属于"网络定义 + 训练脚本"的结构'}

    def test_repair_preserves_structure_after_string(self) -> None:
        raw = '{"a": "说的是"这个"东西", "b": 2}'
        assert _values(raw)[0] == {"a": '说的是"这个"东西', "b": 2}

    def test_valid_json_untouched(self) -> None:
        """修复只在直接解析失败后才试，合法输入不该被改写。"""
        text = '{"a": "x", "b": [1, 2]}'
        assert escape_inner_quotes(text) == text

    def test_already_escaped_quotes_untouched(self) -> None:
        text = '{"a": "\\"x\\""}'
        assert json.loads(escape_inner_quotes(text)) == {"a": '"x"'}

    def test_repair_is_best_effort(self) -> None:
        """引号恰好停在逗号前时判不出来。

        这条锁住已知边界而非期望行为：判据是「引号后面跟不跟结构字符」，跟着逗号的
        裸引号与闭合引号无法区分。所以修复结果仍要过 json.loads，失败就换候选。
        """
        raw = '{"a": "他说"好", 然后走了"}'
        assert _values(raw) == []


class TestBracketCounting:
    def test_braces_inside_strings_ignored(self) -> None:
        """字符串里的括号不参与配对，否则文字里的 } 会让区段提前收尾。"""
        assert _values('前言\n{"a": "含 } 与 { 的文字"}')[0] == {"a": "含 } 与 { 的文字"}

    def test_nested_objects(self) -> None:
        raw = '说明\n{"a": {"b": {"c": [1, {"d": 2}]}}}'
        assert _values(raw)[0] == {"a": {"b": {"c": [1, {"d": 2}]}}}

    def test_duplicate_candidates_yielded_once(self) -> None:
        """代码块与括号配对会切出同一段文本，去重避免调用方重复处理。"""
        assert len(_values('```json\n{"a": 1}\n```')) == 1
