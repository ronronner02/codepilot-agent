"""待检文件挑选（KTD10：中心度 + 提交触碰频次）。

churn 用真实 git 仓库测，不 mock subprocess：这段代码的风险全在「git 输出格式是否
如我所设」，mock 掉正好把要验证的东西替换成了我的假设。构造一个几个提交的临时仓库
成本很低。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.review.select_files import (
    collect_churn,
    rank_files,
    select_review_targets,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo_with_history(tmp_path: Path) -> Path:
    """构造已知 churn 分布的仓库：hot.py 3 次、warm.py 2 次、cold.py 1 次。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")

    for name in ("hot.py", "warm.py", "cold.py"):
        (repo / name).write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "初始提交")

    (repo / "hot.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "warm.py").write_text("y = 2\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "第二次")

    (repo / "hot.py").write_text("x = 3\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "第三次")
    return repo


class TestChurn:
    def test_counts_commits_touching_each_file(self, repo_with_history: Path) -> None:
        churn = collect_churn(repo_with_history)
        assert churn["hot.py"] == 3
        assert churn["warm.py"] == 2
        assert churn["cold.py"] == 1

    def test_untouched_file_absent_from_result(self, repo_with_history: Path) -> None:
        (repo_with_history / "never_committed.py").write_text("z = 1\n", encoding="utf-8")
        assert "never_committed.py" not in collect_churn(repo_with_history)

    def test_max_commits_bounds_the_window(self, repo_with_history: Path) -> None:
        """只看最近 1 个提交时，只有那次触碰的文件被计入。"""
        churn = collect_churn(repo_with_history, max_commits=1)
        assert churn == {"hot.py": 1}

    def test_non_repo_returns_empty_not_raise(self, tmp_path: Path) -> None:
        """churn 拿不到时挑选仍可只按中心度进行，不该让整次评审失败。"""
        assert collect_churn(tmp_path / "not-a-repo") == {}

    def test_directory_without_git_returns_empty(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert collect_churn(plain) == {}


class TestRanking:
    def test_centrality_and_churn_both_influence_order(self) -> None:
        centrality = {"a.py": 0.9, "b.py": 0.1, "c.py": 0.5}
        churn = {"a.py": 1, "b.py": 100, "c.py": 50}
        ranked = dict(rank_files(["a.py", "b.py", "c.py"], centrality, churn))
        # a 中心度最高但 churn 最低，b 相反；c 两者居中。综合分应让 c 不垫底。
        assert ranked["c.py"] > min(ranked["a.py"], ranked["b.py"])

    def test_normalization_prevents_churn_from_dominating(self) -> None:
        """中心度是概率分布（千文件上每个约 0.001），churn 是次数（可到几百）。

        不归一化直接加权等于只按 churn 排序，中心度的权重形同虚设。
        """
        centrality = {"central.py": 0.002, "fringe.py": 0.0001}
        churn = {"central.py": 1, "fringe.py": 300}
        ranked = rank_files(
            ["central.py", "fringe.py"],
            centrality,
            churn,
            centrality_weight=0.9,
            churn_weight=0.1,
        )
        assert ranked[0][0] == "central.py", "高权重的中心度应能压过量纲更大的 churn"

    def test_missing_churn_falls_back_to_centrality_only(self) -> None:
        centrality = {"a.py": 0.1, "b.py": 0.9}
        ranked = rank_files(["a.py", "b.py"], centrality, {})
        assert [p for p, _ in ranked] == ["b.py", "a.py"]

    def test_uniform_signal_does_not_saturate(self) -> None:
        """全部相同的信号不提供区分度，应让另一个信号决定顺序。"""
        centrality = {"a.py": 0.5, "b.py": 0.5}
        churn = {"a.py": 1, "b.py": 9}
        ranked = rank_files(["a.py", "b.py"], centrality, churn)
        assert ranked[0][0] == "b.py"

    def test_ties_break_by_path_for_determinism(self) -> None:
        centrality = {"z.py": 0.5, "a.py": 0.5}
        churn = {"z.py": 3, "a.py": 3}
        assert [p for p, _ in rank_files(["z.py", "a.py"], centrality, churn)] == [
            "a.py",
            "z.py",
        ]

    def test_same_input_same_output(self) -> None:
        centrality = {f"f{i}.py": i / 10 for i in range(6)}
        churn = {f"f{i}.py": (i * 7) % 5 for i in range(6)}
        candidates = [f"f{i}.py" for i in range(6)]
        assert rank_files(candidates, centrality, churn) == rank_files(
            candidates, centrality, churn
        )

    def test_empty_candidates_returns_empty(self) -> None:
        assert rank_files([], {"a.py": 1.0}, {"a.py": 1}) == []

    def test_zero_weights_do_not_divide_by_zero(self) -> None:
        ranked = rank_files(
            ["a.py", "b.py"],
            {"a.py": 0.9, "b.py": 0.1},
            {"a.py": 1, "b.py": 2},
            centrality_weight=0.0,
            churn_weight=0.0,
        )
        assert len(ranked) == 2
        assert all(0.0 <= score <= 1.0 for _, score in ranked)


class TestSelection:
    def test_limit_respected(self) -> None:
        candidates = [f"f{i}.py" for i in range(10)]
        centrality = {p: 1.0 / (i + 1) for i, p in enumerate(candidates)}
        result = select_review_targets(candidates, centrality, {}, limit=3)
        assert len(result.targets) == 3

    def test_basis_states_both_signals_when_churn_present(self) -> None:
        """R17 要求说明检查范围，「为什么是这些文件」是那个说明的一部分。"""
        result = select_review_targets(
            ["a.py", "b.py"], {"a.py": 0.9, "b.py": 0.1}, {"a.py": 2, "b.py": 1}, limit=2
        )
        assert result.churn_available is True
        assert "中心度" in result.basis
        assert "频次" in result.basis

    def test_basis_discloses_churn_unavailable(self) -> None:
        """降级必须可见——读者要知道排序只用了一个信号。"""
        result = select_review_targets(["a.py"], {"a.py": 0.5}, {}, limit=1)
        assert result.churn_available is False
        assert "不可用" in result.basis
        assert "未参与排序" in result.basis

    def test_empty_candidates_reports_no_candidates(self) -> None:
        result = select_review_targets([], {}, {}, limit=5)
        assert result.targets == ()
        assert "无候选" in result.basis

    def test_end_to_end_with_real_repo(self, repo_with_history: Path) -> None:
        """真实 churn 数据下的挑选。hot.py 被改 3 次，应排在 cold.py 之前。"""
        churn = collect_churn(repo_with_history)
        candidates = ["hot.py", "warm.py", "cold.py"]
        centrality = {p: 0.33 for p in candidates}
        result = select_review_targets(candidates, centrality, churn, limit=2)
        assert result.targets[0] == "hot.py"
        assert "cold.py" not in result.targets
