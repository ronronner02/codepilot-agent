"""入口点识别。

三类证据，按可信度从高到低。分级是必要的——报告里说「这是入口」必须能给出依据
（R7 的可追溯要求），而三类证据的强度差别很大：

  MANIFEST    作者在配置里显式声明的。最可信。
  CONVENTION  语言约定的文件名（`__main__.py`、`main.py`）。较可信。
  GRAPH_ROOT  图上零入度节点。最弱——孤立的脚本、被删了引用的死文件、测试固件都是
              零入度。单独看几乎无意义，只作为前两类的补充。

不用 LLM 判断入口，理由同 KTD1：这些是确定信号，代码能读出来，让 LLM 猜只会引入
不可复现的判断。
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from backend.paths import PathEscapeError, resolve_within
from backend.static_analysis.models import DependencyGraph, EntryKind, EntryPoint

# 约定型入口的文件名。`__main__.py` 是 `python -m pkg` 的入口，其余是社区惯例。
_PYTHON_CONVENTION_NAMES = ("__main__.py", "main.py", "app.py", "manage.py", "wsgi.py", "asgi.py")
_TS_CONVENTION_STEMS = ("index", "main", "server", "app")


# 嵌套清单的扫描深度与跳过目录。
#
# 为什么要扫嵌套：前后端分离的仓库把 package.json 放在 `frontend/`、monorepo 放在
# `packages/*/`，只读仓库根会让这类项目一条 manifest 证据都拿不到，入口点全部退化
# 成最弱的图上零入度信号。
#
# 深度设 2 而非无界：再深就进入 `packages/*/src/*` 这类层级，那里的 package.json
# 通常是构建产物或测试固件。加上 node_modules 跳过——里面有成千上万个 package.json。
_MANIFEST_SCAN_DEPTH = 2
_MANIFEST_SKIP_DIRS = frozenset(
    {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build", ".tox"}
)


def _manifest_dirs(repo_root: Path) -> list[Path]:
    """仓库根加有界深度内的子目录，按深度与字典序排列。

    每个候选目录都过 resolve_within（KTD14 的单一校验实现），不在这里重复写一套
    reparse point 判断——漏一处即失效，而复用能保证与其他调用点同步。

    为什么必须校验：`glob` 会穿透符号链接与 junction。仓库里一个指向外部的联接就能
    让扫描落到仓库外，随后读出那里的 package.json，其字段值进入 evidence 字符串并
    最终写进报告——既是内容泄露，也让报告出现仓库外的路径。
    """
    found = [repo_root]
    for depth in range(1, _MANIFEST_SCAN_DEPTH + 1):
        for candidate in sorted(repo_root.glob("/".join(["*"] * depth))):
            if not candidate.is_dir():
                continue
            relative = candidate.relative_to(repo_root)
            if any(
                p in _MANIFEST_SKIP_DIRS or p.startswith(".") for p in relative.parts
            ):
                continue
            try:
                resolve_within(repo_root, relative)
            except PathEscapeError:
                continue
            found.append(candidate)
    return found


def _rel(repo_root: Path, path: Path) -> str:
    return path.relative_to(repo_root).as_posix()


def _read_json(path: Path) -> dict[str, object] | None:
    """读 json 清单。读不到或格式不对返回 None——清单损坏不应中断入口识别。"""
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _from_pyproject(repo_root: Path, manifest_dir: Path) -> list[EntryPoint]:
    """读 pyproject.toml 的 `[project.scripts]` 与 `[project.gui-scripts]`。

    值的形态是 `pkg.module:func`，冒号前是模块路径。转成文件路径时同时试
    `pkg/module.py` 与 `pkg/module/__init__.py`——两者都是合法的模块形态。

    模块路径相对 manifest 所在目录解析，而非仓库根：嵌套项目的包在自己的目录下。
    """
    config = manifest_dir / "pyproject.toml"
    if not config.is_file():
        return []
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []

    project = data.get("project", {})
    if not isinstance(project, dict):
        return []

    label = _rel(repo_root, config) if manifest_dir != repo_root else "pyproject.toml"
    found: list[EntryPoint] = []
    for table in ("scripts", "gui-scripts"):
        scripts = project.get(table, {})
        if not isinstance(scripts, dict):
            continue
        for command, spec in scripts.items():
            module = str(spec).split(":")[0]
            stem = module.replace(".", "/")
            for candidate in (f"{stem}.py", f"{stem}/__init__.py"):
                absolute = manifest_dir / candidate
                if absolute.is_file():
                    found.append(
                        EntryPoint(
                            path=_rel(repo_root, absolute),
                            kind=EntryKind.MANIFEST,
                            evidence=f"{label} [project.{table}] {command} = {spec}",
                        )
                    )
                    break
    return found


def _from_package_json(repo_root: Path, manifest_dir: Path) -> list[EntryPoint]:
    """读 package.json 的 `main`、`bin`、`exports`。

    三个字段的语义不同（main 是库的默认入口，bin 是可执行命令，exports 是条件导出
    表），但对「哪个文件是入口」这个问题它们等价，故统一处理。

    exports 的值可以嵌套（`{".": {"import": "./x.js", "require": "./y.cjs"}}`），
    故递归收集字符串叶子。
    """
    config = manifest_dir / "package.json"
    if not config.is_file():
        return []
    data = _read_json(config)
    if data is None:
        return []

    label = _rel(repo_root, config) if manifest_dir != repo_root else "package.json"
    found: list[EntryPoint] = []

    def record(raw: str, field: str) -> None:
        for candidate in _typescript_source_candidates(raw):
            absolute = manifest_dir / candidate
            if absolute.is_file():
                found.append(
                    EntryPoint(
                        path=_rel(repo_root, absolute),
                        kind=EntryKind.MANIFEST,
                        evidence=f"{label} {field} = {raw}",
                    )
                )
                return

    def walk(value: object, field: str) -> None:
        if isinstance(value, str):
            record(value, field)
        elif isinstance(value, dict):
            for key, nested in value.items():
                walk(nested, f"{field}.{key}")
        elif isinstance(value, list):
            for nested in value:
                walk(nested, field)

    for field_name in ("main", "module", "bin", "exports"):
        if field_name in data:
            walk(data[field_name], field_name)
    return found


def _typescript_source_candidates(raw: str) -> list[str]:
    """把 package.json 里的入口路径映射到可能的源文件。

    声明的入口通常指向构建产物（`dist/index.js`），而依赖图的节点是源文件。故除了
    原路径，还试：去掉 dist/build 前缀后的 src 下同名文件、以及把 `.js` 换成
    `.ts`/`.tsx`。这是启发式——找不到就不记，不猜。
    """
    cleaned = raw.lstrip("./")
    candidates = [cleaned]

    stem = cleaned
    for ext in (".js", ".mjs", ".cjs", ".jsx"):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
            break

    for ext in (".ts", ".tsx", ".mts", ".cts"):
        candidates.append(f"{stem}{ext}")

    # dist/index.js -> src/index.ts
    parts = stem.split("/")
    if parts and parts[0] in ("dist", "build", "lib", "out"):
        rest = "/".join(parts[1:])
        for ext in (".ts", ".tsx"):
            candidates.append(f"src/{rest}{ext}")
            candidates.append(f"{rest}{ext}")

    return candidates


def _from_nx_project(repo_root: Path, manifest_dir: Path) -> list[EntryPoint]:
    """读 Nx 的 project.json：`targets.<target>.options.main`。

    为什么需要单独处理：Nx workspace 的根 package.json 是 private 且不带 main/bin/
    exports，各 app 的入口声明在自己的 project.json 里。实测 ghostfolio 就是这样——
    只看 package.json 时一个 manifest 入口都识别不到，全部退化成图上零入度信号。

    路径相对 workspace 根解析（Nx 的约定），而非相对 project.json 所在目录。
    """
    config = manifest_dir / "project.json"
    if not config.is_file():
        return []
    raw = _read_json(config)
    if raw is None:
        return []

    targets = raw.get("targets")
    if not isinstance(targets, dict):
        return []

    label = _rel(repo_root, config)
    found: list[EntryPoint] = []
    for target_name, target in targets.items():
        if not isinstance(target, dict):
            continue
        options = target.get("options")
        if not isinstance(options, dict):
            continue
        for field_name in ("main", "browser", "server"):
            value = options.get(field_name)
            if not isinstance(value, str):
                continue
            candidate = value.lstrip("./")
            if (repo_root / candidate).is_file():
                found.append(
                    EntryPoint(
                        path=candidate,
                        kind=EntryKind.MANIFEST,
                        evidence=f"{label} targets.{target_name}.options.{field_name} = {value}",
                    )
                )
    return found


def _from_setup_py(repo_root: Path, manifest_dir: Path) -> list[EntryPoint]:
    """setup.py 存在即视为打包入口。

    不解析它的 `console_scripts`——那需要执行 setup.py 或做 AST 分析，而 KTD12
    明确不执行仓库内任何脚本。AST 分析能做，但 console_scripts 的值常来自变量或
    动态拼接，静态读取覆盖率低；现代项目的入口声明也多已迁到 pyproject.toml。
    故只把 setup.py 自身记为入口，并在证据里说明限制。
    """
    config = manifest_dir / "setup.py"
    if not config.is_file():
        return []
    return [
        EntryPoint(
            path=_rel(repo_root, config),
            kind=EntryKind.MANIFEST,
            evidence="setup.py 存在（未解析其 console_scripts：不执行仓库内脚本）",
        )
    ]


def _by_convention(nodes: tuple[str, ...]) -> list[EntryPoint]:
    found: list[EntryPoint] = []
    for node in nodes:
        name = Path(node).name
        if name in _PYTHON_CONVENTION_NAMES:
            found.append(
                EntryPoint(path=node, kind=EntryKind.CONVENTION, evidence=f"文件名约定：{name}")
            )
            continue
        stem = Path(node).stem
        if not any(node.endswith(ext) for ext in (".ts", ".tsx", ".mts", ".cts")):
            continue

        depth = node.count("/")
        # 深度限制只对 index 生效。index.ts 在任何层级都可能是 barrel 文件（再导出
        # 聚合），深层的几乎必然是；而 main/server/app 无论多深都是明确的入口信号。
        #
        # 实测教训：一刀切按深度 <= 2 过滤时，Nx monorepo 的 apps/api/src/main.ts
        # （深度 3）被挡掉，ghostfolio 一个 convention 入口都识别不出来。
        if stem == "index":
            if depth <= 1:
                found.append(
                    EntryPoint(
                        path=node,
                        kind=EntryKind.CONVENTION,
                        evidence=f"文件名约定：index（层级 {depth}，深层 index 视为 barrel）",
                    )
                )
        elif stem in _TS_CONVENTION_STEMS:
            found.append(
                EntryPoint(
                    path=node,
                    kind=EntryKind.CONVENTION,
                    evidence=f"文件名约定：{Path(node).name}",
                )
            )
    return found


def _graph_roots(graph: DependencyGraph, limit: int) -> list[EntryPoint]:
    """零入度节点。

    数量设上限：大仓库里零入度节点可能有几百个（测试文件、脚本、示例），全部列出
    会淹没前两类的强证据。按出度降序取前 limit 个——出度高说明它主动依赖很多东西，
    更像编排入口而非孤立文件。
    """
    reverse = graph.reverse_edges()
    roots = [
        node
        for node in graph.nodes
        if not reverse.get(node, frozenset()) and graph.edges.get(node, frozenset())
    ]
    roots.sort(key=lambda n: (-len(graph.edges.get(n, frozenset())), n))
    return [
        EntryPoint(
            path=node,
            kind=EntryKind.GRAPH_ROOT,
            evidence=f"图上零入度，出度 {len(graph.edges.get(node, frozenset()))}",
        )
        for node in roots[:limit]
    ]


def find_entrypoints(
    repo_root: Path, graph: DependencyGraph, max_graph_roots: int = 10
) -> list[EntryPoint]:
    """汇总三类入口证据。

    同一文件被多类证据命中时只保留最强的一条——报告里重复列出同一个文件会让读者
    以为那是三个入口。排序按 (证据强度, 路径) 使输出稳定。
    """
    manifest_entries: list[EntryPoint] = []
    for manifest_dir in _manifest_dirs(repo_root):
        manifest_entries.extend(_from_pyproject(repo_root, manifest_dir))
        manifest_entries.extend(_from_package_json(repo_root, manifest_dir))
        manifest_entries.extend(_from_nx_project(repo_root, manifest_dir))
        manifest_entries.extend(_from_setup_py(repo_root, manifest_dir))

    candidates = [
        *manifest_entries,
        *_by_convention(graph.nodes),
        *_graph_roots(graph, max_graph_roots),
    ]

    strength = {EntryKind.MANIFEST: 0, EntryKind.CONVENTION: 1, EntryKind.GRAPH_ROOT: 2}
    best: dict[str, EntryPoint] = {}
    for entry in candidates:
        current = best.get(entry.path)
        if current is None or strength[entry.kind] < strength[current.kind]:
            best[entry.path] = entry

    return sorted(best.values(), key=lambda e: (strength[e.kind], e.path))
