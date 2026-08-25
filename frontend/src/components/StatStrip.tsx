/**
 * 分析结果统计。
 *
 * **只统计分析产物，不呈现执行过程**（plan 的 R23）。所以这里没有 per-module 状态、
 * 阶段耗时、token 成本或工具调用序列——那些字段后端也没有，加它们等于同时突破产品定位
 * 与「后端零改动」两条约束。
 *
 * 不挂 role="status" / role="alert" / aria-live：现有组件行为测试用单数查询取进度状态与
 * 错误提示，这里再加一个同名 role 会让那些查询变成多匹配。统计是静态派生值，本身也不需要
 * 播报。
 *
 * 严重度图例的文案是「高危 N」而非「高」：评审发现列表里的徽标就是「高」，两处文本相同会
 * 让按文本查询同时命中两个节点。
 */

import type { AnalysisResult } from '../api/types'

interface Props {
  result: AnalysisResult
}

interface Bucket {
  key: 'high' | 'medium' | 'low' | 'other'
  label: string
  count: number
}

/** 无数据与零值含义不同：没有索引显示「—」，索引了 0 块显示 0。 */
function show(value: number | null): string {
  return value === null ? '—' : String(value)
}

export function StatStrip({ result }: Props) {
  const findings = result.review?.findings ?? []

  const citationCount = result.report
    ? result.report.sections.reduce(
        (sum, section) =>
          sum + section.claims.reduce((inner, claim) => inner + claim.citations.length, 0),
        0,
      )
    : null

  const chunkCount = result.index ? result.index.chunk_count : null
  const unfinished = result.module_failures.length

  const tally = { high: 0, medium: 0, low: 0, other: 0 }
  for (const finding of findings) {
    if (finding.severity === 'high') tally.high += 1
    else if (finding.severity === 'medium') tally.medium += 1
    else if (finding.severity === 'low') tally.low += 1
    // 后端若新增严重度取值，落进「其它」而不是被静默丢弃。
    else tally.other += 1
  }

  const buckets: Bucket[] = [
    { key: 'high', label: '高危', count: tally.high },
    { key: 'medium', label: '中危', count: tally.medium },
    { key: 'low', label: '低危', count: tally.low },
    { key: 'other', label: '其它', count: tally.other },
  ].filter((bucket) => bucket.count > 0) as Bucket[]

  const total = findings.length

  return (
    <section className="stats-block" aria-label="分析结果统计">
      <div className="stats">
        <div className={total > 0 ? 'stat stat--warn' : 'stat'}>
          <span className="stat__n">{total}</span>
          <span className="stat__k">评审发现</span>
        </div>
        <div className="stat">
          <span className="stat__n">{show(citationCount)}</span>
          <span className="stat__k">可核验引用</span>
        </div>
        <div className="stat">
          <span className="stat__n">{show(chunkCount)}</span>
          <span className="stat__k">索引切块</span>
        </div>
        <div className={unfinished > 0 ? 'stat stat--bad' : 'stat'}>
          <span className="stat__n">{unfinished}</span>
          <span className="stat__k">未完成模块</span>
        </div>
      </div>

      {result.review ? (
        <div className="sev">
          <p className="sev__title">严重度分布</p>
          {total === 0 ? (
            <p className="sev__none">0 条发现。各类检查的执行情况见下方评审区。</p>
          ) : (
            <>
              <div className="sev__track">
                {buckets.map((bucket) => (
                  <span
                    key={bucket.key}
                    className={`sev__seg sev__seg--${bucket.key}`}
                    style={{ width: `${(bucket.count / total) * 100}%` }}
                  />
                ))}
              </div>
              <div className="sev__legend">
                {buckets.map((bucket) => (
                  <span className="sev__item" key={bucket.key}>
                    <i className={`sev__dot sev__dot--${bucket.key}`} aria-hidden="true" />
                    {bucket.label} {bucket.count}
                  </span>
                ))}
              </div>
            </>
          )}
        </div>
      ) : null}
    </section>
  )
}
