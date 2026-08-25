"""代码切块（KTD6：按 AST 边界，不按固定字符数）。

用真实文件跑：切块的正确性表现为「行号与源文件一致」，而构造假的语法树会把要验证的
东西替换成我的假设。

两条贯穿约束：
  块的行号必须与源文件对得上——检索结果要能落到具体行（R7 的可追溯基础）。
  密钥形态文件不产生任何切块（KTD17）——索引是落盘的，写进去就可能被检索出来。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.rag.chunker import (
    DEFAULT_MAX_CHUNK_LINES,
    Chunk,
    chunk_file,
    chunk_repo,
)

MAX_BYTES = 1_048_576


def _write(root: Path, rel: str, body: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _chunks(root: Path, rel: str, **kwargs: int) -> list[Chunk]:
    return chunk_file(root, rel, MAX_BYTES, **kwargs)  # type: ignore[arg-type]


def _by_symbol(chunks: list[Chunk]) -> dict[str, Chunk]:
    return {c.symbol: c for c in chunks if c.symbol}


class TestFunctionChunking:
    def test_python_function_is_own_chunk(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "m.py",
            "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n",
        )
        chunks = _chunks(tmp_path, "m.py")
        symbols = _by_symbol(chunks)
        assert "alpha" in symbols
        assert "beta" in symbols
        assert symbols["alpha"].kind == "function"

    def test_line_numbers_match_source(self, tmp_path: Path) -> None:
        """行号对不上就无法引用到具体位置，检索结果的可追溯性随之失效。"""
        _write(
            tmp_path,
            "m.py",
            "# 头部注释\n\ndef target():\n    x = 1\n    return x\n",
        )
        target = _by_symbol(_chunks(tmp_path, "m.py"))["target"]
        assert target.start_line == 3
        assert target.end_line == 5
        assert target.content.splitlines()[0].strip() == "def target():"

    def test_decorator_included_in_range(self, tmp_path: Path) -> None:
        """装饰器承载语义（路由注册、属性），切掉它块就不完整。"""
        _write(
            tmp_path,
            "m.py",
            "@app.route('/x')\ndef handler():\n    return 'ok'\n",
        )
        handler = _by_symbol(_chunks(tmp_path, "m.py"))["handler"]
        assert handler.start_line == 1
        assert "@app.route" in handler.content

    def test_typescript_function_chunked(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "m.ts",
            "export function compute(a: number): number {\n  return a * 2;\n}\n",
        )
        assert "compute" in _by_symbol(_chunks(tmp_path, "m.ts"))

    def test_react_component_chunked(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "Panel.tsx",
            "export const Panel = ({ t }: { t: string }) => {\n"
            "  const [v, setV] = useState(0);\n"
            "  return <div>{t}</div>;\n"
            "};\n",
        )
        symbols = _by_symbol(_chunks(tmp_path, "Panel.tsx"))
        assert "Panel" in symbols
        assert "useState" in symbols["Panel"].content


class TestClassChunking:
    def test_methods_are_separate_chunks_with_parent(self, tmp_path: Path) -> None:
        """方法各自成块：500 行的类若是一块，检索会命中整个类，相似度被无关内容稀释。"""
        _write(
            tmp_path,
            "svc.py",
            "class Service:\n"
            "    timeout = 30\n"
            "\n"
            "    def start(self):\n"
            "        return 1\n"
            "\n"
            "    def stop(self):\n"
            "        return 2\n",
        )
        chunks = _chunks(tmp_path, "svc.py")
        symbols = _by_symbol(chunks)
        assert "Service.start" in symbols
        assert "Service.stop" in symbols
        assert symbols["Service.start"].parent == "Service"
        assert symbols["Service.start"].kind == "method"

    def test_class_header_kept_as_chunk(self, tmp_path: Path) -> None:
        """类属性与装饰器承载「这个类是什么」，丢掉会让「有哪些数据结构」查不到落点。"""
        _write(
            tmp_path,
            "models.py",
            "@dataclass\n"
            "class Config:\n"
            "    host: str\n"
            "    port: int\n"
            "\n"
            "    def url(self):\n"
            "        return f'{self.host}:{self.port}'\n",
        )
        chunks = _chunks(tmp_path, "models.py")
        headers = [c for c in chunks if c.kind == "class_header"]
        assert len(headers) == 1
        assert "@dataclass" in headers[0].content
        assert "host: str" in headers[0].content

    def test_single_line_class_produces_no_header(self, tmp_path: Path) -> None:
        """只有声明行、没有类级属性——没有独立检索价值。"""
        _write(
            tmp_path,
            "m.py",
            "class Empty:\n    def go(self):\n        return 1\n",
        )
        chunks = _chunks(tmp_path, "m.py")
        assert not [c for c in chunks if c.kind == "class_header"]

    def test_class_property_arrow_function_chunked(self, tmp_path: Path) -> None:
        """`handle = () => {}` 是 React 类组件的常见形态，节点是 public_field_definition
        而非 method_definition，漏掉它这类处理函数就不进索引。
        """
        _write(
            tmp_path,
            "c.tsx",
            "export class Form extends Component {\n"
            "  state = { v: 0 };\n"
            "\n"
            "  handleSubmit = (e: Event) => {\n"
            "    e.preventDefault();\n"
            "  };\n"
            "}\n",
        )
        symbols = _by_symbol(_chunks(tmp_path, "c.tsx"))
        assert "Form.handleSubmit" in symbols

    def test_typescript_class_methods_chunked(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "w.ts",
            "export class Widget {\n"
            "  private id = 1;\n"
            "\n"
            "  render(): void {}\n"
            "\n"
            "  destroy(): void {}\n"
            "}\n",
        )
        symbols = _by_symbol(_chunks(tmp_path, "w.ts"))
        assert "Widget.render" in symbols
        assert "Widget.destroy" in symbols


class TestLongFunctionSplitting:
    def test_long_function_split_at_statement_boundary(self, tmp_path: Path) -> None:
        """切分点必须在语句边界，不在字符中间——半个语句对回答代码问题没有价值。"""
        body = "\n".join(f"    step_{i} = compute({i})" for i in range(40))
        _write(tmp_path, "big.py", f"def big():\n{body}\n    return step_0\n")

        chunks = _chunks(tmp_path, "big.py", max_chunk_lines=15, overlap_lines=3)
        parts = [c for c in chunks if c.kind == "function_part"]
        assert len(parts) > 1, "超长函数应被二次切分"
        # 每块的首行应是完整语句，不是语句中间。
        for part in parts:
            first = part.content.splitlines()[0].strip()
            assert first.startswith(("def big", "step_", "return")), f"切在语句中间：{first!r}"

    def test_split_parts_are_indexed(self, tmp_path: Path) -> None:
        body = "\n".join(f"    step_{i} = {i}" for i in range(40))
        _write(tmp_path, "big.py", f"def big():\n{body}\n")
        parts = [c for c in _chunks(tmp_path, "big.py", max_chunk_lines=12) if c.kind == "function_part"]
        assert [p.part_index for p in parts] == list(range(1, len(parts) + 1))

    def test_split_parts_overlap(self, tmp_path: Path) -> None:
        """切分点两侧的语句常有数据依赖，不带重叠时后半块会以来历不明的变量开头。"""
        body = "\n".join(f"    step_{i} = step_{i-1} + 1" if i else "    step_0 = 1" for i in range(40))
        _write(tmp_path, "big.py", f"def big():\n{body}\n")
        parts = [c for c in _chunks(tmp_path, "big.py", max_chunk_lines=15, overlap_lines=5) if c.kind == "function_part"]
        assert len(parts) >= 2
        assert parts[1].start_line < parts[0].end_line, "后一块应与前一块重叠"

    def test_short_function_not_split(self, tmp_path: Path) -> None:
        _write(tmp_path, "m.py", "def small():\n    return 1\n")
        chunks = _chunks(tmp_path, "m.py")
        assert not [c for c in chunks if c.kind == "function_part"]
        assert _by_symbol(chunks)["small"].part_index == 0

    def test_split_covers_whole_function(self, tmp_path: Path) -> None:
        """切分不能丢内容：末尾语句落在最后一块里。"""
        body = "\n".join(f"    step_{i} = {i}" for i in range(30))
        _write(tmp_path, "big.py", f"def big():\n{body}\n    return 'tail-marker'\n")
        parts = [c for c in _chunks(tmp_path, "big.py", max_chunk_lines=12) if c.kind == "function_part"]
        assert any("tail-marker" in p.content for p in parts)


class TestSecretExclusion:
    def test_env_file_produces_no_chunks(self, tmp_path: Path) -> None:
        """KTD17：索引是落盘的，把密钥写进去会让问答链路检索出来。"""
        _write(tmp_path, ".env", "DB_PASSWORD=xQ7fL2mZ9pR4tK8wB3nH\n")
        assert _chunks(tmp_path, ".env") == []

    def test_key_and_pem_produce_no_chunks(self, tmp_path: Path) -> None:
        for name in ("server.pem", "id.key", "store.p12"):
            _write(tmp_path, name, "-----BEGIN PRIVATE KEY-----\nMIIE\n")
            assert _chunks(tmp_path, name) == [], name

    def test_ordinary_source_still_chunked(self, tmp_path: Path) -> None:
        _write(tmp_path, "config.py", "SECRET_NAME = 'x'\n\n\ndef load():\n    return 1\n")
        assert _chunks(tmp_path, "config.py")

    def test_repo_level_excludes_secret_files(self, tmp_path: Path) -> None:
        _write(tmp_path, ".env", "API_KEY=xQ7fL2mZ9pR4tK8wB3nH\n")
        _write(tmp_path, "app.py", "def go():\n    return 1\n")
        chunks = chunk_repo(tmp_path, [".env", "app.py"], MAX_BYTES)
        assert all(c.path != ".env" for c in chunks)
        assert any(c.path == "app.py" for c in chunks)


class TestModuleLevelChunks:
    def test_imports_and_constants_chunked(self, tmp_path: Path) -> None:
        """import 段与模块常量常是查询落点（「用了什么库」、「配置项在哪」）。

        只切定义会让这些内容完全不可检索。
        """
        _write(
            tmp_path,
            "m.py",
            "import os\nimport httpx\n\nTIMEOUT = 30\nRETRIES = 3\n\n\ndef go():\n    return 1\n",
        )
        chunks = _chunks(tmp_path, "m.py")
        module_chunks = [c for c in chunks if c.kind == "module"]
        assert module_chunks
        joined = "\n".join(c.content for c in module_chunks)
        assert "import httpx" in joined
        assert "TIMEOUT = 30" in joined

    def test_no_duplicate_coverage(self, tmp_path: Path) -> None:
        """同一行不该同时落在定义块与模块块里——重复内容会在检索结果里占两个位置。"""
        _write(
            tmp_path,
            "m.py",
            "import os\n\n\ndef go():\n    return os.getcwd()\n",
        )
        chunks = _chunks(tmp_path, "m.py")
        module_lines = {
            line
            for c in chunks
            if c.kind == "module"
            for line in range(c.start_line, c.end_line + 1)
        }
        definition_lines = {
            line
            for c in chunks
            if c.kind != "module"
            for line in range(c.start_line, c.end_line + 1)
        }
        assert not (module_lines & definition_lines)


class TestChunkContract:
    def test_paths_are_repo_relative(self, tmp_path: Path) -> None:
        """绝对路径在容器内外不一致，也会泄露主机目录结构。"""
        _write(tmp_path, "pkg/m.py", "def go():\n    return 1\n")
        chunks = _chunks(tmp_path, "pkg/m.py")
        assert chunks
        for chunk in chunks:
            assert chunk.path == "pkg/m.py"
            assert not Path(chunk.path).is_absolute()

    def test_identifier_is_unique_per_chunk(self, tmp_path: Path) -> None:
        """同名符号在文件里可能出现多次（条件定义、重载），只用符号名会撞键。"""
        body = "\n".join(f"    s{i} = {i}" for i in range(40))
        _write(tmp_path, "m.py", f"def go():\n{body}\n")
        chunks = _chunks(tmp_path, "m.py", max_chunk_lines=12)
        ids = [c.identifier() for c in chunks]
        assert len(ids) == len(set(ids))

    def test_content_matches_line_range(self, tmp_path: Path) -> None:
        source = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
        _write(tmp_path, "m.py", source)
        lines = source.splitlines()
        for chunk in _chunks(tmp_path, "m.py"):
            expected = "\n".join(lines[chunk.start_line - 1 : chunk.end_line])
            assert chunk.content == expected

    def test_unparseable_file_yields_no_chunks(self, tmp_path: Path) -> None:
        _write(tmp_path, "README.md", "# 标题\n\n正文\n")
        assert _chunks(tmp_path, "README.md") == []

    def test_missing_file_yields_no_chunks(self, tmp_path: Path) -> None:
        assert _chunks(tmp_path, "nope.py") == []

    def test_escaping_path_yields_no_chunks(self, tmp_path: Path) -> None:
        """路径校验由 parse_tree 承担（KTD14 的单一实现），这里确认它生效。"""
        assert _chunks(tmp_path, "../outside.py") == []

    def test_empty_file_yields_no_chunks(self, tmp_path: Path) -> None:
        _write(tmp_path, "empty.py", "")
        assert _chunks(tmp_path, "empty.py") == []

    def test_default_max_lines_is_reasonable(self) -> None:
        """默认上限过小会把普通函数切碎，过大会让块失去聚焦。"""
        assert 60 <= DEFAULT_MAX_CHUNK_LINES <= 200

    def test_order_is_deterministic(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "m.py",
            "import os\n\n\nclass A:\n    x = 1\n\n    def m(self):\n        return 1\n\n\ndef top():\n    return 2\n",
        )
        first = _chunks(tmp_path, "m.py")
        second = _chunks(tmp_path, "m.py")
        assert [c.identifier() for c in first] == [c.identifier() for c in second]
