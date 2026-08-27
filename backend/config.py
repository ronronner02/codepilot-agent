"""集中式配置。在应用启动时构造一次并注入，各模块不重复读环境变量。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    deepseek_api_key: str = Field(min_length=1)
    deepseek_base_url: str = "https://api.deepseek.com"
    llm_model_flash: str = "deepseek-v4-flash"
    llm_model_pro: str = "deepseek-v4-pro"

    embedding_provider: Literal["local", "api"] = "local"
    embedding_local_model: str = "jinaai/jina-embeddings-v2-base-code"
    embedding_api_key: str = ""
    embedding_api_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_api_model: str = "text-embedding-v3"

    workspace_root: Path = Path("./.workspace")

    # 双门限。主门是可解析文件数（Python + TypeScript）——它直接对应解析与
    # embedding 的成本；副门是 git 对象体积，粗略拦住即使浅克隆也过大的仓库。
    # 实测参照：click 79 个可解析文件 / 5MB；fastapi 1138 / 55MB；
    # django 2929 / 282MB。总文件数与可解析文件数差异很大（fastapi 共 3139 个
    # 文件，其余是测试固件、文档、翻译），所以按总数设限会误拒有效基准仓库。
    #
    # **门限从 1500 / 300MB 放宽到 8000 / 1.5GB**：原值把 django 这一档（2929 个可解析
    # 文件）挡在外面，而那正是值得分析的项目规模。取 8000 而非更小的数是为了容下 TS
    # 项目——它们的文件数天然高于同规模 Python 项目（实测 nest 1904、refine 6810），
    # 定在 6000 会把 refine 这类仓库又挡回去。
    #
    # 放宽的代价只落在一处：**embedding**。它是唯一按量付费且随文件数线性增长的环节。
    # 实测 fastapi 的 1138 个文件产出 719 块、12 次 embedding 调用、索引耗时 278s；
    # 线性外推 8000 文件约 5000 块、约 32 分钟。其余环节不随规模涨——模块分析的扇出
    # 恒定为 max_fanout_width（12 个模块），评审只查挑选出的文件，解析与切块是本地 CPU。
    #
    # 索引按 repo + commit + provider 缓存，所以这笔成本每个 commit 只付一次；二次分析
    # 同一 commit 秒级命中。
    #
    # 再往上放要先解决的不是门限而是别的：GitHub tree API 在约 10 万条目处截断
    # （`inventory.truncated` 已挡住），而单次分析超过半小时会让「提交后等结果」这个
    # 交互形态本身不成立——那需要断点续跑，属于独立工作。
    max_parseable_files: int = 8_000
    max_repo_size_kb: int = 1_500_000

    max_file_bytes: int = 1_048_576
    max_fanout_width: int = 12

    # 送进向量索引的文件数上限。0 表示不限（全量索引，默认）。
    #
    # **为什么需要这个旋钮**（端到端实跑暴露）：此前代码里没有抽样开关，索引节点拿到的是
    # `[f.path for f in outcome.parsed]`——全量可解析文件。所以「跑一次完整分析」必然连带
    # 全量向量化：fastapi 的 1138 个文件产出 6529 个切块、105 批 embedding、30.5 分钟，
    # 占整次分析 36.6 分钟里的 83%。而此前的基准记录写「只索引前 60 个文件」，那是当次
    # 跑法的限制，不是代码行为——两者的出入正是这个缺口的表现。
    #
    # 默认仍是全量（0）：抽样会让检索漏内容，而漏了不报错。要把它作为默认就等于默认接受
    # 「检索质量不确定」。设成开关的用途是让 owner 在明确权衡后压成本——例如只想看架构
    # 报告、不打算用问答与检索时。
    #
    # 抽样按中心度降序取前 N（cluster 已算好），不按路径序：路径序是任意的，而中心度高的
    # 文件恰是问答最可能问到的。抽样上限会进缓存键（见 cache/key.py 的 index_scope），
    # 所以抽样索引不会被后续的全量分析误当成完整索引命中。
    max_index_files: int = 0

    # 依赖图的节点数上限。超出则降级为目录级分组并标注（不静默失败也不耗尽资源）。
    #
    # 与 max_parseable_files 同值（8000）。**这个对齐是有意的**：依赖图是模块聚类的输入，
    # 文件级粒度给出的聚类质量明显好于目录级——后者把「哪些文件真的互相依赖」这个信号
    # 预先折叠掉了。若图上限低于准入门限，则凡是介于两者之间的仓库都会拿到目录级的架构
    # 视图，而它们恰恰是最需要看清结构的那一档。
    #
    # 放大到 6000 的代价可算：环检测是迭代式 DFS（O(V+E)，不吃递归栈），import 反查是
    # 索引上的常数级查找，两者都线性于规模；8000 节点的边集与反查索引在几十 MB 量级。
    #
    # 降级路径因此回到它本来的用途：兜住直接调用解析层（MCP 工具、离线脚本）时的病态
    # 输入，而不是常规仓库的必经之路。
    max_graph_nodes: int = 8_000

    # 同时在途的 LLM 请求数上限。
    #
    # KTD16 说「DeepSeek 的并发上限（flash 2500、pro 500）在本项目规模下不是约束」，
    # 那是按直连估的。实测经中转网关时 **3 路并发就触发 Cloudflare 524（上游超时）与
    # `Server disconnected without sending a response`**，四次重试全撞在饱和的网关上，
    # 模块分析随之失败。
    #
    # 取 2 是保守值：扇出宽度上限仍是 12（那是成本约束），但同时在途的请求由这里限住。
    # 换成直连或更稳的端点时可调高——它与扇出宽度是两个独立的旋钮，混为一谈会让「降低
    # 成本」和「避开网关限制」互相干扰。
    #
    # **语义已随访客凭证透传改变（KTD3）：这是「同一凭证身份的在途上限」，不再是「本
    # provider 实例的在途上限」。** provider 改成按请求构造之后，实例级信号量会失去全局
    # 约束力（每个任务各有一个池子），所以闸门提到进程级并按 base_url + key 的摘要分池。
    # 按旧语义推算会低估实际在途数。
    llm_max_concurrency: int = 2

    # 全进程的 LLM 在途请求总上限，跨全部凭证身份。
    #
    # 与 llm_max_concurrency 是两个不同的资源维度：后者约束单个上游网关的压力（实测的
    # 饱和点在那里），这一条约束本机出口的总量（连接数、内存、带宽）。只有前者的话，
    # 多个不同 base_url 的访客同时在跑时总在途数没有上界。
    #
    # 取 llm_max_concurrency 的两倍作为默认：单访客场景下它不产生额外限制，多访客时
    # 才开始起作用。
    llm_global_max_concurrency: int = 4

    # 克隆深度。设 1 会让提交频次信号恒为空（KTD12）；实测中型仓库设 200 可拿到
    # 上千提交、体积仅增约 2M。
    clone_depth: int = 200

    # ---- 公网形态下的三项资源保护（U6、U7）。三者各管一种耗尽方式，互不替代。----
    #
    # 命名上刻意与 llm_max_concurrency 区分开：那一条是 **LLM 在途请求数**，下面这条是
    # **分析任务数**。同名不同物，混为一谈会让人以为调一个就够。

    # 同一 IP 在一个时间窗内可提交的分析次数。
    #
    # 取 3 次/小时：一次分析要克隆仓库、解析、向量化、跑十余次 LLM 调用，是分钟级的重活。
    # 对真实评审者来说「看几个仓库」远低于这个量；对刷接口的脚本来说它是硬墙。
    #
    # 客户端 IP 优先取 X-Forwarded-For 最左项。反代不透传真实 IP 时全站共用一个配额——
    # 那是部署前提（README 的公网部署一节），不是这里能兜住的。
    submissions_per_window: int = 3
    submission_window_seconds: int = 3600

    # 同时在跑的分析任务数上限。超出的任务进队列而非被拒（R-54）。
    #
    # 取 2：单机单 worker 下，两个分析已经让 LLM 在途请求、解析 CPU 与磁盘写入同时吃紧。
    # 再多不会更快完成，只会让每个都变慢并抬高 524 的概率。
    max_concurrent_analyses: int = 2

    # 排队上限。队列本身也是资源——无界队列会让「排到第 400 位」这种毫无意义的等待成立。
    max_queued_analyses: int = 8

    # 仓库工作副本的磁盘配额（字节）。达到后按最近最少使用清理非在跑副本（U7）。
    #
    # 默认 20GB 是待部署机器确认的假设值（见计划 Assumptions 与 Open Questions）。
    # 做成配置项，改值不改代码。
    repos_quota_bytes: int = 20 * 1024**3

    # 单次清理的最低回收量占配额的比例。
    #
    # 这道下限防的是「清理与克隆互相追赶」（KTD6）：配额贴顶时每次新分析清掉上一个副本，
    # 下次分析同一仓库又要重新克隆，磁盘始终贴顶而克隆成本被反复付出。回收量低于该比例
    # 时不清理并记 warning，让「配额太小以致放不下两个仓库」这类配置错误以明确失败暴露，
    # 而不是表现为「系统能跑但一直在克隆」。
    quota_min_reclaim_ratio: float = 0.10

    # 可选。未认证时 GitHub API 速率为 60 次/小时，且私有仓库与不存在的仓库同样
    # 返回 404——带 token 才能区分（403 vs 404），速率也提到 5000 次/小时。
    github_token: str = ""

    # 允许跨域的前端源。开发期 Vite 在 5173，容器部署时前端由 nginx 在 80 提供。
    #
    # 不用通配符：这个 API 能触发克隆仓库与 LLM 调用（有成本），通配会让任意站点都能
    # 消耗这些资源。部署到公网时应改成实际域名。
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost",
    ]

    langsmith_api_key: str = ""
    langsmith_project: str = "codepilot-agent"
    langsmith_tracing: bool = False

    @field_validator("embedding_api_key")
    @classmethod
    def _api_key_required_for_api_provider(cls, v: str, info: object) -> str:
        # provider 为 api 时缺 key 应尽早失败，而不是等到第一次向量化才报错。
        data = getattr(info, "data", {})
        if data.get("embedding_provider") == "api" and not v:
            raise ValueError(
                "EMBEDDING_PROVIDER=api 需要 EMBEDDING_API_KEY，当前为空"
            )
        return v

    @property
    def repos_dir(self) -> Path:
        return self.workspace_root / "repos"

    @property
    def index_dir(self) -> Path:
        return self.workspace_root / "index"

    @property
    def analyses_dir(self) -> Path:
        """落盘的分析历史（U21）。

        与 repos_dir 分开的理由是失效条件不同：仓库副本可被配额清理（腾磁盘），而历史
        JSON 是「这次分析发生过、结果是什么」的记录，清了它历史列表就空了（KTD10）。
        """
        return self.workspace_root / "analyses"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
