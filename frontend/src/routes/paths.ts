/**
 * 路由地址的唯一来源。
 *
 * 集中在一处而非在各组件里写字符串字面量：导航项、`CitationLink` 的跳转、测试里的直达
 * 断言三处都要用同一个地址，散落的字面量改一处漏两处，而那类 bug 表现为「点了没反应」
 * 或「刷新后 404」，都不会在类型检查里暴露。
 *
 * 分析相关的页面都带 taskId 段。不用查询参数：URL 可直达可刷新是 R-02 的要求，而路径段
 * 比查询参数更能表达「这一页属于某次分析」的从属关系。
 */

/** 不依赖分析结果的页面。未就绪时仍可访问（R-03 的例外）。 */
export const HOME = '/'
export const SETTINGS = '/settings'
export const MCP = '/mcp'

/** 分析页的路径段。与 NAV_ITEMS 的 id 一一对应。 */
export type AnalysisPage =
  | 'overview'
  | 'architecture'
  | 'chat'
  | 'search'
  | 'security'
  | 'error-handling'
  | 'structural'
  | 'reports'
  | 'viewer'

export function analysisPath(taskId: string, page: AnalysisPage): string {
  return `/a/${encodeURIComponent(taskId)}/${page}`
}

/**
 * 查看器地址，可带文件路径与行号。
 *
 * 文件路径走查询参数而非路径段：仓库相对路径含 `/`，做成路径段会与路由自身的层级混淆，
 * 而 encodeURIComponent 后的 `%2F` 在部分服务端配置下会被提前解码。行号为 0 或缺省时
 * 不带 line 参数——R-22 要求「问题属于文件整体」的发现点击后不跳到第 0 行。
 */
export function viewerPath(taskId: string, filePath?: string, line?: number): string {
  const base = analysisPath(taskId, 'viewer')
  if (!filePath) return base
  const params = new URLSearchParams({ path: filePath })
  if (line && line > 0) params.set('line', String(line))
  return `${base}?${params.toString()}`
}

/**
 * 左侧导航的九项，加上两个不依赖分析的页面（R-01）。
 *
 * `needsResult` 决定未就绪时是否置灰（R-03）。Overview、Settings、MCP 三项例外——前者要
 * 承载提交与进度，后两者是静态内容。
 *
 * 顺序即 R-01 规定的顺序，不按字母或使用频率重排：需求把它作为固定清单给出，重排会让
 * 「导航固定为 9 项」这条要求变得难以核对。
 */
export interface NavDescriptor {
  /** 稳定标识。测试与 aria-current 都用它，不用可变的文案。 */
  id: AnalysisPage | 'settings' | 'mcp'
  label: string
  /** 分析页给出路径段；settings 与 mcp 是绝对地址。 */
  page: AnalysisPage | null
  absolute?: string
  needsResult: boolean
}

export const NAV_ITEMS: readonly NavDescriptor[] = [
  { id: 'overview', label: 'Overview', page: 'overview', needsResult: false },
  { id: 'architecture', label: 'Architecture', page: 'architecture', needsResult: true },
  { id: 'chat', label: 'AI Chat', page: 'chat', needsResult: true },
  { id: 'search', label: 'Code Search', page: 'search', needsResult: true },
  { id: 'security', label: 'Security Review', page: 'security', needsResult: true },
  { id: 'error-handling', label: 'Error Handling', page: 'error-handling', needsResult: true },
  { id: 'structural', label: 'Structural', page: 'structural', needsResult: true },
  { id: 'reports', label: 'Reports', page: 'reports', needsResult: true },
  { id: 'mcp', label: 'MCP', page: null, absolute: MCP, needsResult: false },
] as const

/** 未就绪时的置灰原因（R-03、AE-01 要求每项标注原因，不能只置灰）。 */
export const NOT_READY_REASON = '需先完成一次分析'
