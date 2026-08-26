"""分析历史的落盘与启动重建（U21，R-43~R-46、NA-02、KTD5、AE-12）。

用真实文件系统：要验证的是原子写入、损坏跳过与版本判定，mock 掉文件系统就只是在断言自己
写的假实现。
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.api.schemas import (
    FindingModel,
    LanguageProfileModel,
    ResultResponse,
    ReviewModel,
)
from backend.history.store import (
    MAX_INDEXED_ENTRIES,
    SCHEMA_VERSION,
    load_one,
    save,
    scan,
)


def _result(task_id: str = "t1", **overrides: object) -> ResultResponse:
    base: dict[str, object] = {
        "task_id": task_id,
        "repo": "acme/widget",
        "stage": "done",
        "completed": True,
        "failed": False,
        "commit_sha": "abcdef1234567890",
        "language_profile": LanguageProfileModel(total_files=33, parseable_files=30),
        "review": ReviewModel(
            findings=[
                FindingModel(
                    category="structural",
                    kind="x",
                    path="a.py",
                    line=1,
                    severity="high",
                    message="m",
                    evidence="e",
                )
            ]
        ),
    }
    base.update(overrides)
    return ResultResponse(**base)  # type: ignore[arg-type]


class TestSave:
    def test_creates_directory_on_first_write(self, tmp_path: Path) -> None:
        target = tmp_path / "analyses"
        assert save(target, _result())
        assert (target / "t1.json").is_file()

    def test_written_payload_carries_schema_version(self, tmp_path: Path) -> None:
        save(tmp_path, _result())
        raw = json.loads((tmp_path / "t1.json").read_text(encoding="utf-8"))
        assert raw["schema_version"] == SCHEMA_VERSION
        assert raw["result"]["task_id"] == "t1"

    def test_repeated_save_overwrites_not_appends(self, tmp_path: Path) -> None:
        save(tmp_path, _result(repo="old/name"))
        save(tmp_path, _result(repo="new/name"))

        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        raw = json.loads(files[0].read_text(encoding="utf-8"))
        assert raw["result"]["repo"] == "new/name"

    def test_no_credentials_in_payload(self, tmp_path: Path) -> None:
        """NA-02：凭证不进落盘。`ResultResponse` 里没有该字段，是结构性排除。"""
        save(tmp_path, _result())
        text = (tmp_path / "t1.json").read_text(encoding="utf-8")
        for word in ("api_key", "credentials", "sk-", "base_url"):
            assert word not in text

    def test_leaves_no_temp_files_behind(self, tmp_path: Path) -> None:
        """原子重命名：写完不该留下 .tmp 半成品。"""
        save(tmp_path, _result())
        assert [p.name for p in tmp_path.iterdir()] == ["t1.json"]

    def test_unwritable_directory_returns_false_without_raising(
        self, tmp_path: Path
    ) -> None:
        """持久化失败不该让一次已完成的分析变成失败。"""
        # 用一个文件占住目标路径：mkdir 会失败。
        blocked = tmp_path / "blocked"
        blocked.write_text("not a dir", encoding="utf-8")
        assert save(blocked, _result()) is False


class TestLoad:
    def test_round_trip_preserves_fields(self, tmp_path: Path) -> None:
        save(tmp_path, _result(), created_at=1_700_000_000.0)
        entry = load_one(tmp_path / "t1.json")

        assert entry is not None
        assert entry.result.task_id == "t1"
        assert entry.result.commit_sha == "abcdef1234567890"
        assert entry.result.language_profile is not None
        assert entry.result.language_profile.total_files == 33
        assert entry.created_at == 1_700_000_000.0

    def test_malformed_json_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "bad.json").write_text("{ 这不是 JSON", encoding="utf-8")
        assert load_one(tmp_path / "bad.json") is None

    def test_unknown_schema_version_returns_none(self, tmp_path: Path) -> None:
        """版本不认识与解析失败走同一条路径（R-44）。"""
        (tmp_path / "future.json").write_text(
            json.dumps({"schema_version": 999, "created_at": 1.0, "result": {}}),
            encoding="utf-8",
        )
        assert load_one(tmp_path / "future.json") is None

    def test_missing_required_field_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "partial.json").write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "created_at": 1.0,
                    # 缺 task_id、repo 等必需字段。
                    "result": {"stage": "done"},
                }
            ),
            encoding="utf-8",
        )
        assert load_one(tmp_path / "partial.json") is None

    def test_non_dict_payload_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "list.json").write_text("[1, 2, 3]", encoding="utf-8")
        assert load_one(tmp_path / "list.json") is None


class TestScan:
    def test_missing_directory_yields_empty(self, tmp_path: Path) -> None:
        outcome = scan(tmp_path / "nope")
        assert outcome.entries == []
        assert outcome.corrupt_count == 0

    def test_one_corrupt_among_four_does_not_break_the_list(self, tmp_path: Path) -> None:
        """AE-12：1 个损坏、4 个正常时列表显示 4 条，损坏那条被跳过并计数。"""
        for index in range(4):
            save(tmp_path, _result(task_id=f"t{index}"), created_at=float(index))
        (tmp_path / "corrupt.json").write_text("{ broken", encoding="utf-8")

        outcome = scan(tmp_path)
        assert len(outcome.entries) == 4
        assert outcome.corrupt_count == 1

    def test_entries_sorted_newest_first(self, tmp_path: Path) -> None:
        save(tmp_path, _result(task_id="old"), created_at=1_000.0)
        save(tmp_path, _result(task_id="new"), created_at=2_000.0)

        outcome = scan(tmp_path)
        assert [e.result.task_id for e in outcome.entries] == ["new", "old"]

    def test_version_mismatch_counts_as_corrupt(self, tmp_path: Path) -> None:
        save(tmp_path, _result(task_id="good"))
        (tmp_path / "future.json").write_text(
            json.dumps({"schema_version": 42, "created_at": 1.0, "result": {}}),
            encoding="utf-8",
        )

        outcome = scan(tmp_path)
        assert len(outcome.entries) == 1
        assert outcome.corrupt_count == 1

    def test_ignores_non_json_files(self, tmp_path: Path) -> None:
        save(tmp_path, _result())
        (tmp_path / "notes.txt").write_text("irrelevant", encoding="utf-8")

        outcome = scan(tmp_path)
        assert len(outcome.entries) == 1
        assert outcome.corrupt_count == 0

    def test_index_is_bounded_and_keeps_newest(self, tmp_path: Path) -> None:
        """内存索引有上界，且截断在排序之后——留下的必须是最近的那些。

        没有上界的话，长期运行的部署会同时得到内存增长与响应体膨胀，而两者都不产生错误、
        只是越来越慢。
        """
        total = MAX_INDEXED_ENTRIES + 5
        for index in range(total):
            save(tmp_path, _result(task_id=f"t{index:04d}"), created_at=float(index))

        outcome = scan(tmp_path)
        assert len(outcome.entries) == MAX_INDEXED_ENTRIES
        # 最新的那条在最前，最早的 5 条被截掉。
        assert outcome.entries[0].result.task_id == f"t{total - 1:04d}"
        kept = {e.result.task_id for e in outcome.entries}
        assert "t0000" not in kept
        # 被截掉的条目仍在磁盘上——截断只影响索引，不删文件（KTD10）。
        assert (tmp_path / "t0000.json").is_file()
