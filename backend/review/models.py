"""评审产出的数据类型。

两个设计约束来自需求，不是风格选择：

R16 要求每条发现附带路径、行号、问题类型与判断依据。所以 Finding 的这四个字段都是
必填——「依据」尤其不能可选，一条没有依据的发现无法被核验，而 Reviewer 的价值标准是
「宁可只报 5 条真问题」，不可核验的发现连真假都判定不了。

R17 与 AE2 要求「命中零条与未执行检查在产出中可区分」。这排除了「用空列表表示两者」
的做法——空列表既可能是查过没发现，也可能是没查。所以每类检查产出一份
CategoryOutcome，带明确的 status 与 scope 描述。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class FindingCategory(str, Enum):
    STRUCTURAL = "structural"
    """结构类：循环依赖、层级违规、超大模块、无引用文件。纯图计算。"""
    ERROR_HANDLING = "error_handling"
    """错误处理：裸 except、异常被吞、IO 缺错误处理、资源未关闭。"""
    SECURITY = "security"
    """安全可疑模式：硬编码密钥、SQL 拼接、不安全反序列化、命令拼接。"""


class Severity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class Finding:
    """一条评审发现。

    line 为 0 表示「问题属于文件或模块整体，不落在某一行」——循环依赖与超大模块就是
    这种。不用 None 是为了让排序与序列化少一个分支；0 在 1-based 行号体系里不可能是
    真实行号，语义无歧义。
    """

    category: FindingCategory
    kind: str
    """具体问题类型，如 `circular_dependency`、`bare_except`。比 category 细一级。"""
    path: str
    line: int
    message: str
    evidence: str
    """判断依据（R16）。要写「凭什么这么判」，而非重复 message。"""
    severity: Severity = Severity.MEDIUM
    related_paths: tuple[str, ...] = ()
    """牵涉的其他文件。循环依赖用它列出环上全部成员。"""


class CheckStatus(str, Enum):
    EXECUTED = "executed"
    """已执行。命中数由 findings 长度给出，可以是 0。"""
    SKIPPED = "skipped"
    """未执行。reason 必须说明为什么——缺前置数据、超出规模上限等。"""
    FAILED = "failed"
    """执行中出错。与 SKIPPED 区分：前者是决定不查，后者是想查但失败了。"""


@dataclass
class CategoryOutcome:
    """一类检查的执行结果。

    scope 是「查了什么范围」的人类可读描述，零命中时它承担全部信息量——AE2 明确要求
    零命中的产出要说明已检查的范围与模式集合，而不是留空。
    """

    category: FindingCategory
    status: CheckStatus
    scope: str
    findings: list[Finding] = field(default_factory=list)
    reason: str = ""
    """status 非 EXECUTED 时必须非空。"""

    @property
    def hit_count(self) -> int:
        return len(self.findings)

    def describe(self) -> str:
        """一行摘要。零命中与未执行在这里就必须读起来不同（R17）。"""
        if self.status is CheckStatus.EXECUTED:
            if self.findings:
                return f"{self.category.value}：命中 {self.hit_count} 条（范围：{self.scope}）"
            return f"{self.category.value}：已检查，命中 0 条（范围：{self.scope}）"
        return f"{self.category.value}：未执行（{self.status.value}）——{self.reason}"


class Confidence(str, Enum):
    """候选点的确定程度，决定 LLM 取舍时的倾向。

    分级的用处不是给读者看，而是控制 LLM 判断的默认立场：CERTAIN 的候选除非有明确
    反证否则应当报出，CONTEXTUAL 的候选默认不报、除非确实有问题。没有这个区分，
    LLM 会对所有候选一视同仁，把「裸 except」和「这次 IO 调用没包 try」当成同等
    可疑，而后者在大多数代码里是正常的（异常交给上层处理）。
    """

    CERTAIN = "certain"
    """判据本身已足够定性，如裸 except。误报只可能来自罕见的合理用法。"""
    CONTEXTUAL = "contextual"
    """需要看上下文才能判断，如 IO 调用未包 try——异常可能有意交给调用方。"""


@dataclass(frozen=True)
class Candidate:
    """一个待判断的候选点。

    这一层只做确定性定位，不下结论——按计划的分工，LLM 只做取舍不做发现，以压缩
    幻觉空间。所以候选点带的是「凭什么怀疑」（detector_evidence）与「判断需要的代码
    上下文」（snippet），由后续的判断层决定它是否成为 Finding。

    snippet 存在的理由：LLM 需要看代码才能判断该不该管，而让它自己去读文件会引入
    额外的工具调用与失败面。定位时顺手取出上下文，判断环节就只需一次调用。
    """

    category: FindingCategory
    kind: str
    path: str
    line: int
    snippet: str
    detector_evidence: str
    confidence: Confidence
    enclosing: str = ""
    """所在函数或方法名，为空表示模块级。帮助 LLM 判断异常该不该向上传。"""


@dataclass
class ReviewReport:
    """一次评审的完整产出。

    按类别组织而非扁平的发现列表：R17 要求逐类说明执行情况，扁平列表无法表达「这一类
    查了但零命中」。
    """

    target_files: tuple[str, ...] = ()
    """本次实际检查的文件。挑选依据见 select_files。"""
    outcomes: list[CategoryOutcome] = field(default_factory=list)

    @property
    def all_findings(self) -> list[Finding]:
        return [f for outcome in self.outcomes for f in outcome.findings]

    def summary_lines(self) -> list[str]:
        return [outcome.describe() for outcome in self.outcomes]
