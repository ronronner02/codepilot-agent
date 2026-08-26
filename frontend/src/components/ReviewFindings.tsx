/**
 * 评审发现。
 *
 * **各类检查的执行情况必须与发现列表一起呈现。** 只显示发现列表的话，「查了但零命中」与
 * 「没查」在界面上都是空列表——而后端专门为这个区分做了 status 字段（R17）。零命中时那一节
 * 要说出查了什么范围，否则读者无法判断「没发现问题」有多可信。
 */

import type { ReactNode } from 'react'
import type { Finding, Review } from '../api/types'

interface Props {
  review: Review
  /**
   * 只呈现该类别（U14 的三个评审页各传一个）。省略时呈现全部类别。
   *
   * 做成可选参数而非拆三个组件：三页的呈现逻辑完全相同，拆开会让「未执行与零命中的区分」
   * 在三处各写一遍，而那正是 NA-09 要钉住的地方。
   */
  category?: string
  /** 每条发现的可点跳转（U13）。省略时发现不可点。 */
  renderCitation?: (finding: Finding) => ReactNode
}

export const CATEGORY_LABELS: Record<string, string> = {
  structural: '结构类',
  error_handling: '错误处理',
  security: '安全可疑模式',
}

/**
 * 严重度标签。
 *
 * 只映射 high/medium/low；后端返回其它值时归入「中」并在该条发现旁标注原值——丢弃未知
 * 取值会让一条真实发现从界面上消失，而按原值直接显示会让排序与配色失去依据。
 */
const SEVERITY_LABELS: Record<string, string> = {
  high: '高',
  medium: '中',
  low: '低',
}

export type SeverityKey = 'high' | 'medium' | 'low'

/** 已知的严重度键，按从重到轻。分组呈现按这个顺序，不按字母。 */
export const SEVERITY_KEYS: readonly SeverityKey[] = ['high', 'medium', 'low']

/**
 * 归一化到已知的三档。未知取值归入 medium。
 *
 * 与 `severityLabel` 分开：分组计数要按**键**比对，按文案比对会在文案改动时静默失效
 * （分组全变成 0 而不报错）。
 */
export function severityKey(severity: string): SeverityKey {
  return severity === 'high' || severity === 'medium' || severity === 'low'
    ? severity
    : 'medium'
}

export function severityLabel(severity: string): { label: string; original: string } {
  const known = SEVERITY_LABELS[severity]
  if (known) return { label: known, original: '' }
  return { label: SEVERITY_LABELS.medium, original: severity }
}

/** 键到中文标签。呈现层用它，不重复维护一份映射。 */
export function labelForKey(key: SeverityKey): string {
  return SEVERITY_LABELS[key]
}

const STATUS_LABELS: Record<string, string> = {
  executed: '已执行',
  skipped: '未执行',
  failed: '执行失败',
}

export function ReviewFindings({ review, category, renderCitation }: Props) {
  const outcomes = category
    ? review.outcomes.filter((o) => o.category === category)
    : review.outcomes
  const findings = category
    ? review.findings.filter((f) => f.category === category)
    : review.findings

  return (
    <section className="review" aria-labelledby="review-heading">
      <h2 className="review__heading" id="review-heading">
        {category ? `${CATEGORY_LABELS[category] ?? category}检查` : '代码评审'}
      </h2>

      <div className="review__outcomes">
        {outcomes.length === 0 && category ? (
          <p className="review__empty">本次分析未产出该类别的检查记录。</p>
        ) : null}
        {outcomes.map((outcome) => {
          const executed = outcome.status === 'executed'
          return (
            <div className="review__outcome" key={outcome.category}>
              <h3 className="review__outcome-title">
                {CATEGORY_LABELS[outcome.category] ?? outcome.category}
              </h3>
              <p className="review__outcome-status">
                {STATUS_LABELS[outcome.status] ?? outcome.status}
                {/* 零命中与未执行在这里必须读起来不同（R17、AE2）。 */}
                {executed ? `，命中 ${outcome.hit_count} 条` : ''}
              </p>
              {outcome.reason ? (
                <p className="review__outcome-reason">{outcome.reason}</p>
              ) : null}
              <details className="review__outcome-scope">
                <summary>检查范围</summary>
                <p>{outcome.scope}</p>
              </details>
            </div>
          )
        })}
      </div>

      {findings.length === 0 ? (
        <p className="review__empty">
          本次评审未产出发现。各类检查的执行情况见上方——已执行且命中 0 条与未执行含义不同。
        </p>
      ) : (
        <ul className="review__findings">
          {findings.map((finding, index) => {
            const severity = severityLabel(finding.severity)
            return (
            <li className="finding" key={`${finding.path}-${finding.line}-${index}`}>
              <div className="finding__head">
                <span className={`finding__severity finding__severity--${finding.severity}`}>
                  {severity.label}
                </span>
                {severity.original ? (
                  // 未知取值归入「中」但标注原值：不标注会让「后端换了一档严重度」
                  // 这件事在界面上完全不可见。
                  <span className="finding__severity-raw">原值 {severity.original}</span>
                ) : null}
                <span className="finding__kind">{finding.kind}</span>
                {renderCitation ? (
                  renderCitation(finding)
                ) : (
                  <code className="citation">
                    {finding.line > 0 ? `${finding.path}:${finding.line}` : finding.path}
                  </code>
                )}
              </div>
              <p className="finding__message">{finding.message}</p>
              {/* 判断依据与结论分开显示：读者要能区分「机器凭什么定位到这里」与
                  「模型凭什么认为值得管」。 */}
              <p className="finding__evidence">{finding.evidence}</p>
              {finding.line === 0 ? (
                // R-22：line 为 0 表示问题属于文件整体，不是第 0 行。
                <p className="finding__whole-file">该问题属于文件整体</p>
              ) : null}
            </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
