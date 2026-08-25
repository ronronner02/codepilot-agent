"""依赖图构建：import 归一化、外部依赖、未解析缺口、环、降级。

用真实文件跑完整的 U3 -> U4 路径（parse_repo 再 build_graph），而非手搓 ImportRef。
理由同 test_parser.py：解析规则的缺口只在真实文件布局上暴露，手搓输入会把「query
捕获对了吗」和「路径解析对了吗」两件事混在一起。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.static_analysis.graph import build_graph, centrality
from backend.static_analysis.models import DependencyGraph, Granularity
from backend.static_analysis.parser import parse_repo

MAX_BYTES = 1_048_576
MAX_NODES = 5_000


def _write(root: Path, rel: str, body: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _graph(root: Path, **kwargs: object) -> DependencyGraph:
    files = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]
    outcome = parse_repo(root, files, MAX_BYTES)
    max_nodes = int(kwargs.pop("max_nodes", MAX_NODES))
    return build_graph(root, outcome.parsed, max_nodes=max_nodes, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def py_repo(tmp_path: Path) -> Path:
    """Python 包布局，覆盖绝对导入、两种相对导入、外部依赖、环。"""
    root = tmp_path / "pyrepo"
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/core.py", "from pkg.util import helper\nimport os\n")
    _write(root, "pkg/util.py", "import httpx\n\n\ndef helper():\n    pass\n")
    _write(root, "pkg/sub/__init__.py", "")
    _write(root, "pkg/sub/deep.py", "from ..util import helper\n")
    _write(root, "pkg/sub/sibling.py", "from . import deep\n")
    _write(root, "pkg/ring_a.py", "from pkg.ring_b import b\n\n\ndef a():\n    pass\n")
    _write(root, "pkg/ring_b.py", "from pkg.ring_a import a\n\n\ndef b():\n    pass\n")
    return root


@pytest.fixture
def ts_repo(tmp_path: Path) -> Path:
    """TS 布局，覆盖别名、barrel 再导出、目录导入、外部依赖。"""
    root = tmp_path / "tsrepo"
    _write(
        root,
        "tsconfig.json",
        json.dumps({"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}),
    )
    _write(root, "src/index.ts", "export * from './barrel';\n")
    _write(root, "src/barrel.ts", "export * from './widget';\n")
    _write(root, "src/widget.ts", "import { fmt } from '@/utils/format';\nimport React from 'react';\n")
    _write(root, "src/utils/index.ts", "export * from './format';\n")
    _write(root, "src/utils/format.ts", "export const fmt = (s: string) => s;\n")
    _write(root, "src/consumer.ts", "import { fmt } from './utils';\n")
    return root


class TestPythonResolution:
    def test_absolute_import_becomes_edge(self, py_repo: Path) -> None:
        graph = _graph(py_repo)
        assert "pkg/util.py" in graph.edges["pkg/core.py"]

    def test_relative_import_two_dots_normalized(self, py_repo: Path) -> None:
        """`from ..util import helper` 在 pkg/sub/deep.py 里应指向 pkg/util.py。"""
        graph = _graph(py_repo)
        assert graph.edges["pkg/sub/deep.py"] == frozenset({"pkg/util.py"})

    def test_relative_import_resolves_to_submodule_not_package_init(
        self, py_repo: Path
    ) -> None:
        """`from . import deep` 的真实依赖是兄弟模块，不是包的 __init__.py。

        全记到 __init__.py 上会虚高包入口的中心度，同时丢掉真实的模块间边。
        """
        graph = _graph(py_repo)
        assert graph.edges["pkg/sub/sibling.py"] == frozenset({"pkg/sub/deep.py"})

    def test_third_party_recorded_as_external_not_node(self, py_repo: Path) -> None:
        graph = _graph(py_repo)
        assert "os" in graph.external
        assert "httpx" in graph.external
        assert graph.external["httpx"] == frozenset({"pkg/util.py"})
        assert not any(n in ("os", "httpx") for n in graph.nodes)

    def test_no_self_loops(self, py_repo: Path) -> None:
        _write(py_repo, "pkg/selfref.py", "from pkg.selfref import thing\n")
        graph = _graph(py_repo)
        assert "pkg/selfref.py" not in graph.edges["pkg/selfref.py"]

    def test_src_layout_absolute_import(self, tmp_path: Path) -> None:
        """src 布局：`pkg.mod` 对应 src/pkg/mod.py，从仓库根拼不出来。"""
        root = tmp_path / "srclayout"
        _write(root, "src/pkg/__init__.py", "")
        _write(root, "src/pkg/mod.py", "VALUE = 1\n")
        _write(root, "src/pkg/user.py", "from pkg.mod import VALUE\n")
        graph = _graph(root)
        assert graph.edges["src/pkg/user.py"] == frozenset({"src/pkg/mod.py"})

    def test_missing_relative_import_recorded_as_unresolved(self, py_repo: Path) -> None:
        """指向仓库内却找不到的相对导入是解析缺口，须留痕而非静默丢弃。"""
        _write(py_repo, "pkg/broken.py", "from .nonexistent import thing\n")
        graph = _graph(py_repo)
        gaps = [u for u in graph.unresolved if u.path == "pkg/broken.py"]
        assert len(gaps) == 1
        assert gaps[0].target == ".nonexistent"
        assert "pkg/nonexistent" in gaps[0].reason


class TestTypescriptResolution:
    def test_path_alias_resolved_via_tsconfig(self, ts_repo: Path) -> None:
        graph = _graph(ts_repo)
        assert graph.edges["src/widget.ts"] == frozenset({"src/utils/format.ts"})

    def test_barrel_reexport_recorded(self, ts_repo: Path) -> None:
        graph = _graph(ts_repo)
        assert graph.edges["src/index.ts"] == frozenset({"src/barrel.ts"})
        assert graph.edges["src/barrel.ts"] == frozenset({"src/widget.ts"})

    def test_directory_import_resolves_to_index(self, ts_repo: Path) -> None:
        graph = _graph(ts_repo)
        assert graph.edges["src/consumer.ts"] == frozenset({"src/utils/index.ts"})

    def test_bare_module_is_external(self, ts_repo: Path) -> None:
        graph = _graph(ts_repo)
        assert graph.external["react"] == frozenset({"src/widget.ts"})

    def test_js_extension_maps_to_ts_source(self, tmp_path: Path) -> None:
        """ESM 要求 import 里写 `.js`，实际源文件是 `.ts`。"""
        root = tmp_path / "esm"
        _write(root, "src/target.ts", "export const x = 1;\n")
        _write(root, "src/caller.ts", "import { x } from './target.js';\n")
        graph = _graph(root)
        assert graph.edges["src/caller.ts"] == frozenset({"src/target.ts"})

    def test_nx_tsconfig_base_json_is_read(self, tmp_path: Path) -> None:
        """Nx monorepo 根目录只有 tsconfig.base.json，没有 tsconfig.json。

        实测教训：只读 tsconfig.json 时 ghostfolio（Nx）的 864 个文件只连出 725 条边，
        全部 `@ghostfolio/*` 内部导入被误判为第三方包——静默残缺，不报错。
        """
        root = tmp_path / "nx"
        _write(
            root,
            "tsconfig.base.json",
            json.dumps(
                {
                    "compilerOptions": {
                        "baseUrl": ".",
                        "paths": {"@org/common/*": ["./libs/common/src/*"]},
                    }
                }
            ),
        )
        _write(root, "libs/common/src/config.ts", "export const C = 1;\n")
        _write(root, "apps/api/src/main.ts", "import { C } from '@org/common/config';\n")
        graph = _graph(root)
        assert graph.edges["apps/api/src/main.ts"] == frozenset({"libs/common/src/config.ts"})

    def test_tsconfig_extends_chain_followed(self, tmp_path: Path) -> None:
        """别名常定义在被 extends 的 base 里，内层只覆盖部分选项。"""
        root = tmp_path / "extends"
        _write(
            root,
            "tsconfig.base.json",
            json.dumps({"compilerOptions": {"paths": {"@lib/*": ["./packages/lib/*"]}}}),
        )
        _write(
            root,
            "tsconfig.json",
            json.dumps({"extends": "./tsconfig.base.json", "compilerOptions": {"strict": True}}),
        )
        _write(root, "packages/lib/util.ts", "export const u = 1;\n")
        _write(root, "src/app.ts", "import { u } from '@lib/util';\n")
        graph = _graph(root)
        assert graph.edges["src/app.ts"] == frozenset({"packages/lib/util.ts"})

    def test_tsconfig_with_comments_and_trailing_commas(self, tmp_path: Path) -> None:
        """tsconfig 是 jsonc：允许注释与尾逗号，标准 json 解析器会报错。"""
        root = tmp_path / "jsonc"
        _write(
            root,
            "tsconfig.json",
            '{\n  // 项目别名\n  "compilerOptions": {\n'
            '    "paths": { "@a/*": ["./src/a/*"] },\n  },\n}\n',
        )
        _write(root, "src/a/thing.ts", "export const t = 1;\n")
        _write(root, "src/use.ts", "import { t } from '@a/thing';\n")
        graph = _graph(root)
        assert graph.edges["src/use.ts"] == frozenset({"src/a/thing.ts"})

    def test_unmatched_alias_recorded_as_unresolved(self, ts_repo: Path) -> None:
        _write(ts_repo, "src/bad.ts", "import { q } from '@/missing/thing';\n")
        graph = _graph(ts_repo)
        gaps = [u for u in graph.unresolved if u.path == "src/bad.ts"]
        assert len(gaps) == 1
        assert "别名" in gaps[0].reason

    def test_type_only_import_excluded_when_disabled(self, tmp_path: Path) -> None:
        """include_type_only=False 时应得纯运行时依赖图。"""
        root = tmp_path / "typeonly"
        _write(root, "src/types.ts", "export interface T { a: string }\n")
        _write(root, "src/use.ts", "import type { T } from './types';\n")
        assert _graph(root).edges["src/use.ts"] == frozenset({"src/types.ts"})
        assert _graph(root, include_type_only=False).edges["src/use.ts"] == frozenset()


class TestCycles:
    def test_two_node_cycle_recorded(self, py_repo: Path) -> None:
        graph = _graph(py_repo)
        rings = [c for c in graph.cycles if set(c) == {"pkg/ring_a.py", "pkg/ring_b.py"}]
        assert len(rings) == 1, f"环未记录或重复：{graph.cycles}"

    def test_longer_cycle_recorded_once(self, tmp_path: Path) -> None:
        """同一个环从不同起点进入 DFS 时不应产出多份表示。"""
        root = tmp_path / "ring3"
        _write(root, "a.py", "from b import x\n")
        _write(root, "b.py", "from c import x\n")
        _write(root, "c.py", "from a import x\n")
        graph = _graph(root)
        assert len(graph.cycles) == 1
        assert set(graph.cycles[0]) == {"a.py", "b.py", "c.py"}

    def test_deep_chain_does_not_exhaust_stack(self, tmp_path: Path) -> None:
        """深依赖链不应撞 Python 递归上限——病态结构不该压垮系统。"""
        root = tmp_path / "deep"
        depth = 1200
        for i in range(depth):
            body = f"from m{i + 1} import x\n" if i < depth - 1 else "x = 1\n"
            _write(root, f"m{i}.py", body)
        graph = _graph(root)
        assert graph.cycles == ()
        assert len(graph.nodes) == depth

    def test_acyclic_graph_has_no_cycles(self, ts_repo: Path) -> None:
        assert _graph(ts_repo).cycles == ()


class TestDegradation:
    def test_over_node_limit_degrades_to_directory(self, py_repo: Path) -> None:
        graph = _graph(py_repo, max_nodes=3)
        assert graph.granularity is Granularity.DIRECTORY
        assert graph.degraded_reason is not None
        assert "上限 3" in graph.degraded_reason
        assert set(graph.nodes) == {"pkg", "pkg/sub"}

    def test_within_limit_stays_file_level(self, py_repo: Path) -> None:
        graph = _graph(py_repo)
        assert graph.granularity is Granularity.FILE
        assert graph.degraded_reason is None


class TestCentrality:
    def test_widely_depended_file_ranks_highest(self, tmp_path: Path) -> None:
        root = tmp_path / "central"
        _write(root, "core.py", "VALUE = 1\n")
        for i in range(4):
            _write(root, f"user{i}.py", "from core import VALUE\n")
        _write(root, "leaf.py", "x = 1\n")
        scores = centrality(_graph(root))
        assert scores["core.py"] == max(scores.values())
        assert scores["core.py"] > scores["leaf.py"]

    def test_transitive_importance_beats_raw_indegree(self, tmp_path: Path) -> None:
        """入度相同时，被「重要文件」依赖的应排更高——这是用 PageRank 而非入度的理由。

        两个文件各被 1 个文件依赖：hub_dep 被 hub 依赖（hub 自身被 3 个文件依赖），
        fringe_dep 被无人依赖的 fringe 依赖。入度都是 1，但影响面差别很大。
        """
        root = tmp_path / "transitive"
        _write(root, "hub_dep.py", "A = 1\n")
        _write(root, "hub.py", "from hub_dep import A\n")
        for i in range(3):
            _write(root, f"client{i}.py", "from hub import A\n")
        _write(root, "fringe_dep.py", "B = 1\n")
        _write(root, "fringe.py", "from fringe_dep import B\n")

        graph = _graph(root)
        reverse = graph.reverse_edges()
        assert len(reverse["hub_dep.py"]) == len(reverse["fringe_dep.py"]) == 1

        scores = centrality(graph)
        assert scores["hub_dep.py"] > scores["fringe_dep.py"]

    def test_cycle_does_not_hang_centrality(self, py_repo: Path) -> None:
        scores = centrality(_graph(py_repo))
        assert len(scores) == len(_graph(py_repo).nodes)
        assert all(s > 0 for s in scores.values())

    def test_empty_graph_returns_empty(self) -> None:
        assert centrality(DependencyGraph()) == {}
