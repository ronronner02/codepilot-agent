"""克隆失败的分类与重试。

起因是一次真实故障：分析 leo-kuang-ai/spec-first 时 git 报
`GnuTLS recv error (-110): The TLS connection was non-properly terminated`。
当时的行为有三个问题，本文件逐条钉住修复：

1. 任何非零退出都归为 NOT_FOUND —— 一次网络抖动会显示成「仓库不存在，私有仓库需要配
   GITHUB_TOKEN」，把用户指向完全错误的修法；
2. 没有重试 —— 瞬时故障直接杀掉整次分析；
3. 限流配额已在提交阶段扣掉 —— 按 3 次/小时，三次抖动就把人锁一小时。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend.ingest import clone as clone_mod
from backend.ingest.clone import (
    CLONE_MAX_ATTEMPTS,
    classify_clone_failure,
    clone_repo,
)
from backend.ingest.guards import RejectReason, RepoRejected, parse_repo_url
from tests.support import make_settings

REF = parse_repo_url("https://github.com/leo-kuang-ai/spec-first")

# 触发本次修复的真实 git 输出。
TLS_ERROR = (
    "fatal: unable to access 'https://github.com/leo-kuang-ai/spec-first.git/': "
    "GnuTLS recv error (-110): The TLS connection was non-properly terminated."
)


class TestClassification:
    def test_real_tls_error_is_network_and_retryable(self) -> None:
        reason, transient = classify_clone_failure(TLS_ERROR)
        assert reason is RejectReason.NETWORK_ERROR
        assert transient is True

    @pytest.mark.parametrize(
        "output",
        [
            "fatal: unable to access '...': Could not resolve host: github.com",
            "error: RPC failed; curl 56 Recv failure: Connection reset by peer",
            "fatal: the remote end hung up unexpectedly",
            "fatal: early EOF",
            "OpenSSL SSL_read: Connection was reset, errno 10054",
        ],
    )
    def test_other_transient_shapes(self, output: str) -> None:
        reason, transient = classify_clone_failure(output)
        assert reason is RejectReason.NETWORK_ERROR
        assert transient is True

    def test_repository_not_found_is_permanent(self) -> None:
        reason, transient = classify_clone_failure(
            "remote: Repository not found.\nfatal: repository '...' not found"
        )
        assert reason is RejectReason.NOT_FOUND
        assert transient is False

    def test_auth_failure_maps_to_no_access(self) -> None:
        reason, transient = classify_clone_failure(
            "remote: Permission denied to user.\nfatal: Authentication failed"
        )
        assert reason is RejectReason.NO_ACCESS
        assert transient is False

    def test_permanent_wins_over_transient_when_both_match(self) -> None:
        """「unable to access」是瞬时特征，「not found」是永久特征——同时命中时按永久处理。

        否则一个明确的 404 会被当成抖动反复重试三次，让用户白等两轮退避。
        """
        reason, transient = classify_clone_failure(
            "fatal: unable to access '...': The requested URL returned error: 404 not found"
        )
        assert transient is False
        assert reason is RejectReason.NOT_FOUND

    def test_unknown_failure_is_not_retried(self) -> None:
        """认不出来的失败不重试：宁可让用户看到原始输出自己判断。"""
        reason, transient = classify_clone_failure("fatal: 某个没见过的错误")
        assert transient is False


class TestRetry:
    @staticmethod
    def _fake_git(outcomes: list[tuple[int, str]], calls: list[list[str]]) -> Any:
        """按序返回预设结果的 _run_git 替身。记录每次调用的 args 供断言。"""

        async def fake(args: list[str], cwd: Path | None, timeout: float) -> tuple[int, str]:
            calls.append(args)
            if args[0] == "clone":
                return outcomes.pop(0) if outcomes else (0, "")
            # rev-parse / rev-list：克隆成功后的元数据读取
            return (0, "abc123\n" if args[0] == "rev-parse" else "42\n")

        return fake

    @pytest.mark.asyncio
    async def test_transient_failure_is_retried_then_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(
            clone_mod, "_run_git", self._fake_git([(128, TLS_ERROR)], calls)
        )
        # 不真的等退避
        monkeypatch.setattr(clone_mod.asyncio, "sleep", lambda _: _noop())

        result = await clone_repo(REF, make_settings(workspace_root=tmp_path))

        assert result.head_sha == "abc123"
        assert sum(1 for c in calls if c[0] == "clone") == 2, "首次失败后应重试一次"

    @pytest.mark.asyncio
    async def test_gives_up_after_max_attempts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(
            clone_mod,
            "_run_git",
            self._fake_git([(128, TLS_ERROR)] * CLONE_MAX_ATTEMPTS, calls),
        )
        monkeypatch.setattr(clone_mod.asyncio, "sleep", lambda _: _noop())

        with pytest.raises(RepoRejected) as exc:
            await clone_repo(REF, make_settings(workspace_root=tmp_path))

        assert exc.value.reason is RejectReason.NETWORK_ERROR
        # 文案要说清重试过——否则用户会以为只试了一次。
        assert f"已重试 {CLONE_MAX_ATTEMPTS} 次" in str(exc.value)
        assert sum(1 for c in calls if c[0] == "clone") == CLONE_MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_permanent_failure_is_not_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """404 只该试一次。重试它纯粹是让用户多等。"""
        calls: list[list[str]] = []
        monkeypatch.setattr(
            clone_mod,
            "_run_git",
            self._fake_git([(128, "remote: Repository not found.")], calls),
        )

        with pytest.raises(RepoRejected) as exc:
            await clone_repo(REF, make_settings(workspace_root=tmp_path))

        assert exc.value.reason is RejectReason.NOT_FOUND
        assert sum(1 for c in calls if c[0] == "clone") == 1


async def _noop() -> None:
    return None
