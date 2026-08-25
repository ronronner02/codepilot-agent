/**
 * 后端 API 的响应形状。与 backend/api/schemas.py 对应。
 *
 * 手写而非从 OpenAPI 生成：这个项目只有一个前端消费者，生成器引入的构建步骤与依赖
 * 超过它省下的工作量。代价是两边可能漂移，所以字段名严格照抄后端，不做驼峰转换——
 * 转换层是漂移最容易藏身的地方。
 */

/** 分析阶段。与 backend/api/progress.py 的 Stage 枚举一致。 */
export type Stage =
  | 'queued'
  | 'fetching'
  | 'parsing'
  | 'skeleton_ready'
  | 'planning'
  | 'analyzing_modules'
  | 'indexing'
  | 'reviewing'
  | 'report_ready'
  | 'done'
  | 'failed'

export interface ProgressEvent {
  stage: Stage
  label: string
  detail: string
  /** 阶段内部的推进未必可量化，给不出时为 null——界面显示不确定态而非假装精确。 */
  percent: number | null
  failed: boolean
}

export interface AnalyzeAccepted {
  task_id: string
  repo: string
  stage: Stage
  message: string
}

/**
 * 错误响应。
 *
 * reason 是稳定契约，message 是会变的文案——界面按 reason 分类呈现（AE6），
 * 不解析 message 文本。
 */
export interface ApiError {
  reason: string
  message: string
  detail?: string
}

export interface Citation {
  path: string
  line: number | null
  end_line: number | null
}

export interface Claim {
  text: string
  citations: Citation[]
}

export interface ReportSection {
  key: string
  title: string
  claims: Claim[]
}

export interface MissingParts {
  text: string
  unparsed_files: number
  skipped_modules: number
}

export interface Report {
  repo: string
  commit_sha: string
  summary: string
  sections: ReportSection[]
  missing: MissingParts
  validation_summary: string
  /** 无法核验的结论。非空即说明报告未完全达到可追溯要求，界面须显式呈现。 */
  unsupported_claims: string[]
}

export interface Finding {
  category: string
  kind: string
  path: string
  line: number
  severity: string
  message: string
  evidence: string
}

export interface CategoryOutcome {
  category: string
  status: string
  scope: string
  hit_count: number
  reason: string
}

export interface Review {
  target_files: string[]
  outcomes: CategoryOutcome[]
  findings: Finding[]
}

export interface IndexStatus {
  cache_hit: boolean
  chunk_count: number
  identity: string
  note: string
}

export interface AnalysisResult {
  task_id: string
  repo: string
  stage: Stage
  completed: boolean
  failed: boolean
  error: string
  report: Report | null
  review: Review | null
  index: IndexStatus | null
  module_failures: string[]
}

export interface QaCitation {
  path: string
  start_line: number
  end_line: number
  symbol: string
  distance: number
}

export interface Answer {
  question: string
  /** found 与 answer 分开：未找到时 answer 是说明而非回答，混同会让界面显示空引用区。 */
  found: boolean
  answer: string
  citations: QaCitation[]
  note: string
}
