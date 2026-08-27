"""符号与 import 提取。

用真实源文件而非字符串片段——query 在完整文件上的行为与在片段上不同（顶层节点
类型、缩进上下文都会变）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.static_analysis.models import (
    ImportKind,
    ImportScope,
    ParsedFile,
    SymbolKind,
    UnparsedFile,
)
from backend.static_analysis.parser import parse_file, parse_repo

MAX_BYTES = 1_048_576

PY_SOURCE = '''\
import os
import os.path as osp
from pathlib import Path
from . import sibling
from ..pkg.deep import thing

CONST = 1


def top_level(a, b):
    return a + b


class Service:
    """docstring"""

    def method(self, x):
        return x

    @property
    def prop(self):
        return 1


def _private():
    pass
'''

TS_SOURCE = """\
import { readFile, writeFile } from './fs-helpers';
import type { Config } from '@/types/config';
import defaultExport from '../legacy';
import * as ns from './namespace';
export * from './barrel';

export const arrowFn = (x: number): number => x * 2;
const notAFunction = { a: 1 };

export function namedFn(y: string): void {}

export class Widget {
  private id: number;
  render(): void {}
  static create(): Widget {
    return new Widget();
  }
}

interface Props {
  title: string;
}

type Alias = string | number;
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "service.py").write_text(PY_SOURCE, encoding="utf-8")
    (root / "src" / "widget.ts").write_text(TS_SOURCE, encoding="utf-8")
    return root


def _parse(repo: Path, rel: str) -> ParsedFile:
    result = parse_file(repo, rel, MAX_BYTES)
    assert isinstance(result, ParsedFile), f"预期解析成功，实际：{result}"
    return result


class TestSymbols:
    def test_python_top_level_function(self, repo: Path) -> None:
        syms = _parse(repo, "src/service.py").symbols
        top = [s for s in syms if s.name == "top_level"]
        assert len(top) == 1
        assert top[0].kind is SymbolKind.FUNCTION
        assert top[0].parent is None

    def test_python_class(self, repo: Path) -> None:
        syms = _parse(repo, "src/service.py").symbols
        cls = [s for s in syms if s.name == "Service"]
        assert len(cls) == 1
        assert cls[0].kind is SymbolKind.CLASS

    def test_top_level_class_has_no_parent(self, repo: Path) -> None:
        """类名节点的父节点是 class_definition 自身，往上找类时须跳过它，
        否则顶层类的 qualified_name 变成自指的 `Service.Service`。
        """
        syms = _parse(repo, "src/service.py").symbols
        cls = next(s for s in syms if s.name == "Service")
        assert cls.parent is None
        assert cls.qualified_name == "Service"

    def test_nested_class_records_outer(self, repo: Path) -> None:
        """嵌套类的 parent 是外层类；类内方法的 parent 是直接所属类。"""
        (repo / "src" / "nested.py").write_text(
            "class Outer:\n"
            "    class Inner:\n"
            "        def deep(self):\n"
            "            pass\n",
            encoding="utf-8",
        )
        syms = _parse(repo, "src/nested.py").symbols
        by_name = {s.name: s for s in syms}
        assert by_name["Outer"].parent is None
        assert by_name["Inner"].parent == "Outer"
        assert by_name["deep"].qualified_name == "Inner.deep"

    def test_local_function_in_method_is_not_method(self, repo: Path) -> None:
        """闭包内的局部函数不是外层类的方法——走查遇到函数体即停。"""
        (repo / "src" / "closure.py").write_text(
            "class Holder:\n"
            "    def outer(self):\n"
            "        def local_helper():\n"
            "            pass\n"
            "        return local_helper\n",
            encoding="utf-8",
        )
        syms = _parse(repo, "src/closure.py").symbols
        helper = next(s for s in syms if s.name == "local_helper")
        assert helper.parent is None
        assert helper.kind is SymbolKind.FUNCTION

    def test_python_method_has_parent(self, repo: Path) -> None:
        """方法与函数的区分——qualified_name 是 U7 查定义工具的查找键。"""
        syms = _parse(repo, "src/service.py").symbols
        method = [s for s in syms if s.name == "method"]
        assert len(method) == 1
        assert method[0].kind is SymbolKind.METHOD
        assert method[0].parent == "Service"
        assert method[0].qualified_name == "Service.method"

    def test_line_numbers_are_one_based(self, repo: Path) -> None:
        syms = _parse(repo, "src/service.py").symbols
        top = next(s for s in syms if s.name == "top_level")
        # PY_SOURCE 里 `def top_level` 在第 10 行（1-based）
        assert top.start_line == 10
        assert top.end_line >= top.start_line

    def test_no_duplicate_symbols(self, repo: Path) -> None:
        syms = _parse(repo, "src/service.py").symbols
        keys = [(s.path, s.name, s.start_line) for s in syms]
        assert len(keys) == len(set(keys)), f"重复符号：{keys}"

    def test_typescript_function_and_class(self, repo: Path) -> None:
        syms = _parse(repo, "src/widget.ts").symbols
        names = {s.name for s in syms}
        assert "namedFn" in names
        assert "Widget" in names

    def test_typescript_method(self, repo: Path) -> None:
        syms = _parse(repo, "src/widget.ts").symbols
        render = [s for s in syms if s.name == "render"]
        assert len(render) == 1
        assert render[0].parent == "Widget"

    def test_typescript_arrow_function_captured(self, repo: Path) -> None:
        syms = _parse(repo, "src/widget.ts").symbols
        assert any(s.name == "arrowFn" for s in syms)

    def test_non_function_const_not_captured(self, repo: Path) -> None:
        """`const notAFunction = { a: 1 }` 不是符号定义。"""
        syms = _parse(repo, "src/widget.ts").symbols
        assert not any(s.name == "notAFunction" for s in syms)

    def test_typescript_interface_and_type_alias(self, repo: Path) -> None:
        syms = _parse(repo, "src/widget.ts").symbols
        kinds = {s.name: s.kind for s in syms}
        assert kinds.get("Props") is SymbolKind.INTERFACE
        assert kinds.get("Alias") is SymbolKind.TYPE_ALIAS


class TestImports:
    def test_python_plain_module(self, repo: Path) -> None:
        imports = _parse(repo, "src/service.py").imports
        plain = [i for i in imports if i.target == "os"]
        assert len(plain) == 1
        assert plain[0].kind is ImportKind.MODULE

    def test_python_aliased_module_keeps_full_target(self, repo: Path) -> None:
        imports = _parse(repo, "src/service.py").imports
        assert any(i.target == "os.path" for i in imports)

    def test_python_from_import_records_names(self, repo: Path) -> None:
        imports = _parse(repo, "src/service.py").imports
        frm = [i for i in imports if i.target == "pathlib"]
        assert len(frm) == 1
        assert frm[0].kind is ImportKind.FROM
        assert "Path" in frm[0].names

    def test_python_relative_import_preserves_dots(self, repo: Path) -> None:
        """点数决定 U4 归一化到哪个目录，丢了就无法解析。"""
        imports = _parse(repo, "src/service.py").imports
        single = [i for i in imports if i.target == "."]
        assert len(single) == 1, f"未找到 `from . import`，实际 targets：{[i.target for i in imports]}"

    def test_python_multi_level_relative_import(self, repo: Path) -> None:
        imports = _parse(repo, "src/service.py").imports
        assert any(i.target == "..pkg.deep" for i in imports)

    def test_typescript_named_import(self, repo: Path) -> None:
        imports = _parse(repo, "src/widget.ts").imports
        named = [i for i in imports if i.target == "./fs-helpers"]
        assert len(named) == 1
        assert named[0].kind is ImportKind.FROM
        assert set(named[0].names) == {"readFile", "writeFile"}

    def test_typescript_quotes_stripped(self, repo: Path) -> None:
        imports = _parse(repo, "src/widget.ts").imports
        assert all("'" not in i.target and '"' not in i.target for i in imports)

    def test_typescript_type_only_import_classified(self, repo: Path) -> None:
        imports = _parse(repo, "src/widget.ts").imports
        type_only = [i for i in imports if i.target == "@/types/config"]
        assert len(type_only) == 1
        assert type_only[0].kind is ImportKind.TYPE_ONLY

    def test_typescript_reexport_classified(self, repo: Path) -> None:
        imports = _parse(repo, "src/widget.ts").imports
        reexport = [i for i in imports if i.target == "./barrel"]
        assert len(reexport) == 1
        assert reexport[0].kind is ImportKind.REEXPORT

    def test_typescript_default_and_namespace_import(self, repo: Path) -> None:
        imports = _parse(repo, "src/widget.ts").imports
        targets = {i.target for i in imports}
        assert "../legacy" in targets
        assert "./namespace" in targets


SCOPED_SOURCE = '''\
from typing import TYPE_CHECKING

import top_level

if TYPE_CHECKING:
    from guarded import G

if TYPE_CHECKING:
    pass
else:
    from runtime_fallback import R


class Holder:
    from class_body import CB

    def method(self):
        import method_local

        return method_local


def deferred_user():
    from deferred import D

    return D
'''


class TestImportScope:
    """import 在导入期是否执行。环检测据此判断一条边算不算（见 ImportScope）。"""

    @pytest.fixture
    def scoped(self, tmp_path: Path) -> Path:
        root = tmp_path / "scoped"
        root.mkdir()
        (root / "m.py").write_text(SCOPED_SOURCE, encoding="utf-8")
        return root

    def _scope_of(self, scoped: Path, target: str) -> ImportScope:
        imports = _parse(scoped, "m.py").imports
        matched = [i for i in imports if i.target == target]
        assert len(matched) == 1, f"目标 {target} 未捕获或重复：{[i.target for i in imports]}"
        return matched[0].scope

    def test_top_level_import_is_module_scope(self, scoped: Path) -> None:
        assert self._scope_of(scoped, "top_level") is ImportScope.MODULE

    def test_function_body_import_is_deferred(self, scoped: Path) -> None:
        assert self._scope_of(scoped, "deferred") is ImportScope.DEFERRED

    def test_method_body_import_is_deferred(self, scoped: Path) -> None:
        """方法体与函数体同一判据——方法名下的 block 仍在 _FUNCTION_NODES 链上。"""
        assert self._scope_of(scoped, "method_local") is ImportScope.DEFERRED

    def test_type_checking_block_import_is_guarded(self, scoped: Path) -> None:
        assert self._scope_of(scoped, "guarded") is ImportScope.TYPE_GUARDED

    def test_class_body_import_is_module_scope(self, scoped: Path) -> None:
        """类体在 class 语句求值时就执行，与顶层无实质差别，故算导入期。"""
        assert self._scope_of(scoped, "class_body") is ImportScope.MODULE

    def test_else_branch_of_type_checking_is_module_scope(self, scoped: Path) -> None:
        """`else:` 分支是运行期真正走的那条路，不是「仅供类型检查器」。"""
        assert self._scope_of(scoped, "runtime_fallback") is ImportScope.MODULE

    def test_plain_if_block_import_is_module_scope(self, tmp_path: Path) -> None:
        """条件导入但条件与 TYPE_CHECKING 无关时仍算导入期——它在模块加载时会求值。"""
        root = tmp_path / "plainif"
        root.mkdir()
        (root / "m.py").write_text(
            "import sys\n\nif sys.version_info >= (3, 11):\n    from tomllib import loads\n",
            encoding="utf-8",
        )
        loads = [i for i in _parse(root, "m.py").imports if i.target == "tomllib"]
        assert len(loads) == 1
        assert loads[0].scope is ImportScope.MODULE

    def test_typing_qualified_type_checking_recognised(self, tmp_path: Path) -> None:
        """`if typing.TYPE_CHECKING:` 与裸名写法同样要认。"""
        root = tmp_path / "qualified"
        root.mkdir()
        (root / "m.py").write_text(
            "import typing\n\nif typing.TYPE_CHECKING:\n    from only_types import T\n",
            encoding="utf-8",
        )
        guarded = [i for i in _parse(root, "m.py").imports if i.target == "only_types"]
        assert len(guarded) == 1
        assert guarded[0].scope is ImportScope.TYPE_GUARDED

    def test_typescript_static_import_is_module_scope(self, repo: Path) -> None:
        """ESM 的静态 import 只能在顶层，所以 TS 侧恒为 MODULE。"""
        imports = _parse(repo, "src/widget.ts").imports
        assert imports
        assert all(i.scope is ImportScope.MODULE for i in imports)

    def test_at_import_time_only_true_for_module_scope(self) -> None:
        assert ImportScope.MODULE.at_import_time
        assert not ImportScope.DEFERRED.at_import_time
        assert not ImportScope.TYPE_GUARDED.at_import_time


class TestLanguageCoverage:
    """语言范围：Python 与 TypeScript 做符号级解析，其余只计入文件树。"""

    def test_tsx_component_is_symbol(self, repo: Path) -> None:
        """TSX 走 tsx grammar，组件定义应进符号表。"""
        (repo / "src" / "Panel.tsx").write_text(
            "export const Panel = ({ t }: { t: string }) => <div>{t}</div>;\n"
            "export function Header(): JSX.Element { return <h1 />; }\n",
            encoding="utf-8",
        )
        syms = _parse(repo, "src/Panel.tsx").symbols
        names = {s.name for s in syms}
        assert "Panel" in names
        assert "Header" in names

    def test_declaration_file_parses(self, repo: Path) -> None:
        """`.d.ts` 无实现体，不应因此报错。"""
        (repo / "src" / "api.d.ts").write_text(
            "export declare function fetchIt(id: string): Promise<void>;\n"
            "export interface Shape { kind: string; }\n",
            encoding="utf-8",
        )
        parsed = _parse(repo, "src/api.d.ts")
        assert any(s.name == "Shape" and s.kind is SymbolKind.INTERFACE for s in parsed.symbols)

    def test_javascript_marked_unsupported_not_parse_failure(self, repo: Path) -> None:
        """JS 不在符号级解析范围内，原因应是「扩展名」而非「解析失败」。

        typescript.scm 含 TS 专有节点，用 javascript grammar 构造 Query 会抛
        QueryError。若把 .js 映射到该 grammar，异常落进兜底分支后原因会写成
        「解析失败」，把语言范围问题误报成解析错误。
        """
        (repo / "src" / "legacy.js").write_text("function f() { return 1; }\n", encoding="utf-8")
        result = parse_file(repo, "src/legacy.js", MAX_BYTES)
        assert isinstance(result, UnparsedFile)
        assert "扩展名" in result.reason
        assert "解析失败" not in result.reason


class TestFailurePaths:
    def test_unsupported_suffix_reported_not_raised(self, repo: Path) -> None:
        (repo / "README.md").write_text("# hi", encoding="utf-8")
        result = parse_file(repo, "README.md", MAX_BYTES)
        assert isinstance(result, UnparsedFile)
        assert "扩展名" in result.reason

    def test_oversize_file_skipped(self, repo: Path) -> None:
        big = repo / "src" / "big.py"
        big.write_text("x = 1\n" * 5000, encoding="utf-8")
        result = parse_file(repo, "src/big.py", max_bytes=100)
        assert isinstance(result, UnparsedFile)
        assert "体积" in result.reason

    def test_missing_file_reported(self, repo: Path) -> None:
        result = parse_file(repo, "src/nope.py", MAX_BYTES)
        assert isinstance(result, UnparsedFile)

    def test_syntax_error_does_not_stop_batch(self, repo: Path) -> None:
        """tree-sitter 对语法错误是容错的——它产出含 ERROR 节点的树而非抛异常，
        所以这个文件应当解析成功，只是符号可能不全。整批不应中断。
        """
        (repo / "src" / "broken.py").write_text("def (((: pass\n", encoding="utf-8")
        outcome = parse_repo(repo, ["src/service.py", "src/broken.py"], MAX_BYTES)
        assert len(outcome.parsed) + len(outcome.unparsed) == 2
        assert any(f.path == "src/service.py" for f in outcome.parsed)

    def test_parse_repo_separates_parsed_and_unparsed(self, repo: Path) -> None:
        (repo / "notes.txt").write_text("hi", encoding="utf-8")
        outcome = parse_repo(
            repo, ["src/service.py", "src/widget.ts", "notes.txt"], MAX_BYTES
        )
        assert len(outcome.parsed) == 2
        assert len(outcome.unparsed) == 1
        assert outcome.symbol_count > 0
