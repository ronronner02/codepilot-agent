/**
 * Overview 页（U10）：承载提交、分析进度与失败呈现。
 *
 * 这一页是九页里唯一在未就绪时也可用的数据页（R-03 的例外）——它就是「分析进行中」的
 * 落点。ProgressPanel 与失败呈现从 U1 之前的 `App.tsx` 迁到这里，SSE 生命周期不动
 * （BR-008），只是渲染位置变了。
 *
 * 顶部概览条不在这里——它常驻 `AppShell` 的 topbar，跨页可见（R-06）。
 */

import { FailureNotice } from '../components/FailureNotice'
import { ProgressPanel } from '../components/ProgressPanel'
import { RepoInput } from '../components/RepoInput'
import { StatStrip } from '../components/StatStrip'
import { useAnalysis } from '../state/analysis'
import { usePageHeading } from './usePageHeading'

export function OverviewPage() {
  const headingRef = usePageHeading<HTMLHeadingElement>()
  const { phase, events, result, failure, disconnected, ready, reconnect, submit } =
    useAnalysis()

  const busy = phase === 'submitting' || phase === 'analyzing'
  const queued = result !== null && result.queue_position > 0 && !result.completed

  return (
    <section className="page">
      <h1 className="page__title" ref={headingRef} tabIndex={-1}>
        Overview
      </h1>

      <RepoInput
        disabled={busy}
        disabledReason={
          phase === 'submitting' ? '正在提交…' : '分析进行中，完成后可提交新仓库。'
        }
        onSubmit={(repoUrl) => void submit(repoUrl)}
      />

      {queued ? (
        // 排队位置（R-54）。不给「继续」入口——排队态随进程内存易失（R-46 的延伸）。
        <p className="page__note" role="status">
          已排队，当前第 {result.queue_position} 位，前序分析完成后自动开始。
        </p>
      ) : null}

      {failure ? <FailureNotice reason={failure.reason} message={failure.message} /> : null}

      {phase === 'analyzing' || (events.length > 0 && phase !== 'succeeded') ? (
        <ProgressPanel
          events={events}
          disconnected={disconnected}
          onReconnect={reconnect}
          focusOnMount={false}
        />
      ) : null}

      {ready && result ? <StatStrip result={result} /> : null}

      {ready && result && result.module_failures.length > 0 ? (
        // AE4：报告须标注模块缺失及原因，界面不能把它藏起来。
        <details className="results__module-failures">
          <summary>{result.module_failures.length} 个模块未完成分析</summary>
          <ul>
            {result.module_failures.map((note, index) => (
              <li key={index}>{note}</li>
            ))}
          </ul>
        </details>
      ) : null}

      {ready && result?.index?.cache_hit ? (
        <p className="results__cache-note">{result.index.note}</p>
      ) : null}
    </section>
  )
}
