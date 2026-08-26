/**
 * 顶部概览条（U10，R-06~R-08、BR-002）。
 *
 * 常驻顶部、跨页可见。八项：仓库标识、commit 短 SHA、当前阶段、文件总数、模块数、评审发现
 * 总数、索引切块数、语言分布。
 *
 * **每个数字直接取后端字段，组件内没有算术**（R-08、BR-002）。唯一的例外是 `findings.length`
 * 与语言分布的「其它」合计——前者是取数组长度（不是加权），后者是把已知的前 N 项之外相加，
 * 两者都能指回具体字段。**不出现** Stars、代码行数、技术债估时、星级或任何 0-100 的分数
 * （R-07、NA-01）。
 */

import type { AnalysisResult } from '../api/types'

interface Props {
  /** 仓库标识。分析进行中结果还没到，靠提交响应里的值。 */
  repo: string
  stageLabel: string
  result: AnalysisResult | null
}

/** 语言分布显示前 5 项，其余归入「其它」。截断是界面职责，API 返回全量。 */
const LANGUAGE_LIMIT = 5

function show(value: number | null | undefined): string {
  // 无数据与零值含义不同：没有索引显示「—」，索引了 0 块显示 0。
  return value === null || value === undefined ? '—' : String(value)
}

export function OverviewStrip({ repo, stageLabel, result }: Props) {
  if (!repo) {
    return (
      <div className="strip strip--empty">
        <span className="strip__repo strip__repo--empty">未选择仓库</span>
        <span className="strip__stage">{stageLabel}</span>
      </div>
    )
  }

  const profile = result?.language_profile ?? null
  const entries = profile ? Object.entries(profile.by_language) : []
  const sorted = [...entries].sort((a, b) => b[1] - a[1])
  const top = sorted.slice(0, LANGUAGE_LIMIT)
  const restCount = sorted.slice(LANGUAGE_LIMIT).reduce((sum, [, count]) => sum + count, 0)

  return (
    <div className="strip">
      <span className="strip__repo">{repo}</span>
      {result?.commit_sha ? (
        <span className="strip__sha">{result.commit_sha.slice(0, 12)}</span>
      ) : null}
      <span className="strip__stage">{stageLabel}</span>

      <dl className="strip__stats">
        <div className="strip__stat">
          <dt>文件</dt>
          <dd>{show(profile?.total_files)}</dd>
        </div>
        <div className="strip__stat">
          <dt>模块</dt>
          <dd>{show(result?.modules.length)}</dd>
        </div>
        <div className="strip__stat">
          <dt>评审发现</dt>
          <dd>{show(result?.review?.findings.length ?? null)}</dd>
        </div>
        <div className="strip__stat">
          <dt>索引切块</dt>
          <dd>{show(result?.index?.chunk_count ?? null)}</dd>
        </div>
      </dl>

      {top.length > 0 ? (
        <ul className="strip__langs">
          {top.map(([language, count]) => (
            <li key={language}>
              {language} {count}
            </li>
          ))}
          {restCount > 0 ? <li>其它 {restCount}</li> : null}
        </ul>
      ) : null}
    </div>
  )
}
