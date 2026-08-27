/**
 * Reports 页（U15，R-34~R-37）。
 *
 * 报告五节全文（引用可点）、评审执行情况表、引用校验通过率、缺失部分说明，加导出入口。
 *
 * **不出现任何评分**（R-35、NA-01）。origin 的原始方案有评分条，这一页是最容易「顺手加
 * 回去」的地方——架构评分、代码质量分、安全分、0-100 的合成分数都不允许。通过率取
 * `report.validation_summary`（后端已算好的字符串）而非前端重算：重算会让界面与导出产生
 * 两个来源，而 BR-002 要求数字可指回后端字段。
 */

import { ReportView } from '../components/ReportView'
import { ReviewSummaryTable } from '../components/ReviewSummaryTable'
import { useAnalysis } from '../state/analysis'
import { usePageHeading } from './usePageHeading'

export function ReportsPage() {
  const headingRef = usePageHeading<HTMLHeadingElement>()
  const { result, taskId, ready } = useAnalysis()

  if (!ready || !result) {
    return (
      <section className="page">
        <h1 className="page__title" ref={headingRef} tabIndex={-1}>
          Reports
        </h1>
        <p className="page__empty">分析结果尚未就绪。完成一次分析后可查看报告全文。</p>
      </section>
    )
  }

  return (
    <section className="page">
      <h1 className="page__title" ref={headingRef} tabIndex={-1}>
        Reports
      </h1>

      <ExportBar taskId={taskId} />

      {result.report ? (
        <ReportView report={result.report} focusOnMount={false} taskId={taskId} />
      ) : (
        <p className="page__empty">
          本次分析未产出架构报告。评审结果与结构数据仍可查看。
        </p>
      )}

      <h2 className="page__section-title">评审执行情况</h2>
      <ReviewSummaryTable outcomes={result.review?.outcomes ?? []} />

      {result.module_failures.length > 0 ? (
        <div className="page__note">
          <p>{result.module_failures.length} 个模块未完成分析：</p>
          <ul>
            {result.module_failures.map((note, index) => (
              <li key={index}>{note}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  )
}

/**
 * 导出入口（U19、U20）。
 *
 * 用普通链接而非 fetch + Blob：浏览器对 `Content-Disposition: attachment` 的处理已经是
 * 「下载并按后端给的文件名保存」，走 fetch 要自己造 objectURL、自己解析文件名、自己清理，
 * 而那三件事都可能出错。代价是失败时看不到后端的 reason——PDF 失败会打开一个错误 JSON，
 * 所以 PDF 的失败说明写在按钮旁而非依赖跳转结果。
 */
function ExportBar({ taskId }: { taskId: string }) {
  const base = `/api/analyses/${encodeURIComponent(taskId)}/export`
  return (
    <div className="export-bar">
      <span className="export-bar__label">导出本次分析</span>
      <a className="export-bar__link" href={`${base}?format=md`}>
        Markdown
      </a>
      <a className="export-bar__link" href={`${base}?format=html`}>
        HTML
      </a>
      <a className="export-bar__link" href={`${base}?format=pdf`}>
        PDF
      </a>
      {/* 不写「三种格式口径一致」：那是实现保证，不是用户需要读的话。PDF 失败时端点会返回
          带 reason 的错误，用户那时才需要知道能换格式——写在这里是提前解释一个还没发生的问题。 */}
    </div>
  )
}
