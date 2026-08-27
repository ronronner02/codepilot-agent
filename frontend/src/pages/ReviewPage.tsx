/**
 * 三个评审页（U14，R-30~R-33、NA-09）。
 *
 * 一个组件按类别参数化，三个路由各传一个。三页的呈现逻辑完全相同——拆成三个组件会让
 * 「未执行 / 零命中 / 有发现」三态的区分在三处各写一遍，而 NA-09 要钉住的正是这三种文案
 * 必须不同且都非空。
 *
 * 三态的判定读该类别的 `CategoryOutcome`：
 *
 * - `skipped` → 未执行，给出 reason（R-31。**不得显示为零发现**——那会让人以为查过了）；
 * - `failed` → 执行失败，与未执行可区分（一个是没查，一个是查崩了，下一步动作不同）；
 * - `executed` 且 `hit_count` 为 0 → 已执行零命中，给出 scope 覆盖范围（R-32：说清查了
 *   什么范围，读者才能判断「没发现问题」有多可信）；
 * - 有发现 → 按严重度分组并显示各组计数（R-30）。
 */

import { CitationLink } from '../components/CitationLink'
import {
  ReviewFindings,
  SEVERITY_KEYS,
  labelForKey,
  severityKey,
} from '../components/ReviewFindings'
import type { Finding } from '../api/types'
import { useAnalysis } from '../state/analysis'
import { usePageHeading } from './usePageHeading'

interface Props {
  /** 后端 `FindingCategory` 的取值：structural / error_handling / security。 */
  category: string
  /**
   * 页标题。与导航标签一致（R-01 规定了导航的九个名字）。
   *
   * 与 `CATEGORY_LABELS` 的中文名分开：导航上写 Security Review，页标题也该是它——两者
   * 不一致会让用户点了 Security Review 却看到「安全可疑模式」，怀疑自己点错了。中文类别名
   * 仍出现在内容区的检查标题里，因为那对应后端的类别语义。
   */
  title: string
}


export function ReviewPage({ category, title }: Props) {
  const headingRef = usePageHeading<HTMLHeadingElement>()
  const { result, taskId, ready } = useAnalysis()

  if (!ready || !result?.review) {
    return (
      <section className="page">
        <h1 className="page__title" ref={headingRef} tabIndex={-1}>
          {title}
        </h1>
        <p className="page__empty">
          分析结果尚未就绪。
        </p>
      </section>
    )
  }

  const review = result.review
  const outcome = review.outcomes.find((o) => o.category === category)
  const findings = review.findings.filter((f) => f.category === category)

  // 按严重度分组计数（R-30）。按**键**比对而非按文案：文案改动时按文案比对会静默失效，
  // 表现为所有分组都是 0 而不报错。未知取值由 severityKey 归入 medium。
  const buckets = SEVERITY_KEYS.map((key) => ({
    key,
    label: labelForKey(key),
    count: findings.filter((f) => severityKey(f.severity) === key).length,
  })).filter((bucket) => bucket.count > 0)

  return (
    <section className="page">
      <h1 className="page__title" ref={headingRef} tabIndex={-1}>
        {title}
      </h1>

      {outcome === undefined ? (
        <p className="page__empty">本次分析未产出该类别的检查记录。</p>
      ) : outcome.status === 'skipped' ? (
        <div className="review-state review-state--skipped" role="status">
          <p className="review-state__title">未执行</p>
          <p className="review-state__reason">{outcome.reason || '未给出原因'}</p>
          <p className="review-state__hint">
            未执行不等于零发现——这一类检查本次没有运行，结果未知。
          </p>
        </div>
      ) : outcome.status === 'failed' ? (
        <div className="review-state review-state--failed" role="status">
          <p className="review-state__title">执行失败</p>
          <p className="review-state__reason">{outcome.reason || '未给出原因'}</p>
          <p className="review-state__hint">
            检查已启动但中途失败，与「未执行」不同：部分文件可能已被检查过。
          </p>
        </div>
      ) : findings.length === 0 ? (
        <div className="review-state review-state--clean" role="status">
          <p className="review-state__title">已执行，零命中</p>
          <p className="review-state__reason">覆盖范围：{outcome.scope}</p>
          <p className="review-state__hint">
            这一类检查已运行且在上述范围内未发现问题。范围之外的文件未被检查。
          </p>
        </div>
      ) : (
        <>
          <p className="review-state__counts">
            共 {findings.length} 条发现：
            {buckets.map((bucket) => `${bucket.label} ${bucket.count} 条`).join('，')}
          </p>
          <ReviewFindings
            review={review}
            category={category}
            renderCitation={(finding: Finding) => (
              <CitationLink taskId={taskId} path={finding.path} line={finding.line} />
            )}
          />
        </>
      )}
    </section>
  )
}
