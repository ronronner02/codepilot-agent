"""双门限的阈值判断。

阈值锚在实测数据上：click 79 个可解析文件 / 5103 KB；fastapi 1138 / 55589；
django 2929 / 282129。默认上限 1500 个可解析文件让前两个通过、django 被拒。
"""

from __future__ import annotations

import pytest

from backend.config import Settings
from backend.ingest.admission import check_thresholds
from backend.ingest.github_api import RepoMetadata
from backend.ingest.guards import RejectReason, RepoRef, RepoRejected
from backend.ingest.tree import FileInventory

REF = RepoRef(owner="pallets", name="click")


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"deepseek_api_key": "k", "_env_file": None}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _meta(size_kb: int) -> RepoMetadata:
    return RepoMetadata(
        size_kb=size_kb,
        default_branch="main",
        archived=False,
        is_fork=False,
        primary_language="Python",
    )


def _inv(parseable: int, total: int | None = None, truncated: bool = False) -> FileInventory:
    return FileInventory(
        total_files=total if total is not None else parseable * 2,
        parseable_files=parseable,
        truncated=truncated,
    )


class TestWithinThresholds:
    def test_click_scale_passes(self) -> None:
        check_thresholds(REF, _meta(5103), _inv(79, 166), _settings())

    def test_fastapi_scale_passes(self) -> None:
        """总文件数 3139 远超旧的 3000 总数上限，但可解析仅 1138 —— 应通过。"""
        check_thresholds(REF, _meta(55589), _inv(1138, 3139), _settings())

    def test_exactly_at_parseable_limit_passes(self) -> None:
        check_thresholds(REF, _meta(1000), _inv(1500), _settings(max_parseable_files=1500))


class TestRejections:
    def test_django_scale_rejected_on_parseable_gate(self) -> None:
        with pytest.raises(RepoRejected) as exc:
            check_thresholds(REF, _meta(282129), _inv(2929, 7085), _settings())
        assert exc.value.reason is RejectReason.TOO_LARGE
        assert "2929" in exc.value.detail

    def test_size_gate_rejects_independently(self) -> None:
        """可解析文件少但体积巨大——近期提交里有大二进制的形态。"""
        with pytest.raises(RepoRejected) as exc:
            check_thresholds(REF, _meta(500_000), _inv(50), _settings())
        assert exc.value.reason is RejectReason.TOO_LARGE
        assert "KB" in exc.value.detail

    def test_truncated_inventory_rejected(self) -> None:
        with pytest.raises(RepoRejected) as exc:
            check_thresholds(REF, _meta(1000), _inv(10, truncated=True), _settings())
        assert exc.value.reason is RejectReason.TOO_LARGE

    def test_one_over_parseable_limit_rejected(self) -> None:
        with pytest.raises(RepoRejected):
            check_thresholds(
                REF, _meta(1000), _inv(1501), _settings(max_parseable_files=1500)
            )
