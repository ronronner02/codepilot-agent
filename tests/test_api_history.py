"""历史列表端点与落盘重建的集成（U9、U21，R-43~R-46、NA-10、AE-11、AE-12）。

重启由「用同一个 workspace_root 造第二个 app」模拟——那正是重启后发生的事：进程内存空了，
落盘目录还在。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.history.store import SCHEMA_VERSION
from backend.main import create_app
from tests.support import make_settings
from tests.test_api import (
    _TEST_CREDENTIALS,
    _rich_client,
    _run_to_completion,
    _stub_nodeset,
)


def _submit_and_finish(client: TestClient, url: str = "https://github.com/acme/widget") -> str:
    response = client.post(
        "/api/analyses", json={"repo_url": url, "credentials": _TEST_CREDENTIALS}
    )
    task_id = response.json()["task_id"]
    _run_to_completion(client, task_id)
    return task_id


class TestSummaryShape:
    def test_entry_carries_repo_sha_time_files_findings(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """AE-11：每条含仓库标识、commit 短 SHA、时间、文件数、发现数。"""
        with _rich_client(monkeypatch, tmp_path) as client:
            task_id = _submit_and_finish(client)
            listed = client.get("/api/analyses").json()

            entry = next(item for item in listed if item["task_id"] == task_id)
            assert entry["repo"] == "acme/widget"
            assert entry["commit_sha"]
            assert entry["created_at"] > 0
            assert entry["file_count"] > 0
            assert entry["finding_count"] >= 0

    def test_no_score_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """NA-01：列表不含评分字段。"""
        with _rich_client(monkeypatch, tmp_path) as client:
            _submit_and_finish(client)
            body = json.dumps(client.get("/api/analyses").json())
            for word in ("score", "rating", "stars", "tech_debt", "grade"):
                assert word not in body.lower()

    def test_sorted_newest_first(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        with _rich_client(monkeypatch, tmp_path) as client:
            first = _submit_and_finish(client, "https://github.com/acme/one")
            second = _submit_and_finish(client, "https://github.com/acme/two")

            listed = client.get("/api/analyses").json()
            ids = [item["task_id"] for item in listed]
            assert ids.index(second) < ids.index(first)


class TestQueuedExclusion:
    def test_queued_task_absent_from_list(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """R-46 的延伸、NA-10：排队中的任务不出现在列表。

        排队态随进程内存易失，列出来就等于暗示它可以恢复。
        """
        import asyncio

        from backend.graph.builder import NodeSet
        from backend.graph.observability import observed

        gate = asyncio.Event()
        base = _stub_nodeset()

        @observed("ingest", fatal=True)
        async def blocking(state: dict) -> dict:  # type: ignore[type-arg]
            await gate.wait()
            return {"workdir": str(tmp_path / "stub"), "commit_sha": "c" * 40}

        nodes = NodeSet(
            ingest=blocking,
            parse=base.parse,
            cluster=base.cluster,
            planner=base.planner,
            module_agent=base.module_agent,
            chunk_and_index=base.chunk_and_index,
            select_files=base.select_files,
            reviewer=base.reviewer,
            synthesize=base.synthesize,
        )
        monkeypatch.setattr("backend.api.tasks.make_real_nodes", lambda s, p: nodes)
        app = create_app(
            make_settings(
                workspace_root=tmp_path / "ws",
                max_concurrent_analyses=1,
                submissions_per_window=10,
            )
        )

        with TestClient(app) as client:
            running = client.post(
                "/api/analyses",
                json={"repo_url": "https://github.com/acme/one", "credentials": _TEST_CREDENTIALS},
            ).json()
            queued = client.post(
                "/api/analyses",
                json={"repo_url": "https://github.com/acme/two", "credentials": _TEST_CREDENTIALS},
            ).json()
            assert queued["queue_position"] == 1

            listed = client.get("/api/analyses").json()
            ids = {item["task_id"] for item in listed}
            assert queued["task_id"] not in ids
            assert running["task_id"] in ids

            # 界面没有「继续」入口可给——列表里连这条记录都没有。
            gate.set()


class TestRestartRebuild:
    def test_completed_analysis_survives_restart(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """AE-12：分析完成后落盘，重启后仍在列表且可载入。"""
        workspace = tmp_path / "ws"

        with _rich_client(monkeypatch, tmp_path) as client:
            task_id = _submit_and_finish(client)
            assert (workspace / "analyses" / f"{task_id}.json").is_file()

        # 第二个 app 共用同一个 workspace，进程内存是空的——即重启。
        monkeypatch.setattr(
            "backend.api.tasks.make_real_nodes", lambda s, p: _stub_nodeset()
        )
        restarted = create_app(make_settings(workspace_root=workspace))
        with TestClient(restarted) as client:
            listed = client.get("/api/analyses").json()
            assert any(item["task_id"] == task_id for item in listed)

            # 结果也能读回来。
            result = client.get(f"/api/analyses/{task_id}")
            assert result.status_code == 200
            assert result.json()["repo"] == "acme/widget"
            assert result.json()["report"] is not None

    def test_corrupt_file_skipped_after_restart(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "ws"

        with _rich_client(monkeypatch, tmp_path) as client:
            task_id = _submit_and_finish(client)

        analyses = workspace / "analyses"
        (analyses / "broken.json").write_text("{ not json", encoding="utf-8")
        (analyses / "future.json").write_text(
            json.dumps({"schema_version": SCHEMA_VERSION + 99, "created_at": 1.0, "result": {}}),
            encoding="utf-8",
        )

        monkeypatch.setattr(
            "backend.api.tasks.make_real_nodes", lambda s, p: _stub_nodeset()
        )
        restarted = create_app(make_settings(workspace_root=workspace))
        with TestClient(restarted) as client:
            listed = client.get("/api/analyses").json()
            # 两个坏文件被跳过，好的那条仍在。
            assert [item["task_id"] for item in listed] == [task_id]

    def test_persisted_file_contains_no_credentials(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """NA-02 在落盘实现后的复测——落盘是凭证泄露的新路径。"""
        with _rich_client(monkeypatch, tmp_path) as client:
            task_id = _submit_and_finish(client)

        text = (tmp_path / "ws" / "analyses" / f"{task_id}.json").read_text(
            encoding="utf-8"
        )
        assert _TEST_CREDENTIALS["api_key"] not in text
        assert "guest-key" not in text
        assert "credentials" not in text

    def test_viewer_reports_cleared_not_task_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """KTD10 的第四态：重启后查看器要说「代码副本已清理」，不是「任务不存在」。

        两者的区别对用户是实质性的：报告就在眼前时看到「该分析不存在」是自相矛盾的信号，
        而前端正是按 reason 分类呈现的。
        """
        workspace = tmp_path / "ws"
        with _rich_client(monkeypatch, tmp_path) as client:
            task_id = _submit_and_finish(client)

        monkeypatch.setattr(
            "backend.api.tasks.make_real_nodes", lambda s, p: _stub_nodeset()
        )
        restarted = create_app(make_settings(workspace_root=workspace))
        with TestClient(restarted) as client:
            # 结果可读——这是前提。
            assert client.get(f"/api/analyses/{task_id}").status_code == 200

            for path in (f"/api/analyses/{task_id}/file?path=a.py", f"/api/analyses/{task_id}/tree"):
                response = client.get(path)
                assert response.status_code == 410, path
                assert response.json()["reason"] == "workspace_cleared", path
                assert "分析结果" in response.json()["message"]

            # 真正不存在的任务仍是 task_not_found，两者可区分。
            missing = client.get("/api/analyses/nope/file?path=a.py")
            assert missing.status_code == 404
            assert missing.json()["reason"] == "task_not_found"

    def test_search_still_works_after_restart(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """检索不依赖工作副本：索引目录不被清理，缓存键也不含副本路径（KTD10）。

        所以重启后检索该给出「未建索引」或真实命中，而不是「任务不存在」——后者会让用户
        以为这次分析整个没了。
        """
        workspace = tmp_path / "ws"
        with _rich_client(monkeypatch, tmp_path) as client:
            task_id = _submit_and_finish(client)

        monkeypatch.setattr(
            "backend.api.tasks.make_real_nodes", lambda s, p: _stub_nodeset()
        )
        restarted = create_app(make_settings(workspace_root=workspace))
        with TestClient(restarted) as client:
            response = client.post(
                f"/api/analyses/{task_id}/search", json={"query": "token 校验"}
            )
            assert response.status_code != 404
            assert response.json().get("reason") != "task_not_found"

    def test_questions_still_reachable_after_restart(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """问答只依赖索引，重启后不该返回「任务不存在」。

        否则界面上会出现一份可读的报告配一个「该分析不存在」的问答框——两个自相矛盾的信号。
        """
        workspace = tmp_path / "ws"
        with _rich_client(monkeypatch, tmp_path) as client:
            task_id = _submit_and_finish(client)

        monkeypatch.setattr(
            "backend.api.tasks.make_real_nodes", lambda s, p: _stub_nodeset()
        )
        restarted = create_app(make_settings(workspace_root=workspace))
        with TestClient(restarted) as client:
            response = client.post(
                f"/api/analyses/{task_id}/questions",
                json={"question": "认证在哪", "credentials": _TEST_CREDENTIALS},
            )
            assert response.status_code != 404
            assert response.json().get("reason") != "task_not_found"

    def test_quota_cleanup_leaves_history_readable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """KTD10、AE-15：配额清理只删仓库副本，落盘 JSON 仍在，分析仍可载入。"""
        import shutil

        workspace = tmp_path / "ws"
        with _rich_client(monkeypatch, tmp_path) as client:
            task_id = _submit_and_finish(client)

        # 模拟配额清理：删掉仓库目录，落盘目录不动。
        repos = workspace / "repos"
        if repos.exists():
            shutil.rmtree(repos)

        monkeypatch.setattr(
            "backend.api.tasks.make_real_nodes", lambda s, p: _stub_nodeset()
        )
        restarted = create_app(make_settings(workspace_root=workspace))
        with TestClient(restarted) as client:
            assert (
                client.get(f"/api/analyses/{task_id}").status_code == 200
            ), "落盘 JSON 应与仓库副本解耦"
            listed = client.get("/api/analyses").json()
            assert any(item["task_id"] == task_id for item in listed)
