"""错误处理缺陷的候选点定位。

这一层只做确定性定位，不下结论——所以测试断言的是「定位到没有」与「置信度分级对
不对」，不断言「这是不是真问题」（那由判断层负责，且需要 LLM）。

不误报的场景与命中的场景同等重要：Reviewer 的标准是求准不求全，一个把正常代码全
报出来的检测器比不检测更糟——读者会因此不再相信任何一条发现。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.review.error_handling import find_error_handling_candidates
from backend.review.models import Confidence, FindingCategory
from backend.static_analysis.parser import ParsedTree, parse_tree

MAX_BYTES = 1_048_576


def _candidates(tmp_path: Path, rel: str, body: str) -> list:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    parsed = parse_tree(tmp_path, rel, MAX_BYTES)
    assert isinstance(parsed, ParsedTree), f"预期建树成功，实际：{parsed}"
    return find_error_handling_candidates(parsed)


def _kinds(candidates: list) -> set[str]:
    return {c.kind for c in candidates}


class TestBareExcept:
    def test_bare_except_located(self, tmp_path: Path) -> None:
        found = _candidates(
            tmp_path,
            "m.py",
            "def go():\n    try:\n        risky()\n    except:\n        log()\n",
        )
        bare = [c for c in found if c.kind == "bare_except"]
        assert len(bare) == 1
        assert bare[0].line == 4, "行号应落在 except 那一行"
        assert bare[0].confidence is Confidence.CERTAIN

    def test_typed_except_not_flagged(self, tmp_path: Path) -> None:
        found = _candidates(
            tmp_path,
            "m.py",
            "def go():\n    try:\n        risky()\n    except ValueError:\n        log()\n",
        )
        assert "bare_except" not in _kinds(found)

    def test_except_with_alias_not_flagged(self, tmp_path: Path) -> None:
        """`except ValueError as exc:` 的 value 是 as_pattern，仍算已指定类型。"""
        found = _candidates(
            tmp_path,
            "m.py",
            "def go():\n    try:\n        risky()\n    except ValueError as exc:\n        log(exc)\n",
        )
        assert "bare_except" not in _kinds(found)

    def test_evidence_explains_the_risk(self, tmp_path: Path) -> None:
        found = _candidates(
            tmp_path, "m.py", "try:\n    risky()\nexcept:\n    log()\n"
        )
        evidence = next(c for c in found if c.kind == "bare_except").detector_evidence
        assert "KeyboardInterrupt" in evidence, "依据应说明裸 except 的具体危害"

    def test_typescript_catch_without_type_not_flagged(self, tmp_path: Path) -> None:
        """TS 的 catch 本就不指定异常类型，那是语言设计而非缺陷。"""
        found = _candidates(
            tmp_path, "m.ts", "function go() {\n  try { risky(); } catch (e) { log(e); }\n}\n"
        )
        assert "bare_except" not in _kinds(found)


class TestSwallowedExceptions:
    def test_except_pass_located(self, tmp_path: Path) -> None:
        found = _candidates(
            tmp_path,
            "m.py",
            "def go():\n    try:\n        risky()\n    except Exception:\n        pass\n",
        )
        swallowed = [c for c in found if c.kind == "swallowed_exception"]
        assert len(swallowed) == 1
        assert swallowed[0].confidence is Confidence.CERTAIN

    def test_except_ellipsis_located(self, tmp_path: Path) -> None:
        found = _candidates(
            tmp_path,
            "m.py",
            "def go():\n    try:\n        risky()\n    except OSError:\n        ...\n",
        )
        assert "swallowed_exception" in _kinds(found)

    def test_except_with_logging_not_flagged(self, tmp_path: Path) -> None:
        found = _candidates(
            tmp_path,
            "m.py",
            "def go():\n    try:\n        risky()\n    except OSError as exc:\n        log(exc)\n",
        )
        assert "swallowed_exception" not in _kinds(found)

    def test_except_with_reraise_not_flagged(self, tmp_path: Path) -> None:
        """`except X: raise CustomError` 是异常转换，不是吞掉。"""
        found = _candidates(
            tmp_path,
            "m.py",
            "def go():\n    try:\n        risky()\n    except OSError as exc:\n"
            "        raise RuntimeError('x') from exc\n",
        )
        assert "swallowed_exception" not in _kinds(found)

    def test_typescript_empty_catch_located(self, tmp_path: Path) -> None:
        found = _candidates(
            tmp_path, "m.ts", "function go() {\n  try { risky(); } catch (e) {}\n}\n"
        )
        assert "swallowed_exception" in _kinds(found)

    def test_typescript_catch_without_binding_located(self, tmp_path: Path) -> None:
        """`catch {}` 无 parameter 字段，同样是空处理。"""
        found = _candidates(tmp_path, "m.ts", "function go() {\n  try { risky(); } catch {}\n}\n")
        assert "swallowed_exception" in _kinds(found)

    def test_typescript_catch_with_handling_not_flagged(self, tmp_path: Path) -> None:
        found = _candidates(
            tmp_path,
            "m.ts",
            "function go() {\n  try { risky(); } catch (e) { console.error(e); }\n}\n",
        )
        assert "swallowed_exception" not in _kinds(found)


class TestUnprotectedIO:
    def test_io_call_outside_try_located(self, tmp_path: Path) -> None:
        found = _candidates(tmp_path, "m.py", "def go():\n    data = open('a.txt').read()\n")
        io = [c for c in found if c.kind == "unprotected_io"]
        assert io
        assert all(c.confidence is Confidence.CONTEXTUAL for c in io)

    def test_io_call_inside_try_not_flagged(self, tmp_path: Path) -> None:
        found = _candidates(
            tmp_path,
            "m.py",
            "def go():\n    try:\n        data = open('a.txt').read()\n"
            "    except OSError as exc:\n        log(exc)\n        return None\n",
        )
        assert "unprotected_io" not in _kinds(found)

    def test_io_call_inside_except_block_is_still_flagged(self, tmp_path: Path) -> None:
        """写在 except 块里的 IO 调用并未被保护——它自己就可能抛异常。

        只看「祖先链上有 try_statement」会把这种情况误判为已处理。
        """
        found = _candidates(
            tmp_path,
            "m.py",
            "def go():\n    try:\n        risky()\n    except OSError:\n"
            "        fallback = open('b.txt').read()\n        return fallback\n",
        )
        assert "unprotected_io" in _kinds(found)

    def test_evidence_discloses_that_propagation_may_be_intentional(
        self, tmp_path: Path
    ) -> None:
        """CONTEXTUAL 类的依据必须说明「这可能是正常设计」，否则判断层会倾向全部报出。"""
        found = _candidates(tmp_path, "m.py", "def go():\n    return open('a.txt').read()\n")
        evidence = next(c for c in found if c.kind == "unprotected_io").detector_evidence
        assert "调用方" in evidence

    def test_ordinary_call_not_flagged(self, tmp_path: Path) -> None:
        """只查固定名单内的调用。全部调用都当候选会让候选数与代码量同阶。"""
        found = _candidates(tmp_path, "m.py", "def go():\n    return compute(1, 2)\n")
        assert "unprotected_io" not in _kinds(found)

    def test_qualified_io_call_matched(self, tmp_path: Path) -> None:
        """`requests.get(...)` 的限定名可见，能确定它做网络 IO。"""
        found = _candidates(tmp_path, "m.py", "def go():\n    return requests.get(url)\n")
        assert "unprotected_io" in _kinds(found)

    def test_same_method_name_on_other_object_not_flagged(self, tmp_path: Path) -> None:
        """方法名本身说明不了它做不做 IO——`d.get(k)` 与 `requests.get(url)` 同名。

        实测教训：只按方法名匹配时 fastapi 上产出 3593 个候选（每文件 3.2 个），
        `client.get`、`dict.get` 全被命中。那个量级既是泛泛之谈，也让判断层成本失控。
        """
        found = _candidates(
            tmp_path,
            "m.py",
            "def go(d, client):\n"
            "    a = d.get('k')\n"
            "    b = client.get('/path')\n"
            "    c = items.read()\n"
            "    return a, b, c\n",
        )
        assert "unprotected_io" not in _kinds(found)

    def test_unqualified_builtin_still_matched(self, tmp_path: Path) -> None:
        """`open()` 无需限定——这个名字本身唯一指向 IO。"""
        found = _candidates(tmp_path, "m.py", "def go():\n    return open('a.txt')\n")
        assert "unprotected_io" in _kinds(found)


class TestUnclosedResources:
    def test_open_assigned_outside_with_located(self, tmp_path: Path) -> None:
        found = _candidates(tmp_path, "m.py", "def go():\n    f = open('a.txt')\n    return f\n")
        unclosed = [c for c in found if c.kind == "unclosed_resource"]
        assert len(unclosed) == 1
        assert unclosed[0].confidence is Confidence.CONTEXTUAL

    def test_with_statement_not_flagged(self, tmp_path: Path) -> None:
        found = _candidates(
            tmp_path, "m.py", "def go():\n    with open('a.txt') as f:\n        return f.read()\n"
        )
        assert "unclosed_resource" not in _kinds(found)

    def test_typescript_not_checked(self, tmp_path: Path) -> None:
        """TS 的文件句柄由 GC 与 Promise 管理，没有等价的确定判据。"""
        found = _candidates(tmp_path, "m.ts", "function go() {\n  const f = open('a.txt');\n}\n")
        assert "unclosed_resource" not in _kinds(found)


class TestCandidateContract:
    def test_candidate_carries_context_for_judgment(self, tmp_path: Path) -> None:
        """判断层要看代码才能取舍，定位时顺手取出上下文可省掉一轮工具调用。"""
        found = _candidates(
            tmp_path,
            "pkg/m.py",
            "def outer():\n    try:\n        risky()\n    except:\n        pass\n",
        )
        candidate = found[0]
        assert candidate.category is FindingCategory.ERROR_HANDLING
        assert candidate.path == "pkg/m.py"
        assert candidate.snippet
        assert "except" in candidate.snippet
        assert candidate.enclosing == "outer"

    def test_module_level_has_empty_enclosing(self, tmp_path: Path) -> None:
        found = _candidates(tmp_path, "m.py", "try:\n    risky()\nexcept:\n    pass\n")
        assert found[0].enclosing == ""

    def test_both_detectors_may_hit_same_location(self, tmp_path: Path) -> None:
        """裸 except 且吞异常是两个不同问题，判断层需分别取舍。"""
        found = _candidates(tmp_path, "m.py", "try:\n    risky()\nexcept:\n    pass\n")
        assert {"bare_except", "swallowed_exception"} <= _kinds(found)

    def test_order_is_deterministic(self, tmp_path: Path) -> None:
        body = (
            "def a():\n    try:\n        risky()\n    except:\n        pass\n\n"
            "def b():\n    f = open('x')\n    return f\n"
        )
        first = _candidates(tmp_path, "m.py", body)
        second = _candidates(tmp_path, "m.py", body)
        assert [(c.line, c.kind) for c in first] == [(c.line, c.kind) for c in second]

    def test_syntax_error_file_does_not_raise(self, tmp_path: Path) -> None:
        """tree-sitter 对语法错误容错，产出含 ERROR 节点的树。检测不应因此崩溃。"""
        found = _candidates(tmp_path, "m.py", "def broken(((:\n    except:\n")
        assert isinstance(found, list)
