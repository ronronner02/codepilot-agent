/**
 * 评审执行情况表（U15，R-34）。
 *
 * 三类检查各自的 status / scope / hit_count / reason 并列成表。与 `ReviewFindings` 的分块
 * 呈现分工不同：那里是「逐类展开看细节」，这里是「一眼看完三类都做了没有」——Reports 页
 * 要的是后者，因为它是整份报告的门面。
 *
 * 用 `<table>` 而非 div 网格：三类 × 四列是真实的表格数据，读屏软件靠表头关联才能念出
 * 「安全可疑模式，未执行，原因：未配置 LLM provider」。div 网格会念成一串孤立的词。
 */

import type { CategoryOutcome } from '../api/types'
import { CATEGORY_LABELS } from './ReviewFindings'

interface Props {
  outcomes: CategoryOutcome[]
}

const STATUS_LABELS: Record<string, string> = {
  executed: '已执行',
  skipped: '未执行',
  failed: '执行失败',
}

export function ReviewSummaryTable({ outcomes }: Props) {
  if (outcomes.length === 0) {
    return <p className="page__empty">本次分析未产出评审执行记录。</p>
  }

  return (
    <table className="review-table">
      <caption className="review-table__caption">三类检查的执行情况</caption>
      <thead>
        <tr>
          <th scope="col">类别</th>
          <th scope="col">状态</th>
          <th scope="col">命中</th>
          <th scope="col">范围 / 原因</th>
        </tr>
      </thead>
      <tbody>
        {outcomes.map((outcome) => {
          const executed = outcome.status === 'executed'
          return (
            <tr key={outcome.category}>
              <th scope="row">{CATEGORY_LABELS[outcome.category] ?? outcome.category}</th>
              <td>{STATUS_LABELS[outcome.status] ?? outcome.status}</td>
              {/* 未执行时命中数显示「—」而非 0：0 会被读成「查过了没问题」。 */}
              <td>{executed ? outcome.hit_count : '—'}</td>
              <td>{executed ? outcome.scope : outcome.reason || '未给出原因'}</td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}
