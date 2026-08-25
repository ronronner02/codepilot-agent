"""测试共用的构造助手。

抽出来的唯一理由是 `_env_file=None` 这一项容易漏。漏了之后测试会读真实的 `.env`，
行为随机器上有没有那个文件而变——这类失败在 CI 与本地表现不同，最难定位。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from backend.config import Settings


def junction_or_skip(link: Path, target: Path) -> None:
    """创建 Windows 目录联接（junction），不可用时跳过。

    为什么需要它而非符号链接：符号链接在 Windows 上要提权，实测报 `WinError 1314`，
    于是相关测试被跳过——而路径逃逸的 proof intent 是 required，跳过等于门没关。
    junction 免提权即可创建，同样是 reparse point，`realpath` 与 `glob` 都会穿透它，
    因此能在不提权的环境里提供真实的文件系统层逃逸覆盖。
    """
    if os.name != "nt":
        pytest.skip("目录联接是 Windows 专有机制")
    link.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"无法创建目录联接：{result.stdout.strip()} {result.stderr.strip()}")


def make_settings(**overrides: object) -> Settings:
    """构造测试用配置。

    `_env_file=None` 切断 .env 读取，让测试只依赖显式传入的值。
    deepseek_api_key 是必填项，给占位值——静态解析层与 ingest 都不调用 LLM。
    """
    base: dict[str, object] = {
        "deepseek_api_key": "test-key-not-used",
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]
