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
  /** 排队位置（1-based）。0 表示已直接开始（R-54）。 */
  queue_position: number
}

/** 最近分析列表的一条（U9、U21）。不含任何评分字段（NA-01）。 */
export interface AnalysisSummary {
  task_id: string
  repo: string
  stage: Stage
  completed: boolean
  failed: boolean
  commit_sha: string
  /** Unix 秒。界面按它倒序并格式化显示（R-45）。 */
  created_at: number
  file_count: number
  finding_count: number
}

/** 文件的一段内容（U12）。 */
export interface FileContent {
  path: string
  start_line: number
  end_line: number
  total_lines: number
  content: string
  /** 截断必须可见，不静默（R-18）。 */
  truncated: boolean
  truncated_note: string
}

export interface FileTreeEntry {
  /** 仓库相对路径。跳转与高亮都按它匹配。 */
  path: string
  is_dir: boolean
  /** 目录的直接子文件数。文件条目为 0。 */
  file_count: number
}

export interface FileTree {
  root: string
  entries: FileTreeEntry[]
  truncated_note: string
}

export interface SearchHit {
  path: string
  start_line: number
  end_line: number
  /** 所属符号名。模块级切块为空串。 */
  symbol: string
  content: string
  /** 距离越小越相关。不换算成百分比——那会造出一个后端没有的数字。 */
  distance: number
}

export interface SearchResult {
  query: string
  /** found 与 hits 分开：「未命中」与「未建索引」是两件事，后者由 409 表达（R-25）。 */
  found: boolean
  hits: SearchHit[]
  reason: string
  note: string
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

/** 一个模块分组，附该模块的分析结论。summary 是原文，界面不得二次概括（R-11）。 */
export interface ModuleInfo {
  name: string
  files: string[]
  internal_edges: number
  external_edges: number
  /** 分组来历：directory / split / merged。 */
  origin: string
  summary: string
  /** 非空表示该模块分析不完整，界面须标注。 */
  limitation: string
}

/** 一条依赖边。方向是「导入方 → 被导入方」（R-09）。 */
export interface DependencyEdge {
  source: string
  target: string
}

export interface UnresolvedImport {
  target: string
  path: string
  line: number
  reason: string
}

export interface DependencyGraphInfo {
  nodes: string[]
  edges: DependencyEdge[]
  /** 仓库外依赖：原始 import 目标 → 导入它的文件。 */
  external: Record<string, string[]>
  /** 指向仓库内却没解析到的 import。与 external 区分：这是解析规则的缺口。 */
  unresolved: UnresolvedImport[]
  /** 导入期成立的环。延迟导入构成的环不在其中，见 call_time_cycles。 */
  cycles: string[][]
  /** 仅调用期成立的环：环上有函数作用域的延迟导入，导入期不成立。
   *  通常是作者主动规避循环依赖的手段，不是问题。 */
  call_time_cycles: string[][]
  granularity: string
  /** 非空表示已降级为目录级粒度，界面须显式标注（R-13）。 */
  degraded_reason: string
}

/** 语言构成。by_language 是全量映射，截断前几项是界面职责。 */
export interface LanguageProfile {
  total_files: number
  parseable_files: number
  by_language: Record<string, number>
}

export interface AnalysisResult {
  task_id: string
  repo: string
  stage: Stage
  completed: boolean
  failed: boolean
  error: string
  /** 排队位置（1-based）。0 表示已开跑或已终止（R-54）。 */
  queue_position: number
  /** 顶层 commit：报告为 null 时概览条仍要显示它。 */
  commit_sha: string
  report: Report | null
  review: Review | null
  index: IndexStatus | null
  modules: ModuleInfo[]
  dependency_graph: DependencyGraphInfo | null
  language_profile: LanguageProfile | null
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
