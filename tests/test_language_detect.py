"""语言构成统计。

重点是跳过依赖目录——node_modules 会让统计完全失真，一个 50 个源文件的项目可能有
两万个依赖文件。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.ingest.language_detect import detect_languages


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    files = {
        "src/main.py": "print(1)",
        "src/util.py": "x = 1",
        "src/types.d.ts": "declare const x: number;",
        "web/app.tsx": "export const A = () => null;",
        "web/api.ts": "export const f = () => 1;",
        "README.md": "# hi",
        "pyproject.toml": "[project]",
        "assets/logo.png": "binary-ish",
        "node_modules/left-pad/index.js": "module.exports = 1;",
        "node_modules/left-pad/deep/nested/a.ts": "export const a = 1;",
        ".git/config": "[core]",
        "__pycache__/main.cpython-313.pyc": "bytecode",
        "dist/bundle.js": "minified",
    }
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def test_skips_dependency_and_vcs_dirs(sample_repo: Path) -> None:
    profile = detect_languages(sample_repo)
    # 8 个计入：2 py + 1 d.ts + 1 tsx + 1 ts + md + toml + png
    assert profile.total_files == 8


def test_counts_by_language(sample_repo: Path) -> None:
    profile = detect_languages(sample_repo)
    assert profile.by_language["Python"] == 2
    assert profile.by_language["TypeScript"] == 3  # .d.ts 的 suffix 是 .ts
    assert profile.by_language["Markdown"] == 1
    assert profile.by_language["Other"] == 1  # .png


def test_parseable_count_excludes_docs_and_config(sample_repo: Path) -> None:
    profile = detect_languages(sample_repo)
    assert profile.parseable_files == 5  # 2 py + 3 ts/tsx


def test_share_sums_to_one(sample_repo: Path) -> None:
    profile = detect_languages(sample_repo)
    assert sum(profile.by_language.values()) == profile.total_files
    assert 0 < profile.share("Python") < 1


def test_empty_repo_does_not_divide_by_zero(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    profile = detect_languages(empty)
    assert profile.total_files == 0
    assert profile.share("Python") == 0.0
