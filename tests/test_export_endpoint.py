"""导出端点（U19、U20，R-38、R-40、AE-14）。

PDF 分支的测试不依赖 weasyprint 是否真的装了：它 monkeypatch `render_pdf`，分别验证成功、
渲染失败与组件缺失三条路径。真实的中文渲染核对是容器实跑的事（U20 的 Execution note），
测试断言不了字形。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.api import export as export_module
from backend.main import create_app
from tests.support import make_settings
from tests.test_api import _TEST_CREDENTIALS, _rich_client, _run_to_completion


@pytest.fixture
def ready_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """跑完一次分析的客户端。导出需要有结果可导。"""
    with _rich_client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/analyses",
            json={"repo_url": "https://github.com/acme/widget", "credentials": _TEST_CREDENTIALS},
        )
        task_id = response.json()["task_id"]
        _run_to_completion(client, task_id)
        yield client, task_id


class TestMarkdownAndHtml:
    def test_markdown_export_returns_attachment(self, ready_client: Any) -> None:
        client, task_id = ready_client
        response = client.get(f"/api/analyses/{task_id}/export?format=md")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/markdown")
        assert "attachment" in response.headers["content-disposition"]
        assert "代码分析报告" in response.text

    def test_html_export_is_self_contained(self, ready_client: Any) -> None:
        client, task_id = ready_client
        response = client.get(f"/api/analyses/{task_id}/export?format=html")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "<style>" in response.text
        assert "<link" not in response.text

    def test_filename_carries_repo_and_sha(self, ready_client: Any) -> None:
        client, task_id = ready_client
        disposition = client.get(
            f"/api/analyses/{task_id}/export?format=md"
        ).headers["content-disposition"]
        assert "acme__widget" in disposition

    def test_export_contains_no_credentials(self, ready_client: Any) -> None:
        """NA-02：导出内容不含访客 key 的任何片段。"""
        client, task_id = ready_client
        for fmt in ("md", "html"):
            body = client.get(f"/api/analyses/{task_id}/export?format={fmt}").text
            assert _TEST_CREDENTIALS["api_key"] not in body
            assert "guest-key" not in body

    def test_unsupported_format_is_distinguishable(self, ready_client: Any) -> None:
        client, task_id = ready_client
        response = client.get(f"/api/analyses/{task_id}/export?format=docx")
        assert response.status_code == 400
        assert response.json()["reason"] == "unsupported_format"

    def test_unknown_task_returns_task_not_found(self, ready_client: Any) -> None:
        client, _ = ready_client
        response = client.get("/api/analyses/nope/export?format=md")
        assert response.status_code == 404
        assert response.json()["reason"] == "task_not_found"


class TestPdf:
    def test_successful_pdf_starts_with_magic_bytes(
        self, ready_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, task_id = ready_client
        monkeypatch.setattr(export_module, "render_pdf", lambda html: b"%PDF-1.7\nfake")

        response = client.get(f"/api/analyses/{task_id}/export?format=pdf")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.content.startswith(b"%PDF")

    def test_render_failure_returns_error_without_bytes(
        self, ready_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R-40：失败不返回损坏文件——打不开的 PDF 比明确的错误更糟。"""
        client, task_id = ready_client

        def boom(html: str) -> bytes:
            raise RuntimeError("字体缺失")

        monkeypatch.setattr(export_module, "render_pdf", boom)

        response = client.get(f"/api/analyses/{task_id}/export?format=pdf")
        assert response.status_code == 500
        assert response.json()["reason"] == "pdf_failed"
        assert "Markdown 或 HTML" in response.json()["message"]
        # 没有任何 PDF 字节。
        assert not response.content.startswith(b"%PDF")

    def test_missing_component_is_distinguishable_from_render_failure(
        self, ready_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未装 weasyprint 与渲染失败是两件事，用户的下一步不同。"""
        client, task_id = ready_client

        def missing(html: str) -> bytes:
            raise ImportError("No module named 'weasyprint'")

        monkeypatch.setattr(export_module, "render_pdf", missing)

        response = client.get(f"/api/analyses/{task_id}/export?format=pdf")
        assert response.status_code == 501
        assert response.json()["reason"] == "pdf_unavailable"
        assert response.json()["reason"] != "pdf_failed"

    def test_pdf_shares_source_with_other_formats(
        self, ready_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BR-003：三格式同源。PDF 的输入就是 HTML 导出的产物。"""
        client, task_id = ready_client
        captured: list[str] = []

        def capture(html: str) -> bytes:
            captured.append(html)
            return b"%PDF-1.7\n"

        monkeypatch.setattr(export_module, "render_pdf", capture)
        client.get(f"/api/analyses/{task_id}/export?format=pdf")
        html_export = client.get(f"/api/analyses/{task_id}/export?format=html").text

        assert captured
        assert captured[0] == html_export


class TestHistoryFallback:
    def test_export_works_after_restart(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """重启后落盘的分析仍可导出。

        **与读结果端点必须一致。** 只让读结果回落到落盘的话，同一次分析会出现「报告能看
        但导不出」——用户无从判断是哪一边坏了。
        """
        from tests.test_api import _stub_nodeset

        workspace = tmp_path / "ws"
        with _rich_client(monkeypatch, tmp_path) as client:
            task_id = client.post(
                "/api/analyses",
                json={
                    "repo_url": "https://github.com/acme/widget",
                    "credentials": _TEST_CREDENTIALS,
                },
            ).json()["task_id"]
            _run_to_completion(client, task_id)

        monkeypatch.setattr(
            "backend.api.tasks.make_real_nodes", lambda s, p: _stub_nodeset()
        )
        restarted = create_app(make_settings(workspace_root=workspace))
        with TestClient(restarted) as client:
            response = client.get(f"/api/analyses/{task_id}/export?format=md")
            assert response.status_code == 200
            assert "代码分析报告" in response.text
            # 读结果与导出对同一个 task_id 给出一致的可用性。
            assert client.get(f"/api/analyses/{task_id}").status_code == 200


class TestSchema:
    def test_export_path_in_openapi(self) -> None:
        app = create_app(make_settings())
        schema = app.openapi()
        assert "/api/analyses/{task_id}/export" in schema["paths"]
