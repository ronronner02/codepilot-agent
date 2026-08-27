"""文件内容与文件树端点（U3：R-16、R-17、R-18、R-57、BR-007、NA-04）。

**路径逃逸的断言必须落在真实文件系统上。** 字符串层面的检查挡不住 realpath 层面的
逃逸——一个指向仓库外的 junction 在字面上仍位于工作目录之内，逐段的字符串比较与
前缀包含判断会双双放行。所以这里实际创建符号链接与 junction，沿用
`tests/test_paths.py` 与 `tests/test_ingest_clone_escape.py` 已建立的手法。

这个端点是本期新增的**公网可达的任意路径入口**，所以它的负面断言比正面断言更重要：
读到工作副本之外的任何内容都是严重漏洞。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.api.progress import Stage
from backend.api.tasks import AnalysisTask
from backend.main import create_app
from tests.support import junction_or_skip, make_settings

REPO_URL = "https://github.com/acme/widget"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """一份仓库工作副本，外加一个仓库外的秘密文件供逃逸测试作目标。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("仓库外的内容，不得被读到", encoding="utf-8")

    workdir = tmp_path / "repos" / "acme__widget"
    (workdir / "pkg").mkdir(parents=True)
    (workdir / "pkg" / "core.py").write_text(
        "\n".join(f"line {i}" for i in range(1, 61)), encoding="utf-8"
    )
    (workdir / "pkg" / "empty.py").write_text("", encoding="utf-8")
    (workdir / "README.md").write_text("# widget", encoding="utf-8")
    (workdir / "node_modules").mkdir()
    (workdir / "node_modules" / "junk.js").write_text("x", encoding="utf-8")
    (workdir / "__pycache__").mkdir()
    return workdir


def _app_with_task(
    tmp_path: Path, workdir: Path | None, *, completed: bool = True
) -> Any:
    """建一个应用并直接往注册表里塞一个已完成的任务。

    不跑真实分析：本单元要验证的是路径校验与读取语义，而跑一遍图会把 clone、解析、
    LLM 全部拖进来——那些失败会掩盖这里真正要看的东西。
    """
    app = create_app(make_settings(workspace_root=tmp_path / "ws"))
    task = AnalysisTask(
        task_id="t1",
        repo_url=REPO_URL,
        repo="acme/widget",
        stage=Stage.DONE if completed else Stage.PARSING,
        completed=completed,
    )
    if workdir is not None:
        task.final_state["workdir"] = str(workdir)
    app.state.registry._tasks[task.task_id] = task
    return app


@pytest.fixture
def client(tmp_path: Path, workspace: Path) -> Any:
    with TestClient(_app_with_task(tmp_path, workspace)) as test_client:
        yield test_client


def _content(client: TestClient, path: str, **params: Any) -> Any:
    return client.get(
        "/api/analyses/t1/file", params={"path": path, **params}
    )


def _tree(client: TestClient, path: str = ".") -> Any:
    return client.get("/api/analyses/t1/tree", params={"path": path})


class TestPathEscape:
    """NA-04：四类逃逸输入全部被拒。proof intent 为 required。"""

    def test_relative_traversal_rejected(self, client: TestClient) -> None:
        """Covers AE-06。`../../../etc/passwd` 被拒且不返回任何内容。"""
        response = _content(client, "../../../etc/passwd")
        assert response.status_code == 400
        assert response.json()["reason"] == "invalid_path"
        assert "content" not in response.json()

    def test_traversal_into_sibling_of_workdir_rejected(
        self, client: TestClient
    ) -> None:
        """逃到工作副本的兄弟目录同样被拒——那里放着别的访客的仓库副本。"""
        assert _content(client, "../../outside/secret.txt").status_code == 400

    def test_absolute_posix_path_rejected(self, client: TestClient) -> None:
        """Covers AE-06。绝对路径在端点层就被拒，不进 resolve_within（R-57）。"""
        response = _content(client, "/etc/passwd")
        assert response.status_code == 400
        assert response.json()["reason"] == "invalid_path"

    def test_absolute_windows_path_rejected(
        self, client: TestClient, workspace: Path
    ) -> None:
        """带盘符的绝对路径同样被拒，即便它指向的就是工作副本内的文件。

        `resolve_within` 本身允许绝对路径入参并逐段校验，所以这条不是「防逃逸」而是
        「收窄输入形态」：公网端点上少接受一种写法就少一种被构造的可能。
        """
        target = (workspace / "pkg" / "core.py").resolve()
        response = _content(client, str(target))
        assert response.status_code == 400
        assert response.json()["reason"] == "invalid_path"

    def test_symlink_to_outside_rejected(
        self, client: TestClient, workspace: Path, tmp_path: Path
    ) -> None:
        """Covers AE-06。经仓库内指向仓库外的符号链接的路径被拒。"""
        link = workspace / "leak"
        try:
            link.symlink_to(tmp_path / "outside", target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"无法创建符号链接（Windows 需提权）：{exc}")
        assert _content(client, "leak/secret.txt").status_code == 400

    def test_junction_to_outside_rejected(
        self, client: TestClient, workspace: Path, tmp_path: Path
    ) -> None:
        """Covers AE-06。Windows junction 被拒。

        junction 比符号链接更需要防：它免提权即可创建，而这个端点是公网入口。
        """
        junction_or_skip(workspace / "leakdir", tmp_path / "outside")
        assert _content(client, "leakdir/secret.txt").status_code == 400

    def test_tree_skips_junction_entries(
        self, client: TestClient, workspace: Path, tmp_path: Path
    ) -> None:
        """`iterdir` 会穿透 junction，所以文件树的每个条目都要过校验。"""
        junction_or_skip(workspace / "leakdir", tmp_path / "outside")
        body = _tree(client).json()
        assert all(entry["path"] != "leakdir" for entry in body["entries"])


class TestFileContent:
    def test_returns_content_with_line_bounds(self, client: TestClient) -> None:
        body = _content(client, "pkg/core.py").json()
        assert body["path"] == "pkg/core.py"
        assert body["start_line"] == 1
        assert body["total_lines"] == 60
        assert body["content"].startswith("line 1")

    def test_line_range_honoured(self, client: TestClient) -> None:
        body = _content(client, "pkg/core.py", start_line=10, end_line=12).json()
        assert body["start_line"] == 10
        assert body["end_line"] == 12
        assert body["content"] == "line 10\nline 11\nline 12"

    def test_out_of_range_end_converges(self, client: TestClient) -> None:
        """越界收敛而非报错——界面跳转到某条发现的行号时可能带上超出文件末尾的范围。"""
        body = _content(client, "pkg/core.py", start_line=55, end_line=9999).json()
        assert body["end_line"] == 60
        assert body["truncated"] is False

    def test_truncation_is_explicit(self, tmp_path: Path, workspace: Path) -> None:
        """Covers AE-06。超上限的文件返回前 N 行并说明共 M 行，不静默截断（R-18）。"""
        big = workspace / "big.py"
        big.write_text("\n".join(f"row {i}" for i in range(1, 5001)), encoding="utf-8")
        with TestClient(_app_with_task(tmp_path, workspace)) as client:
            body = _content(client, "big.py").json()
        assert body["total_lines"] == 5000
        assert body["truncated"] is True
        assert "5000" in body["truncated_note"]
        assert body["end_line"] < 5000

    def test_oversized_file_has_its_own_reason(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        """体积门与行数门是两道独立的门。

        只有行数门的话，一个几十 MB 的单行压缩产物会被整体读进内存再截断——那是内存
        问题而非显示问题，所以它有自己的 reason，不走截断路径。
        """
        (workspace / "bundle.min.js").write_text("x" * 600_000, encoding="utf-8")
        with TestClient(_app_with_task(tmp_path, workspace)) as client:
            response = _content(client, "bundle.min.js")
        assert response.status_code == 413
        assert response.json()["reason"] == "file_too_large"

    def test_missing_file_distinct_from_invalid_path(self, client: TestClient) -> None:
        """「文件不存在」与「路径非法」是两个 reason——前者该换文件，后者是被拒绝。"""
        missing = _content(client, "pkg/nope.py")
        invalid = _content(client, "../outside/secret.txt")
        assert missing.status_code == 404
        assert missing.json()["reason"] == "file_not_found"
        assert invalid.json()["reason"] != missing.json()["reason"]

    def test_directory_request_rejected_without_listing(
        self, client: TestClient
    ) -> None:
        """请求目录时返回可区分的错误，不返回目录内容。"""
        response = _content(client, "pkg")
        assert response.status_code == 400
        assert response.json()["reason"] == "not_a_file"

    def test_empty_file_is_not_an_error(self, client: TestClient) -> None:
        body = _content(client, "pkg/empty.py").json()
        assert body["total_lines"] == 0
        assert body["content"] == ""
        assert body["truncated"] is False

    def test_unknown_task_returns_task_not_found(self, client: TestClient) -> None:
        response = client.get("/api/analyses/nope/file", params={"path": "README.md"})
        assert response.status_code == 404
        assert response.json()["reason"] == "task_not_found"

    def test_cleared_workspace_has_its_own_reason(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        """KTD10 的第四态：落盘结果还在，只有仓库副本被配额清理删掉了。

        与「文件不存在」必须可区分——界面据此显示「代码副本已清理」而不是让用户
        以为自己路径写错了。
        """
        app = _app_with_task(tmp_path, workspace)
        import shutil

        shutil.rmtree(workspace)
        with TestClient(app) as client:
            response = _content(client, "pkg/core.py")
        assert response.status_code == 410
        assert response.json()["reason"] == "workspace_cleared"

    def test_workspace_not_yet_available(self, tmp_path: Path) -> None:
        """分析还没克隆完时的空态，与「已清理」不是同一件事。"""
        app = _app_with_task(tmp_path, None, completed=False)
        with TestClient(app) as client:
            response = _content(client, "pkg/core.py")
        assert response.status_code == 409
        assert response.json()["reason"] == "workspace_unavailable"


class TestFileTree:
    def test_lists_single_level_only(self, client: TestClient) -> None:
        """按需展开一层。递归在大仓库上产出上万条目，界面也用不上。"""
        body = _tree(client).json()
        paths = {entry["path"] for entry in body["entries"]}
        assert "pkg" in paths
        assert "README.md" in paths
        # pkg 的成员不在根的列表里——它们要再请求一次才出现。
        assert "pkg/core.py" not in paths

    def test_child_directory_listed_on_demand(self, client: TestClient) -> None:
        body = _tree(client, "pkg").json()
        paths = {entry["path"] for entry in body["entries"]}
        assert paths == {"pkg/core.py", "pkg/empty.py"}

    def test_skips_noise_directories(self, client: TestClient) -> None:
        """node_modules / .git / __pycache__ 不出现在结果里。"""
        paths = {entry["path"] for entry in _tree(client).json()["entries"]}
        assert "node_modules" not in paths
        assert "__pycache__" not in paths

    def test_marks_directories_and_counts_files(self, client: TestClient) -> None:
        entries = {e["path"]: e for e in _tree(client).json()["entries"]}
        assert entries["pkg"]["is_dir"] is True
        assert entries["pkg"]["file_count"] == 2
        assert entries["README.md"]["is_dir"] is False

    def test_missing_directory_reports_reason(self, client: TestClient) -> None:
        response = _tree(client, "nope")
        assert response.status_code == 404
        assert response.json()["reason"] == "directory_not_found"

    def test_file_path_rejected_as_tree_root(self, client: TestClient) -> None:
        response = _tree(client, "README.md")
        assert response.status_code == 400
        assert response.json()["reason"] == "not_a_directory"

    def test_empty_repo_returns_empty_entries(self, tmp_path: Path) -> None:
        """空仓库返回空列表而非报错——界面据此显示「无可显示文件」。"""
        empty = tmp_path / "repos" / "empty__repo"
        empty.mkdir(parents=True)
        with TestClient(_app_with_task(tmp_path, empty)) as client:
            body = _tree(client).json()
        assert body["entries"] == []

    def test_tree_escape_rejected(self, client: TestClient) -> None:
        assert _tree(client, "../..").status_code == 400

    def test_cleared_workspace_reason_on_tree(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        app = _app_with_task(tmp_path, workspace)
        import shutil

        shutil.rmtree(workspace)
        with TestClient(app) as client:
            assert _tree(client).json()["reason"] == "workspace_cleared"

    def test_entry_cap_is_reported(self, tmp_path: Path) -> None:
        """超上限时截断并说明，不静默少给条目。"""
        wide = tmp_path / "repos" / "wide__repo"
        wide.mkdir(parents=True)
        for i in range(150):
            (wide / f"f{i:03d}.py").write_text("x", encoding="utf-8")
        with TestClient(_app_with_task(tmp_path, wide)) as client:
            body = _tree(client).json()
        assert len(body["entries"]) < 150
        assert "150" in body["truncated_note"]


class TestNoLlmOnRead:
    def test_reading_files_never_constructs_llm(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Covers AE-05, NA-07。连续打开 10 个文件，LLM 构造次数保持为 0。

        断言构造次数而非请求数：读文件路径上根本不该出现 provider，构造点比请求点更靠
        上游，钉住它能同时挡住「构造了但没调」这种将来容易被加进来的中间状态。
        """
        constructed: list[object] = []
        from backend.providers import llm as llm_module

        original = llm_module.LLMProvider.__init__

        def counted(self: Any, *args: Any, **kwargs: Any) -> None:
            constructed.append(self)
            original(self, *args, **kwargs)

        monkeypatch.setattr(llm_module.LLMProvider, "__init__", counted)

        for _ in range(10):
            assert _content(client, "pkg/core.py").status_code == 200
        assert constructed == []

    def test_tree_never_constructs_llm(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        constructed: list[object] = []
        from backend.providers import llm as llm_module

        original = llm_module.LLMProvider.__init__

        def counted(self: Any, *args: Any, **kwargs: Any) -> None:
            constructed.append(self)
            original(self, *args, **kwargs)

        monkeypatch.setattr(llm_module.LLMProvider, "__init__", counted)
        assert _tree(client).status_code == 200
        assert constructed == []
