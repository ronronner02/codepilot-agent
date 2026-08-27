/**
 * 架构报告。
 *
 * 引用路径与行号必须可见（计划的验收项）——那是报告可核验的前提，藏在 tooltip 或悬浮层里
 * 等于没有。
 *
 * 长路径横向滚动而非撑破布局（计划的响应式要求）：`.citation` 的样式用 `overflow-x: auto`，
 * 不用 `text-overflow: ellipsis`——省略号会把行号截掉，而行号正是引用的关键部分。
 */

import { useEffect, useRef } from 'react'
import type { Citation, Report } from '../api/types'
import { CitationLink, formatLocation } from './CitationLink'

interface Props {
  report: Report
  /** 完成后焦点移到报告标题（计划的可访问交互要求）。 */
  focusOnMount: boolean
  /**
   * 当前任务。非空时引用变为可点，跳转到查看器并定位（U13，R-12）。
   *
   * 传 taskId 而非传一个渲染函数：引用的跳转语义只有一种（去查看器看那个位置），让调用方
   * 各自决定怎么跳会把 `CitationLink` 的集中收口打散。
   */
  taskId?: string
}

/** 引用的文本形态。与可点引用共用 `formatLocation`——两份实现会在边界上漂移。 */
function formatCitation(citation: Citation): string {
  return formatLocation(citation.path, citation.line, citation.end_line)
}

export function ReportView({ report, focusOnMount, taskId = '' }: Props) {
  const headingRef = useRef<HTMLHeadingElement>(null)

  useEffect(() => {
    if (focusOnMount) headingRef.current?.focus()
  }, [focusOnMount])

  const nonEmptySections = report.sections.filter((section) => section.claims.length > 0)

  return (
    <section className="report" aria-labelledby="report-heading">
      <h2 className="report__heading" id="report-heading" ref={headingRef} tabIndex={-1}>
        架构报告
      </h2>
      <p className="report__meta">
        {report.repo}
        {report.commit_sha ? ` · ${report.commit_sha.slice(0, 12)}` : ''}
      </p>

      {report.summary ? <p className="report__summary">{report.summary}</p> : null}

      {nonEmptySections.length === 0 ? (
        <p className="report__empty">
          报告没有通过校验的结论。这通常意味着模块分析未完成，或结论都无法追溯到具体文件。
        </p>
      ) : (
        nonEmptySections.map((section) => (
          <div className="report__section" key={section.key}>
            <h3 className="report__section-title">{section.title}</h3>
            <ul className="report__claims">
              {section.claims.map((claim, index) => (
                <li className="report__claim" key={`${section.key}-${index}`}>
                  <p className="report__claim-text">{claim.text}</p>
                  {claim.citations.length > 0 ? (
                    <ul className="report__citations">
                      {claim.citations.map((citation, citationIndex) => (
                        <li key={`${citation.path}-${citationIndex}`}>
                          {taskId ? (
                            <CitationLink
                              taskId={taskId}
                              path={citation.path}
                              line={citation.line}
                              endLine={citation.end_line}
                            />
                          ) : (
                            <code className="citation">{formatCitation(citation)}</code>
                          )}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        ))
      )}

      {report.unsupported_claims.length > 0 ? (
        // 无法核验的结论要显式呈现，不能只在后端日志里——读者需要知道报告有多少内容被丢弃了。
        <details className="report__unsupported">
          <summary>
            {report.unsupported_claims.length} 条结论因引用无法核验被丢弃
          </summary>
          <ul>
            {report.unsupported_claims.map((note, index) => (
              <li key={index}>{note}</li>
            ))}
          </ul>
        </details>
      ) : null}

      {/* 缺失部分始终显示：空节与「没有这一节」在读者看来不同。 */}
      <details className="report__missing">
        <summary>分析的缺失部分</summary>
        <pre className="report__missing-text">{report.missing.text}</pre>
      </details>

      {report.validation_summary ? (
        <p className="report__validation">{report.validation_summary}</p>
      ) : null}
    </section>
  )
}
