/**
 * 查看器右栏：当前文件的评审发现（U12，R-19~R-22、NA-07）。
 *
 * **内容只来自已有评审发现，不发任何新请求、不触发任何 LLM 调用**（R-21、NA-07）。这是 D9
 * 的决定，也是本组件最容易实现走偏的地方：任何「打开文件时补充分析一下」的想法都违反它。
 * 实现上表现为——整个组件没有 import 任何 API 客户端函数。
 *
 * **四种空态必须可区分**（R-20、KTD10 的第四态）：
 *
 * 1. 文件不在评审目标范围内 —— 没查过这个文件，结果未知；
 * 2. 在范围内但零命中 —— 查过了，这个文件没问题；
 * 3. 工作副本已被清理 —— 源文件读不到，但评审发现仍在；
 * 4. 还没选文件 —— 引导用户去点文件树。
 *
 * 把 1 和 2 混成一句「无发现」是最常见的退化，而两者对读者的含义正相反。
 */

import { CitationLink } from './CitationLink'
import { severityLabel } from './ReviewFindings'
import type { Finding, Review } from '../api/types'

interface Props {
  taskId: string
  /** 当前打开的文件。空串表示还没选。 */
  path: string
  review: Review | null
  /** 工作副本已被配额清理（KTD10 的第四态）。 */
  workspaceCleared: boolean
}

export function FileFindings({ taskId, path, review, workspaceCleared }: Props) {
  if (!path) {
    return (
      <aside className="findings findings--empty" aria-label="该文件的评审发现">
        <p>从左侧文件树选一个文件，这里显示它已有的评审发现。</p>
      </aside>
    )
  }

  if (workspaceCleared) {
    return (
      <aside className="findings" aria-label="该文件的评审发现">
        <h2 className="findings__heading">评审发现</h2>
        <p className="findings__state">
          代码副本已清理，无法查看源文件。分析结果、报告与评审发现仍可读——只是没有源码可
          对照。
        </p>
      </aside>
    )
  }

  if (review === null) {
    return (
      <aside className="findings" aria-label="该文件的评审发现">
        <h2 className="findings__heading">评审发现</h2>
        <p className="findings__state">本次分析未产出评审数据。</p>
      </aside>
    )
  }

  const findings = review.findings.filter((finding) => finding.path === path)
  const inScope = review.target_files.includes(path)

  return (
    <aside className="findings" aria-label="该文件的评审发现">
      <h2 className="findings__heading">评审发现</h2>

      {findings.length === 0 ? (
        inScope ? (
          <p className="findings__state">
            已评审，本文件无发现。它在本次评审的目标范围内，各类检查未在其中命中问题。
          </p>
        ) : (
          <p className="findings__state">
            本文件不在本次评审目标范围内。评审只挑选了一部分文件深查，所以这里没有结果不等于
            它没有问题。
          </p>
        )
      ) : (
        <ul className="findings__list">
          {findings.map((finding: Finding, index: number) => {
            const severity = severityLabel(finding.severity)
            return (
              <li className="finding" key={`${finding.line}-${index}`}>
                <div className="finding__head">
                  <span
                    className={`finding__severity finding__severity--${finding.severity}`}
                  >
                    {severity.label}
                  </span>
                  {severity.original ? (
                    <span className="finding__severity-raw">原值 {severity.original}</span>
                  ) : null}
                  <span className="finding__kind">{finding.kind}</span>
                </div>
                {finding.line > 0 ? (
                  <CitationLink taskId={taskId} path={finding.path} line={finding.line} />
                ) : (
                  // R-22：line 为 0 不是第 0 行，而是「属于文件整体」。
                  <p className="finding__whole-file">该问题属于文件整体</p>
                )}
                <p className="finding__message">{finding.message}</p>
                <p className="finding__evidence">{finding.evidence}</p>
              </li>
            )
          })}
        </ul>
      )}
    </aside>
  )
}
