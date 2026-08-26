"""API 的请求与响应形状。

**响应模型显式声明，不直接序列化内部 dataclass。** 内部类型（ArchitectureReport、
ReviewReport）会随实现演进，而 API 是对外契约（Interface Contracts：「工具名与参数一旦
发布即视为对外契约」）。中间隔一层让内部重构不破坏前端。

错误响应带可区分的 `reason` 字段（AE6）：界面要按「仓库不存在」、「无访问权限」、「超规模」
分别呈现，只给 HTTP 状态码不够——404 无法区分「仓库不存在」与「任务标识不存在」。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class LlmCredentials(BaseModel):
    """访客自带的 LLM 凭证（BR-004）。

    **只在请求里出现，不在任何响应里出现。** 这是结构性排除而非「记得删」：响应模型
    不含该字段，所以它进不了读结果、进不了落盘（落盘的是 `ResultResponse` 的形状）、
    也进不了 `/api/health` 摘要。

    四项可选、逐项回落到服务端配置。`api_key` 与 `base_url` 在提交分析时是必需的，
    但那道校验放在提交端点而非这里——pydantic 的 422 给不出指向设置页的 reason，而
    R-49 要求拒绝时引导用户去配置。
    """

    api_key: str = ""
    base_url: str = ""
    model_flash: str = ""
    model_pro: str = ""


class AnalyzeRequest(BaseModel):
    repo_url: str = Field(min_length=1, description="GitHub 仓库地址")
    credentials: LlmCredentials | None = None
    """访客凭证。缺失时提交被拒（R-68：拦截在提交阶段，不回落服务端凭证）。"""


class AnalyzeAccepted(BaseModel):
    """提交成功。分析在后台跑，用 task_id 拉进度与结果。"""

    task_id: str
    repo: str
    stage: str
    message: str
    queue_position: int = 0
    """排队位置（1-based）。0 表示已直接开始，未进队列（R-54 要求显示位置）。"""


class ErrorResponse(BaseModel):
    """错误响应。

    reason 用 RejectReason 的取值，让前端能按类型分别呈现而不必解析 message 文本
    ——文本会随文案调整而变，reason 是稳定契约。
    """

    reason: str
    message: str
    detail: str = ""


class CitationModel(BaseModel):
    path: str
    line: int | None = None
    end_line: int | None = None


class ClaimModel(BaseModel):
    text: str
    citations: list[CitationModel] = Field(default_factory=list)


class ReportSectionModel(BaseModel):
    key: str
    title: str
    claims: list[ClaimModel] = Field(default_factory=list)


class MissingPartsModel(BaseModel):
    """缺失部分（R11）。text 始终非空——空节与「没有这一节」在读者看来不同。"""

    text: str
    unparsed_files: int = 0
    skipped_modules: int = 0


class ReportModel(BaseModel):
    repo: str
    commit_sha: str = ""
    summary: str = ""
    sections: list[ReportSectionModel] = Field(default_factory=list)
    missing: MissingPartsModel
    validation_summary: str = ""
    unsupported_claims: list[str] = Field(default_factory=list)
    """无法核验的结论。非空即说明报告未完全达到 R7，前端应显式呈现。"""


class FindingModel(BaseModel):
    category: str
    kind: str
    path: str
    line: int
    severity: str
    message: str
    evidence: str


class CategoryOutcomeModel(BaseModel):
    """一类检查的执行情况。

    status 与 hit_count 都给：R17 要求「命中零条与未执行检查在产出中可区分」，
    只给发现列表的话两者都是空数组。
    """

    category: str
    status: str
    scope: str
    hit_count: int
    reason: str = ""


class ReviewModel(BaseModel):
    target_files: list[str] = Field(default_factory=list)
    outcomes: list[CategoryOutcomeModel] = Field(default_factory=list)
    findings: list[FindingModel] = Field(default_factory=list)


class IndexStatusModel(BaseModel):
    cache_hit: bool
    chunk_count: int
    identity: str = ""
    note: str = ""


class ModuleModel(BaseModel):
    """一个模块分组，附该模块的分析结论。

    summary 与 limitation 从 ModuleAnalysis 并进来而非单开一个响应字段：界面上它们
    总是与模块一起呈现（点节点看详情），分两个数组会让前端自己做一次按名字的连接，
    而那次连接的失败形态是「详情栏空着但没报错」。
    """

    name: str
    files: list[str] = Field(default_factory=list)
    internal_edges: int = 0
    external_edges: int = 0
    origin: str = ""
    """分组来历：directory / split / merged。让聚类结果可解释。"""
    summary: str = ""
    """模块分析结论的**原文**。R-11 禁止二次概括，所以这里逐字带过，不做摘要或截断。"""
    limitation: str = ""
    """非空表示该模块分析不完整，界面须标注。"""


class DependencyEdgeModel(BaseModel):
    """一条依赖边，方向是「导入方 -> 被导入方」。

    用显式的 source/target 而非 `dict[导入方, 被导入方列表]`：后者的方向靠字段命名与
    文档约定表达，而消费方（节点图）要的正是方向本身。显式两字段让方向在类型上就成立，
    序列化过程也不可能把它反转。
    """

    source: str
    target: str


class UnresolvedImportModel(BaseModel):
    target: str
    path: str
    line: int
    reason: str


class DependencyGraphModel(BaseModel):
    nodes: list[str] = Field(default_factory=list)
    edges: list[DependencyEdgeModel] = Field(default_factory=list)
    external: dict[str, list[str]] = Field(default_factory=dict)
    """仓库外依赖：原始 import 目标 -> 导入它的文件。"""
    unresolved: list[UnresolvedImportModel] = Field(default_factory=list)
    """看起来指向仓库内却没解析到的 import。与 external 区分：这是解析规则的缺口。"""
    cycles: list[list[str]] = Field(default_factory=list)
    granularity: str = "file"
    degraded_reason: str = ""
    """非空表示图已降级为目录级。R-13 要求显式标注，不静默以粗粒度呈现。"""


class LanguageProfileModel(BaseModel):
    """语言构成。by_language 是全量映射——截断多少项是界面职责。"""

    total_files: int = 0
    parseable_files: int = 0
    by_language: dict[str, int] = Field(default_factory=dict)


class FileContentModel(BaseModel):
    """一个文件的一段内容。

    `truncated` 与 `truncated_note` 都给：布尔量让界面决定要不要显示提示条，文案说清
    「共多少行、怎么继续读」。只给文案的话界面得判断空串，而那是在用字符串表达布尔量。
    """

    path: str
    start_line: int
    end_line: int
    total_lines: int
    content: str
    truncated: bool = False
    truncated_note: str = ""


class FileTreeEntryModel(BaseModel):
    path: str
    """仓库相对路径。用完整相对路径而非仅文件名——界面的跳转与高亮都按它匹配。"""
    is_dir: bool
    file_count: int = 0
    """目录的直接子文件数。给读者判断值不值得展开。文件条目为 0。"""


class FileTreeModel(BaseModel):
    root: str
    entries: list[FileTreeEntryModel] = Field(default_factory=list)
    truncated_note: str = ""


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, description="自然语言查询")
    limit: int = 8
    """返回条数。超出 1~20 时收敛而非报错，沿用 MCP 侧的语义。"""


class SearchHitModel(BaseModel):
    path: str
    start_line: int
    end_line: int
    symbol: str = ""
    """所属符号名。模块级切块为空串。"""
    content: str
    distance: float
    """距离越小越相关。原样给出而不换算成百分比——那会造出一个后端没有的数字。"""


class SearchResponse(BaseModel):
    """检索结果。

    `found` 与 `hits` 分开的理由同 `AnswerResponse`：命中为空是正常结果，而「未建索引」
    是另一件事，后者由 409 + `index_missing` 表达（AE-08 要求两者不混同）。
    """

    query: str
    found: bool
    hits: list[SearchHitModel] = Field(default_factory=list)
    reason: str = ""
    """`no_match` 表示检索无命中。命中时为空串。"""
    note: str = ""


class ResultResponse(BaseModel):
    """读结果。报告与评审发现一并返回（U12 的 Dependencies 明写这一点）。

    **扩展一律是加法（KTD4）。** 概览条与 Architecture 页需要的字段都挂在这里而不新开
    端点：一个页面发两次请求，两份响应的一致性就得由前端保证，而这些数据本来同源于一份
    `AnalysisState`。新增字段全部可选或带默认值，旧字段语义不变。
    """

    task_id: str
    repo: str
    stage: str
    completed: bool
    failed: bool
    error: str = ""
    queue_position: int = 0
    """排队位置（1-based）。0 表示已开跑或已终止。界面据此显示「排队中，第 N 位」。"""
    commit_sha: str = ""
    """从 report 提升到顶层：报告为 None 时概览条仍要显示它（R-06）。"""
    report: ReportModel | None = None
    review: ReviewModel | None = None
    index: IndexStatusModel | None = None
    modules: list[ModuleModel] = Field(default_factory=list)
    dependency_graph: DependencyGraphModel | None = None
    language_profile: LanguageProfileModel | None = None
    module_failures: list[str] = Field(default_factory=list)
    """未完成分析的模块及原因（AE4：报告须标注缺失）。"""


class AnalysisSummary(BaseModel):
    """历史列表的一条（U9、U21）。

    取代原 `TaskSummary`：在它的 5 个字段上加法扩展 commit 短 SHA、时间、文件数、发现数。
    旧模型已无消费方，一并删除——留着会让下一个读者不确定该用哪个。
    **不含任何评分字段**（NA-01）——列表是最容易「顺手加个星级」的地方。

    `created_at` 是 Unix 秒而非格式化字符串：格式化是界面职责（时区与语言由浏览器决定），
    后端给字符串会让「本地时间」变成服务器时间。
    """

    task_id: str
    repo: str
    stage: str
    completed: bool
    failed: bool
    commit_sha: str = ""
    created_at: float = 0.0
    file_count: int = 0
    finding_count: int = 0




class QuestionRequest(BaseModel):
    question: str = Field(min_length=1)
    credentials: LlmCredentials | None = None
    """问答同样用访客凭证。为空时回落到该任务提交时携带的凭证（刷新页面后仍可提问）。"""


class QaCitationModel(BaseModel):
    path: str
    start_line: int
    end_line: int
    symbol: str = ""
    distance: float


class AnswerResponse(BaseModel):
    """问答结果。

    found 与 answer 分开的理由同 rag.qa.Answer：未找到时 answer 是说明而非回答，
    混同会让前端无法判断该不该显示引用区（R14）。
    """

    question: str
    found: bool
    answer: str
    citations: list[QaCitationModel] = Field(default_factory=list)
    note: str = ""
