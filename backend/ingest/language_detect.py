"""克隆后的语言构成统计（R1）。

与克隆前的 trees API 清单不同：这一份是权威结果，用于报告与解析预算；那一份只用于
准入判断。两者会有小差异（浅克隆的工作树 == 默认分支的树，但 .gitignore 之外的
生成物可能不同）。

跳过依赖目录：node_modules 之类会让统计完全失真——一个 50 个源文件的项目可能有
两万个依赖文件。跳过它们不是优化，是正确性。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from backend.ingest.tree import SCALE_COUNTED_SUFFIXES

SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".next",
        ".nuxt",
        "target",
        "vendor",
        ".tox",
        "site-packages",
    }
)

SUFFIX_TO_LANGUAGE = {
    ".py": "Python",
    ".pyi": "Python",
    ".ts": "TypeScript",
    ".mts": "TypeScript",
    ".cts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".jsx": "JavaScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".rb": "Ruby",
    ".md": "Markdown",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".css": "CSS",
    ".scss": "CSS",
    ".html": "HTML",
    ".sh": "Shell",
    ".sql": "SQL",
}


@dataclass
class LanguageProfile:
    total_files: int = 0
    parseable_files: int = 0
    by_language: Counter[str] = field(default_factory=Counter)

    def share(self, language: str) -> float:
        if self.total_files == 0:
            return 0.0
        return self.by_language[language] / self.total_files


def detect_languages(workdir: Path) -> LanguageProfile:
    profile = LanguageProfile()

    for path in workdir.rglob("*"):
        # 任一父目录在跳过名单里就整条跳过——rglob 不支持剪枝，只能逐条判断。
        if any(part in SKIP_DIRS for part in path.relative_to(workdir).parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue

        profile.total_files += 1
        suffix = path.suffix.lower()
        # .d.ts 的 suffix 是 .ts，归类正确，无需特殊处理。
        language = SUFFIX_TO_LANGUAGE.get(suffix)
        if language:
            profile.by_language[language] += 1
        else:
            profile.by_language["Other"] += 1
        if suffix in SCALE_COUNTED_SUFFIXES:
            profile.parseable_files += 1

    return profile
