"""单轮代码问答（R12、R13、R14、AE5）。

单轮而非多轮：Product Contract 的决定——省下的工时投入 Reviewer，会话状态管理在三条链路
里技术含量最低。所以这里没有会话记忆，也不做 query 改写。

三个设计点：

**相似度阈值把「没有相关代码」与「有」分开（AE5）。** Chroma 返回 k 个最近邻，不管有多远
——U9 的基准实跑演示了后果：查「用户认证用了哪些策略」在没有认证代码的索引里返回了
`getPMT()`、`getP()`（距离 0.48-0.51）。把它们当答案喂给 LLM，就会基于金融计算函数编造
认证机制。阈值之下直接返回未找到，一次 LLM 调用都不发。

**引用由代码从元数据组装，不由模型生成（R13）。** 模型用「片段 N」指代来源，代码再映射
回路径与行号。模型编不出它没看到的编号，也就编不出幻觉路径。

**上下文预算按字符算并保序。** 检索结果按相似度降序，预算用尽即停——最相关的片段优先进
上下文，而不是被后面的长片段挤掉。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from backend.cache.key import CacheKey
from backend.graph.prompts.qa import SYSTEM_PROMPT, build_question_message
from backend.providers.embedding import EmbeddingProvider
from backend.providers.llm import LLMProvider
from backend.rag.store import SearchHit, VectorStore

logger = logging.getLogger("codepilot.qa")

# 距离阈值。cosine 距离，越小越相关。
#
# 取 0.45 的依据来自基准仓库实测（docs/2026-08-25-u9-index-benchmark.txt）：
#   0.26  查「OpenAPI schema 怎么生成」命中 FastAPI.openapi —— 正是那个方法
#   0.37-0.41  查「依赖注入怎么解析」命中 Depends / Security —— 相关
#   0.48-0.51  查「认证用了哪些策略」命中 getPMT / getP —— 完全无关
# 两组之间有清晰间隔，0.45 落在中间。
#
# 这是启发式阈值，且与 embedding 模型绑定：换 provider 后距离分布会变，需重新实测。
DEFAULT_DISTANCE_THRESHOLD = 0.45

# 进入上下文的片段数上限。
#
# 取 6 的依据：多数代码问题的答案集中在两三个位置，6 个留出余量；再多则相关性最低的几个
# 片段会稀释模型的注意力，而它们本来也帮不上忙。
DEFAULT_MAX_SNIPPETS = 6

# 上下文的字符预算。
#
# 取 24000 字符（约 8K token）：给模型留足回答空间，也避免长片段把预算吃光。
DEFAULT_CONTEXT_BUDGET = 24_000

# 单个片段的字符上限。超出则截断并标注。
#
# 存在的理由：一个 500 行的类头块能吃掉整个预算，让其余片段全被挤掉。截断它比丢掉其他
# 片段更好——问题的答案往往需要多个位置对照。
MAX_SNIPPET_CHARS = 6_000

_SNIPPET_REFERENCE = re.compile(r"片段\s*(\d+)")


@dataclass(frozen=True)
class Citation:
    """回答里的一条引用。全部字段来自检索块的元数据，无 LLM 参与。"""

    index: int
    path: str
    start_line: int
    end_line: int
    symbol: str
    distance: float

    def render(self) -> str:
        location = f"{self.path}:{self.start_line}-{self.end_line}"
        return f"[片段 {self.index}] {location}" + (f"（{self.symbol}）" if self.symbol else "")


@dataclass
class Answer:
    """一次问答的结果。

    found 与 answer 分开：未找到时 answer 是说明文字而非回答，两者混同会让调用方无法
    判断该不该显示引用区（R14 要求「明确说明未找到」，而带引用的未找到说明会自相矛盾）。
    """

    question: str
    found: bool
    answer: str
    citations: tuple[Citation, ...] = ()
    llm_calls: int = 0
    searched: int = 0
    """检索返回的片段数（阈值过滤前）。"""
    best_distance: float | None = None
    note: str = ""
    referenced_indices: tuple[int, ...] = field(default_factory=tuple)
    """回答实际引用到的片段编号。未被引用的片段不进 citations——列出模型没用到的
    片段会让读者以为结论有更多依据。"""


def _truncate(content: str) -> tuple[str, bool]:
    if len(content) <= MAX_SNIPPET_CHARS:
        return content, False
    return content[:MAX_SNIPPET_CHARS] + "\n…[片段过长已截断]", True


def _select_snippets(
    hits: list[SearchHit],
    max_snippets: int,
    budget: int,
) -> list[tuple[int, SearchHit, str]]:
    """按相似度顺序选片段，预算用尽即停。

    保序是关键：最相关的先进，而不是让后面的长片段把预算吃光后挤掉更相关的。
    """
    selected: list[tuple[int, SearchHit, str]] = []
    used = 0
    for hit in hits[:max_snippets]:
        content, _ = _truncate(hit.content)
        if selected and used + len(content) > budget:
            break
        selected.append((len(selected) + 1, hit, content))
        used += len(content)
    return selected


def _referenced_indices(answer: str, available: set[int]) -> tuple[int, ...]:
    """从回答里提取被引用的片段编号。

    只认真实存在的编号：模型偶尔会写「片段 7」而实际只给了 5 个，那个编号无处可指。
    """
    found = {
        int(match)
        for match in _SNIPPET_REFERENCE.findall(answer)
        if int(match) in available
    }
    return tuple(sorted(found))


def _replace_references(answer: str, citations: dict[int, Citation]) -> str:
    """把「片段 N」替换成真实位置。

    替换而非追加的理由：读者在读到结论的那一句就该看到依据在哪，而不是回头对照文末的
    引用列表。
    """

    def substitute(match: re.Match[str]) -> str:
        index = int(match.group(1))
        citation = citations.get(index)
        if citation is None:
            return match.group(0)
        return f"{citation.path}:{citation.start_line}-{citation.end_line}"

    return _SNIPPET_REFERENCE.sub(substitute, answer)


async def answer_question(
    question: str,
    cache_key: CacheKey,
    index_dir: Path,
    embedding: EmbeddingProvider,
    provider: LLMProvider,
    distance_threshold: float = DEFAULT_DISTANCE_THRESHOLD,
    max_snippets: int = DEFAULT_MAX_SNIPPETS,
    context_budget: int = DEFAULT_CONTEXT_BUDGET,
) -> Answer:
    """回答一个代码问题。

    索引不存在时返回明确错误而非空回答——「这个仓库还没分析」与「没找到相关代码」对用户
    的下一步动作完全不同：前者该去发起分析，后者该换个问法。
    """
    cleaned = question.strip()
    if not cleaned:
        return Answer(
            question=question,
            found=False,
            answer="问题为空，无法检索。",
            note="empty_question",
        )

    store = VectorStore(index_dir, cache_key.digest)
    if not store.exists():
        return Answer(
            question=cleaned,
            found=False,
            answer=(
                f"该仓库尚未建立索引（{cache_key.describe()}），无法回答代码问题。"
                "请先对该仓库发起一次分析。"
            ),
            note="index_missing",
        )

    try:
        query_vector = (await embedding.embed_texts([cleaned]))[0]
    except Exception as exc:  # noqa: BLE001 — 向量化失败要说清，不能返回空回答
        return Answer(
            question=cleaned,
            found=False,
            answer=f"问题向量化失败（{type(exc).__name__}: {exc}），无法检索。",
            note="embedding_failed",
        )

    hits = store.search(query_vector, limit=max_snippets * 2)
    best = hits[0].distance if hits else None
    relevant = [hit for hit in hits if hit.distance <= distance_threshold]

    if not relevant:
        # AE5：不进入 LLM。基于无关片段生成回答就是编造——U9 实跑演示过：查认证策略
        # 命中了金融计算函数，喂给模型必然产出一段像样但错误的解释。
        detail = (
            f"最接近的片段距离 {best:.3f}，超过相关性阈值 {distance_threshold}"
            if best is not None
            else "检索未返回任何片段"
        )
        return Answer(
            question=cleaned,
            found=False,
            answer=f"未在该仓库中找到与问题相关的代码。{detail}。",
            searched=len(hits),
            best_distance=best,
            llm_calls=0,
            note="below_threshold",
        )

    selected = _select_snippets(relevant, max_snippets, context_budget)
    snippets = [
        (index, hit.symbol or f"{hit.path} 的模块级代码", content)
        for index, hit, content in selected
    ]

    try:
        response = await provider.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_question_message(cleaned, snippets)},
            ],
            tier="pro",
        )
        raw = (response.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        return Answer(
            question=cleaned,
            found=False,
            answer=f"生成回答失败（{type(exc).__name__}: {exc}）。检索到相关片段但未能作答。",
            searched=len(hits),
            best_distance=best,
            llm_calls=1,
            note="llm_failed",
        )

    if not raw:
        return Answer(
            question=cleaned,
            found=False,
            answer="模型返回空回答。检索到相关片段但未能作答。",
            searched=len(hits),
            best_distance=best,
            llm_calls=1,
            note="empty_answer",
        )

    all_citations = {
        index: Citation(
            index=index,
            path=hit.path,
            start_line=hit.start_line,
            end_line=hit.end_line,
            symbol=hit.symbol,
            distance=hit.distance,
        )
        for index, hit, _ in selected
    }

    referenced = _referenced_indices(raw, set(all_citations))
    # 模型没引用任何片段时保留全部引用：它可能用了别的措辞表达来源，而完全不给引用
    # 会让 R13 落空。宁可多给，不要一条都没有。
    kept = referenced or tuple(sorted(all_citations))

    return Answer(
        question=cleaned,
        found=True,
        answer=_replace_references(raw, all_citations),
        citations=tuple(all_citations[i] for i in kept),
        llm_calls=1,
        searched=len(hits),
        best_distance=best,
        referenced_indices=referenced,
        note=(
            "回答未显式引用片段编号，已列出全部检索片段作为依据"
            if not referenced
            else ""
        ),
    )
