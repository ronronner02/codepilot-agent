"""API 的请求与响应形状。

**响应模型显式声明，不直接序列化内部 dataclass。** 内部类型（ArchitectureReport、
ReviewReport）会随实现演进，而 API 是对外契约（Interface Contracts：「工具名与参数一旦
发布即视为对外契约」）。中间隔一层让内部重构不破坏前端。

错误响应带可区分的 `reason` 字段（AE6）：界面要按「仓库不存在」、「无访问权限」、「超规模」
分别呈现，只给 HTTP 状态码不够——404 无法区分「仓库不存在」与「任务标识不存在」。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AnalyzeRequest(BaseModel):
    repo_url: str = Field(min_length=1, description="GitHub 仓库地址")


class AnalyzeAccepted(BaseModel):
    """提交成功。分析在后台跑，用 task_id 拉进度与结果。"""

    task_id: str
    repo: str
    stage: str
    message: str


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


class ResultResponse(BaseModel):
    """读结果。报告与评审发现一并返回（U12 的 Dependencies 明写这一点）。"""

    task_id: str
    repo: str
    stage: str
    completed: bool
    failed: bool
    error: str = ""
    report: ReportModel | None = None
    review: ReviewModel | None = None
    index: IndexStatusModel | None = None
    module_failures: list[str] = Field(default_factory=list)
    """未完成分析的模块及原因（AE4：报告须标注缺失）。"""


class TaskSummary(BaseModel):
    task_id: str
    repo: str
    stage: str
    completed: bool
    failed: bool


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1)


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
