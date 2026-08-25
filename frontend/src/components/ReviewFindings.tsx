/**
 * 评审发现。
 *
 * **各类检查的执行情况必须与发现列表一起呈现。** 只显示发现列表的话，「查了但零命中」与
 * 「没查」在界面上都是空列表——而后端专门为这个区分做了 status 字段（R17）。零命中时那一节
 * 要说出查了什么范围，否则读者无法判断「没发现问题」有多可信。
 */

import type { Review } from '../api/types'

interface Props {
  review: Review
}

const CATEGORY_LABELS: Record<string, string> = {
  structural: '结构类',
  error_handling: '错误处理',
  security: '安全可疑模式',
}

const SEVERITY_LABELS: Record<string, string> = {
  high: '高',
  medium: '中',
  low: '低',
}

const STATUS_LABELS: Record<string, string> = {
  executed: '已执行',
  skipped: '未执行',
  failed: '执行失败',
}

export function ReviewFindings({ review }: Props) {
  return (
    <section className="review" aria-labelledby="review-heading">
      <h2 className="review__heading" id="review-heading">
        代码评审
      </h2>

      <div className="review__outcomes">
        {review.outcomes.map((outcome) => {
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

      {review.findings.length === 0 ? (
        <p className="review__empty">
          本次评审未产出发现。各类检查的执行情况见上方——已执行且命中 0 条与未执行含义不同。
        </p>
      ) : (
        <ul className="review__findings">
          {review.findings.map((finding, index) => (
            <li className="finding" key={`${finding.path}-${finding.line}-${index}`}>
              <div className="finding__head">
                <span className={`finding__severity finding__severity--${finding.severity}`}>
                  {SEVERITY_LABELS[finding.severity] ?? finding.severity}
                </span>
                <span className="finding__kind">{finding.kind}</span>
                <code className="citation">
                  {finding.line > 0 ? `${finding.path}:${finding.line}` : finding.path}
                </code>
              </div>
              <p className="finding__message">{finding.message}</p>
              {/* 判断依据与结论分开显示：读者要能区分「机器凭什么定位到这里」与
                  「模型凭什么认为值得管」。 */}
              <p className="finding__evidence">{finding.evidence}</p>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
