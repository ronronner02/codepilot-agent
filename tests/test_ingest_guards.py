"""地址解析的准入测试。

重点是拒绝路径：这是不可信输入的第一道门，解析结果会流进 clone URL 与磁盘目录名。
"""

from __future__ import annotations

import pytest

from backend.ingest.guards import RejectReason, RepoRef, RepoRejected, parse_repo_url


class TestAcceptedForms:
    def test_plain_https_url(self) -> None:
        ref = parse_repo_url("https://github.com/pallets/click")
        assert ref == RepoRef(owner="pallets", name="click")

    def test_dot_git_suffix_stripped(self) -> None:
        ref = parse_repo_url("https://github.com/pallets/click.git")
        assert ref.name == "click"

    def test_trailing_slash_tolerated(self) -> None:
        assert parse_repo_url("https://github.com/pallets/click/").name == "click"

    def test_browser_tree_path_truncated(self) -> None:
        ref = parse_repo_url("https://github.com/pallets/click/tree/main/src")
        assert ref == RepoRef(owner="pallets", name="click")

    def test_names_with_dots_and_dashes(self) -> None:
        ref = parse_repo_url("https://github.com/some-org/my.project_v2")
        assert ref == RepoRef(owner="some-org", name="my.project_v2")


class TestRejectedForms:
    @pytest.mark.parametrize(
        "raw",
        [
            "https://github.com/../etc/passwd",
            "https://github.com/owner/..",
            "https://github.com//click",
            "https://github.com/pallets",
            "https://gitlab.com/owner/repo",
            "https://evil.com/github.com/owner/repo",
            "git@github.com:pallets/click.git",
            "pallets/click",
            "not-a-url-at-all",
            "",
        ],
    )
    def test_rejected_with_invalid_url_reason(self, raw: str) -> None:
        with pytest.raises(RepoRejected) as exc:
            parse_repo_url(raw)
        assert exc.value.reason is RejectReason.INVALID_URL

    def test_leading_dash_rejected(self) -> None:
        """以 `-` 开头的段会被 git 当成命令行选项。"""
        with pytest.raises(RepoRejected):
            parse_repo_url("https://github.com/-upload-pack/click")

    def test_backslash_in_name_rejected(self) -> None:
        with pytest.raises(RepoRejected):
            parse_repo_url("https://github.com/owner/na\\me")

    @pytest.mark.parametrize(
        "raw",
        [
            "https://github.com/owner/.",
            "https://github.com/owner/..git",
            "https://github.com/./repo",
        ],
    )
    def test_dot_only_segment_rejected(self, raw: str) -> None:
        """剥离 .git 后成为纯点的名字会让 clone_url 重新拼出 `..`。"""
        with pytest.raises(RepoRejected):
            parse_repo_url(raw)

    def test_dotfile_repo_name_still_accepted(self) -> None:
        """`.github` 是 GitHub 上真实的仓库名，纯点检查不能误拒它。"""
        assert parse_repo_url("https://github.com/some-org/.github").name == ".github"


class TestRepoRef:
    def test_workdir_name_has_no_path_separator(self) -> None:
        ref = RepoRef(owner="some-org", name="my.project")
        assert "/" not in ref.workdir_name
        assert "\\" not in ref.workdir_name

    def test_clone_url_shape(self) -> None:
        ref = RepoRef(owner="pallets", name="click")
        assert ref.clone_url == "https://github.com/pallets/click.git"
