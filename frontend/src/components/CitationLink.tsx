/**
 * 引用的统一跳转（U13，R-12、R-23、R-26、R-28、R-33、R-37）。
 *
 * 四处引用（报告结论、评审发现、检索命中、问答引用）共用这一个组件。集中在一处的理由是
 * 四处的跳转语义完全相同，分散实现会让「line 为 0 时不跳第 0 行」这类边界在四处各写一遍
 * ——而漏掉的那一处不会报错，只会跳到一个不存在的行。
 *
 * **呈现形态不能退化**（R-37、AE-19）：等宽字体、路径与行号完整、不截断、不移入悬浮层。
 * 这是 002 的既有成果（`ReportView` 已实现），本组件改为可点时沿用同一个 `citation` 类，
 * 超宽由容器横向滚动而非撑破整页。
 */

import { Link } from 'react-router-dom'
import { viewerPath } from '../routes/paths'

interface Props {
  taskId: string
  path: string
  /** 起始行。0 或 null 表示「问题属于文件整体」，跳转时不带行号（R-22）。 */
  line?: number | null
  endLine?: number | null
}

/** 引用的文本形态。与 `ReportView.formatCitation` 同一套规则。 */
export function formatLocation(
  path: string,
  line?: number | null,
  endLine?: number | null,
): string {
  if (!line || line <= 0) return path
  if (endLine === null || endLine === undefined || endLine === line) {
    return `${path}:${line}`
  }
  return `${path}:${line}-${endLine}`
}

export function CitationLink({ taskId, path, line, endLine }: Props) {
  const label = formatLocation(path, line, endLine)

  // 没有 taskId 时不可点：引用的目标是「这次分析的工作副本」，脱离分析无处可跳。
  if (!taskId) {
    return <code className="citation">{label}</code>
  }

  return (
    <Link className="citation citation--link" to={viewerPath(taskId, path, line ?? 0)}>
      {label}
    </Link>
  )
}
