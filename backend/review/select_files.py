"""待检文件挑选。KTD10：依赖图中心度 + 提交触碰频次。

这是 Goal Capsule 点名的决策焦点之一——挑选策略决定评审发现是真问题还是泛泛之谈。
全量检查在千文件仓库上不可行（错误处理与安全类要过 LLM 判断，成本与时间都乘以文件
数），所以「查哪些」直接决定产出质量。

两个信号各自表达什么：

  中心度   被依赖多，出问题影响面大。来自 U4 的 PageRank，已含传递性。
  churn    被反复修改，通常是复杂度与债务的聚集地。

为什么是这两个而不是别的：两者都从已有数据算出，不需要额外 LLM 调用，且各自的含义
能向人解释清楚。「LLM 觉得哪些文件可疑」这类信号无法复现也无法核验。

**churn 用 `git log --name-only` 数提交触碰次数，不用 `--numstat` 的行数增删。**
KTD10 给的理由是前者只需 tree 对象、后者需要历史 blob——这对有界浅克隆有实质差别。
另有一层：「哪些文件是债务聚集地」用变更频率就够，代码可维护性研究通常也用频率而非
行数；行数还会被格式化提交、文件搬移、生成物更新放大。

（计划 U11 的 Approach 行写的是 `--numstat`，与 KTD10 矛盾。按 KTD10 实现——它是
权威技术决定且带论证。）
"""

from __future__ import annotations

import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from backend.ingest.clone import git_env

# 两个信号的默认权重。和为 1，便于把综合分读作加权平均。
#
# 中心度略重于 churn 的理由：中心度直接对应「出问题影响面大」，是评审要找的东西；
# churn 高的文件也可能只是活跃开发中的正常迭代。权重作为参数暴露，KTD10 说明它要在
# 基准仓库实跑中调参。
DEFAULT_CENTRALITY_WEIGHT = 0.6
DEFAULT_CHURN_WEIGHT = 0.4


def collect_churn(
    repo_root: Path, max_commits: int = 500, timeout: float = 60.0
) -> dict[str, int]:
    """统计每个文件被多少个提交触碰过。

    `--name-only` 配合 `--pretty=format:` 让输出只有文件路径，逐行计数即得频次。
    同一提交内同一文件只出现一次，所以直接计数不会重复。

    失败返回空字典而非抛异常：churn 是两个信号之一，拿不到时挑选仍可只按中心度进行
    （见 rank_files 的降级说明）。让整次评审因为 git 不可用而失败是不合比例的。

    max_commits 有上限的理由：大仓库的完整历史可能有几十万提交，而浅克隆本来也只有
    有界深度（KTD12 的 clone_depth）。这里的上限是第二道保险。
    """
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                f"--max-count={max_commits}",
                "--name-only",
                "--pretty=format:",
                "--no-renames",
            ],
            cwd=str(repo_root),
            env=git_env(),
            capture_output=True,
            # 断开 stdin：子进程继承父进程的 stdin 会在 stdio 服务里吞掉协议字节。
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    if result.returncode != 0:
        return {}

    counter: Counter[str] = Counter()
    for line in result.stdout.splitlines():
        path = line.strip()
        if path:
            counter[path] += 1
    return dict(counter)


def _normalize(values: dict[str, float]) -> dict[str, float]:
    """把分数线性缩放到 [0, 1]。

    必须归一化才能加权：中心度是概率分布（千文件仓库上每个约 0.001），churn 是次数
    （可到几百）。不归一化直接加权等于只按 churn 排序，中心度的权重形同虚设。

    全部相同时返回 0 而非 1：此时该信号不提供区分度，给 0 让另一个信号决定顺序，
    比给 1 让两者都饱和更合理。
    """
    if not values:
        return {}
    lowest = min(values.values())
    highest = max(values.values())
    span = highest - lowest
    if span <= 0:
        return {key: 0.0 for key in values}
    return {key: (value - lowest) / span for key, value in values.items()}


def rank_files(
    candidates: list[str],
    centrality: dict[str, float],
    churn: dict[str, int],
    centrality_weight: float = DEFAULT_CENTRALITY_WEIGHT,
    churn_weight: float = DEFAULT_CHURN_WEIGHT,
) -> list[tuple[str, float]]:
    """按综合分降序排列候选文件，返回 (路径, 分数)。

    churn 为空（git 不可用、或仓库无历史）时退化为纯中心度排序，而不是让全部分数塌成
    0。这个降级要能被调用方看见——select_review_targets 会在 scope 描述里写明。

    同分时按路径字典序，保证同输入同输出（测试场景明确要求确定顺序）。
    """
    if not candidates:
        return []

    centrality_scores = _normalize({path: centrality.get(path, 0.0) for path in candidates})
    churn_scores = _normalize({path: float(churn.get(path, 0)) for path in candidates})

    has_churn = any(churn.get(path) for path in candidates)
    if not has_churn:
        centrality_weight, churn_weight = 1.0, 0.0

    total_weight = centrality_weight + churn_weight
    if total_weight <= 0:
        centrality_weight, churn_weight, total_weight = 1.0, 0.0, 1.0

    scored = [
        (
            path,
            (
                centrality_weight * centrality_scores.get(path, 0.0)
                + churn_weight * churn_scores.get(path, 0.0)
            )
            / total_weight,
        )
        for path in candidates
    ]
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored


@dataclass(frozen=True)
class SelectionResult:
    """挑选结果与它的依据。

    带上 basis 的理由：评审产出要说明「查了什么范围」（R17），而「为什么是这些文件」
    是那个说明的一部分。只返回路径列表会让读者无法判断挑选是否合理。
    """

    targets: tuple[str, ...]
    basis: str
    churn_available: bool


def select_review_targets(
    candidates: list[str],
    centrality: dict[str, float],
    churn: dict[str, int],
    limit: int,
    centrality_weight: float = DEFAULT_CENTRALITY_WEIGHT,
    churn_weight: float = DEFAULT_CHURN_WEIGHT,
) -> SelectionResult:
    """挑出待检文件，并给出可陈述的挑选依据。"""
    ranked = rank_files(candidates, centrality, churn, centrality_weight, churn_weight)
    churn_available = any(churn.get(path) for path in candidates)

    if not ranked:
        return SelectionResult(targets=(), basis="无候选文件", churn_available=churn_available)

    if churn_available:
        basis = (
            f"从 {len(candidates)} 个文件中按「中心度 {centrality_weight:.0%} + "
            f"提交触碰频次 {churn_weight:.0%}」取前 {min(limit, len(ranked))} 个"
        )
    else:
        basis = (
            f"从 {len(candidates)} 个文件中按中心度取前 {min(limit, len(ranked))} 个；"
            "提交频次不可用（git 历史缺失或不可读），该信号未参与排序"
        )

    return SelectionResult(
        targets=tuple(path for path, _ in ranked[:limit]),
        basis=basis,
        churn_available=churn_available,
    )
