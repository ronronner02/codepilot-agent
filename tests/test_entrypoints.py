"""入口点识别：三类证据的强度分级与去重。

断言的重点不只是「找到了入口」，还有「给出的依据是什么」——报告里说某文件是入口
必须能落到可核验的依据上（R7），而 manifest 声明与图上零入度的可信度差别很大。
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.static_analysis.entrypoints import find_entrypoints
from backend.static_analysis.models import DependencyGraph, EntryKind
from tests.support import junction_or_skip as _junction_or_skip


def _write(root: Path, rel: str, body: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _graph(edges: dict[str, list[str]]) -> DependencyGraph:
    nodes = sorted({*edges, *(t for targets in edges.values() for t in targets)})
    return DependencyGraph(
        nodes=tuple(nodes), edges={n: frozenset(edges.get(n, ())) for n in nodes}
    )


def _by_path(root: Path, graph: DependencyGraph) -> dict[str, tuple[EntryKind, str]]:
    return {e.path: (e.kind, e.evidence) for e in find_entrypoints(root, graph)}


class TestManifest:
    def test_pyproject_scripts_detected(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "pyproject.toml",
            '[project]\nname = "demo"\n[project.scripts]\ndemo-cli = "demo.cli:main"\n',
        )
        _write(tmp_path, "demo/cli.py", "def main():\n    pass\n")
        found = _by_path(tmp_path, _graph({"demo/cli.py": []}))
        kind, evidence = found["demo/cli.py"]
        assert kind is EntryKind.MANIFEST
        assert "project.scripts" in evidence
        assert "demo.cli:main" in evidence

    def test_pyproject_script_pointing_to_package_init(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "pyproject.toml",
            '[project]\nname = "d"\n[project.scripts]\nrun = "pkg:main"\n',
        )
        _write(tmp_path, "pkg/__init__.py", "def main():\n    pass\n")
        assert "pkg/__init__.py" in _by_path(tmp_path, _graph({"pkg/__init__.py": []}))

    def test_package_json_main_detected(self, tmp_path: Path) -> None:
        _write(tmp_path, "package.json", json.dumps({"main": "src/index.ts"}))
        _write(tmp_path, "src/index.ts", "export const x = 1;\n")
        kind, evidence = _by_path(tmp_path, _graph({"src/index.ts": []}))["src/index.ts"]
        assert kind is EntryKind.MANIFEST
        assert "package.json main" in evidence

    def test_package_json_dist_path_maps_to_source(self, tmp_path: Path) -> None:
        """声明的入口常指向构建产物，依赖图的节点是源文件。"""
        _write(tmp_path, "package.json", json.dumps({"main": "dist/index.js"}))
        _write(tmp_path, "src/index.ts", "export const x = 1;\n")
        found = _by_path(tmp_path, _graph({"src/index.ts": []}))
        assert found["src/index.ts"][0] is EntryKind.MANIFEST

    def test_package_json_nested_exports(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "package.json",
            json.dumps({"exports": {".": {"import": "./src/mod.ts"}}}),
        )
        _write(tmp_path, "src/mod.ts", "export const y = 2;\n")
        kind, evidence = _by_path(tmp_path, _graph({"src/mod.ts": []}))["src/mod.ts"]
        assert kind is EntryKind.MANIFEST
        assert "exports" in evidence

    def test_package_json_bin_map(self, tmp_path: Path) -> None:
        _write(tmp_path, "package.json", json.dumps({"bin": {"tool": "src/cli.ts"}}))
        _write(tmp_path, "src/cli.ts", "export const run = () => {};\n")
        assert _by_path(tmp_path, _graph({"src/cli.ts": []}))["src/cli.ts"][0] is EntryKind.MANIFEST

    def test_setup_py_recorded_with_limitation_noted(self, tmp_path: Path) -> None:
        """不解析 console_scripts（不执行仓库内脚本），依据里要写明这个限制。"""
        _write(tmp_path, "setup.py", "from setuptools import setup\nsetup()\n")
        kind, evidence = _by_path(tmp_path, _graph({"setup.py": []}))["setup.py"]
        assert kind is EntryKind.MANIFEST
        assert "console_scripts" in evidence

    def test_nested_package_json_detected(self, tmp_path: Path) -> None:
        """前后端分离的仓库把 package.json 放在子目录，只读仓库根会漏掉。"""
        _write(tmp_path, "frontend/package.json", json.dumps({"main": "src/main.tsx"}))
        _write(tmp_path, "frontend/src/main.tsx", "export const App = () => null;\n")
        found = _by_path(tmp_path, _graph({"frontend/src/main.tsx": []}))
        kind, evidence = found["frontend/src/main.tsx"]
        assert kind is EntryKind.MANIFEST
        assert "frontend/package.json" in evidence

    def test_nested_pyproject_resolves_relative_to_its_dir(self, tmp_path: Path) -> None:
        """嵌套项目的包在自己目录下，模块路径不能按仓库根解析。"""
        _write(
            tmp_path,
            "services/api/pyproject.toml",
            '[project]\nname = "api"\n[project.scripts]\nserve = "api.main:run"\n',
        )
        _write(tmp_path, "services/api/api/main.py", "def run():\n    pass\n")
        found = _by_path(tmp_path, _graph({"services/api/api/main.py": []}))
        assert found["services/api/api/main.py"][0] is EntryKind.MANIFEST

    def test_manifest_scan_does_not_follow_junction_outside_repo(
        self, tmp_path: Path
    ) -> None:
        """glob 会穿透 junction。仓库内一个指向外部的联接就能让扫描落到仓库外，
        读出那里的 package.json，其字段值进入 evidence 并写进报告（KTD14）。
        """
        outside = tmp_path / "outside"
        _write(outside, "package.json", json.dumps({"main": "leak.ts"}))
        _write(outside, "leak.ts", "export const secret = 1;\n")

        repo = tmp_path / "repo"
        _write(repo, "src/own.ts", "export const o = 1;\n")
        _junction_or_skip(repo / "vendored", outside)

        found = _by_path(repo, _graph({"src/own.ts": []}))
        assert not any("leak" in p for p in found), f"读到了仓库外的清单：{found}"
        assert not any("leak" in evidence for _, evidence in found.values())

    def test_node_modules_manifests_ignored(self, tmp_path: Path) -> None:
        """node_modules 里有成千上万个 package.json，不能进扫描范围。"""
        _write(tmp_path, "node_modules/dep/package.json", json.dumps({"main": "index.ts"}))
        _write(tmp_path, "node_modules/dep/index.ts", "export const d = 1;\n")
        _write(tmp_path, "src/own.ts", "export const o = 1;\n")
        found = _by_path(tmp_path, _graph({"src/own.ts": []}))
        assert not any("node_modules" in p for p in found)

    def test_nx_project_json_detected(self, tmp_path: Path) -> None:
        """Nx workspace 的入口声明在各 app 的 project.json，不在根 package.json。

        实测教训：ghostfolio 的根 package.json 是 private 且无 main/bin/exports，
        只看 package.json 时一个 manifest 入口都识别不到。
        """
        _write(tmp_path, "package.json", json.dumps({"name": "ws", "private": True}))
        _write(
            tmp_path,
            "apps/api/project.json",
            json.dumps({"targets": {"build": {"options": {"main": "apps/api/src/main.ts"}}}}),
        )
        _write(tmp_path, "apps/api/src/main.ts", "export const boot = () => {};\n")
        found = _by_path(tmp_path, _graph({"apps/api/src/main.ts": []}))
        kind, evidence = found["apps/api/src/main.ts"]
        assert kind is EntryKind.MANIFEST
        assert "targets.build.options.main" in evidence

    def test_malformed_manifest_does_not_raise(self, tmp_path: Path) -> None:
        _write(tmp_path, "package.json", "{ not valid json ")
        _write(tmp_path, "pyproject.toml", "[project\nbroken")
        _write(tmp_path, "src/a.ts", "export const a = 1;\n")
        assert find_entrypoints(tmp_path, _graph({"src/a.ts": []})) is not None


class TestConvention:
    def test_python_dunder_main(self, tmp_path: Path) -> None:
        kind, evidence = _by_path(tmp_path, _graph({"pkg/__main__.py": ["pkg/core.py"]}))[
            "pkg/__main__.py"
        ]
        assert kind is EntryKind.CONVENTION
        assert "__main__.py" in evidence

    def test_shallow_ts_index_is_convention(self, tmp_path: Path) -> None:
        found = _by_path(tmp_path, _graph({"src/index.ts": ["src/app.ts"]}))
        assert found["src/index.ts"][0] is EntryKind.CONVENTION

    def test_deep_barrel_index_not_entrypoint(self, tmp_path: Path) -> None:
        """深层目录里的 index.ts 是 barrel 文件，不是入口。"""
        graph = _graph({"src/features/auth/deep/index.ts": ["src/features/auth/deep/impl.ts"]})
        found = _by_path(tmp_path, graph)
        entry = found.get("src/features/auth/deep/index.ts")
        assert entry is None or entry[0] is not EntryKind.CONVENTION

    def test_deep_main_is_still_entrypoint(self, tmp_path: Path) -> None:
        """深度限制只对 index 生效：monorepo 的 apps/*/src/main.ts 是真实入口。

        实测教训：一刀切按深度过滤时，Nx 的 apps/api/src/main.ts（深度 3）被挡掉，
        ghostfolio 一个 convention 入口都识别不出来。
        """
        graph = _graph({"apps/api/src/main.ts": ["apps/api/src/app.module.ts"]})
        kind, _ = _by_path(tmp_path, graph)["apps/api/src/main.ts"]
        assert kind is EntryKind.CONVENTION


class TestGraphRoots:
    def test_zero_indegree_node_recorded(self, tmp_path: Path) -> None:
        kind, evidence = _by_path(tmp_path, _graph({"scripts/run.py": ["lib/core.py"]}))[
            "scripts/run.py"
        ]
        assert kind is EntryKind.GRAPH_ROOT
        assert "零入度" in evidence

    def test_isolated_file_not_reported(self, tmp_path: Path) -> None:
        """零入度且零出度的孤立文件不算入口——那只是没人用的文件。"""
        graph = DependencyGraph(nodes=("orphan.py",), edges={"orphan.py": frozenset()})
        assert find_entrypoints(tmp_path, graph) == []

    def test_graph_roots_capped_and_ranked_by_outdegree(self, tmp_path: Path) -> None:
        """零入度节点可能有几百个，须设上限并按出度排序，否则淹没强证据。"""
        edges: dict[str, list[str]] = {"lib/core.py": []}
        for i in range(8):
            edges[f"s/script{i}.py"] = ["lib/core.py"]
        edges["s/orchestrator.py"] = [f"lib/mod{j}.py" for j in range(5)]
        for j in range(5):
            edges[f"lib/mod{j}.py"] = []

        found = find_entrypoints(tmp_path, _graph(edges), max_graph_roots=3)
        roots = [e for e in found if e.kind is EntryKind.GRAPH_ROOT]
        assert len(roots) == 3
        assert roots[0].path == "s/orchestrator.py"


class TestDeduplication:
    def test_strongest_evidence_wins(self, tmp_path: Path) -> None:
        """同一文件被多类证据命中时只留最强的——重复列出会被读成多个入口。"""
        _write(tmp_path, "package.json", json.dumps({"main": "src/index.ts"}))
        _write(tmp_path, "src/index.ts", "export const x = 1;\n")
        graph = _graph({"src/index.ts": ["src/other.ts"]})

        found = find_entrypoints(tmp_path, graph)
        matches = [e for e in found if e.path == "src/index.ts"]
        assert len(matches) == 1
        assert matches[0].kind is EntryKind.MANIFEST

    def test_manifest_sorted_before_weaker_evidence(self, tmp_path: Path) -> None:
        _write(tmp_path, "package.json", json.dumps({"main": "src/declared.ts"}))
        _write(tmp_path, "src/declared.ts", "export const d = 1;\n")
        graph = _graph({"src/declared.ts": ["src/dep.ts"], "scripts/loose.ts": ["src/dep.ts"]})

        kinds = [e.kind for e in find_entrypoints(tmp_path, graph)]
        assert kinds == sorted(kinds, key=lambda k: [EntryKind.MANIFEST, EntryKind.CONVENTION, EntryKind.GRAPH_ROOT].index(k))
