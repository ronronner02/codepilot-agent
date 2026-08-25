/**
 * 单页应用。无路由，状态由组件内部管理（Implementation Scope Boundaries）。
 *
 * 状态机按计划的状态矩阵：初始 / 提交中 / 分析中 / 成功 / 失败 / 断连。用一个显式的
 * phase 字段而非多个布尔量——多个布尔量允许出现「既提交中又成功」这类不可能的组合，
 * 而那类 bug 表现为界面同时显示两个区块。
 *
 * SSE 的生命周期集中在一个 useEffect 里，清理函数关闭连接。不把 EventSource 存进 state：
 * 那会让它参与渲染依赖，而它的变化不该触发重渲染。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiRequestError, NetworkError, fetchResult, submitAnalysis } from './api/client'
import { connectProgress } from './api/sse'
import type { AnalysisResult, ProgressEvent } from './api/types'
import { AppShell } from './components/AppShell'
import type { NavItem } from './components/AppShell'
import { ProgressPanel } from './components/ProgressPanel'
import { QaPanel } from './components/QaPanel'
import { RepoInput } from './components/RepoInput'
import { ReportView } from './components/ReportView'
import { ReviewFindings } from './components/ReviewFindings'
import { StatStrip } from './components/StatStrip'

type Phase = 'idle' | 'submitting' | 'analyzing' | 'succeeded' | 'failed'

/** 顶部条与侧栏共用的阶段呈现。tone 只决定配色，不决定文案。 */
const PHASE_VIEW: Record<Phase, { label: string; tone: 'idle' | 'busy' | 'ok' | 'bad' }> = {
  idle: { label: '未开始', tone: 'idle' },
  submitting: { label: '提交中', tone: 'busy' },
  analyzing: { label: '分析中', tone: 'busy' },
  succeeded: { label: '已完成', tone: 'ok' },
  failed: { label: '失败', tone: 'bad' },
}

/**
 * 失败的分类呈现（AE6）。
 *
 * 按 reason 而非 message 文本分类：message 会随后端文案调整而变，reason 是稳定契约。
 * 每类给出不同的下一步提示——统一显示「失败」会让用户不知道该改地址、换仓库还是重试。
 */
const FAILURE_HINTS: Record<string, string> = {
  invalid_url: '地址格式不正确。请用 https://github.com/owner/repo 的形式。',
  not_found: '仓库不存在或不可访问。若是私有仓库，需要配置 GITHUB_TOKEN 才能区分这两种情况。',
  no_access: '没有访问权限。私有仓库需要有权限的 token。',
  too_large: '仓库规模超出本系统的处理上限。可以换一个较小的仓库。',
  network_error: '网络或上游服务异常，稍后重试。',
  task_not_found: '任务不存在，可能已因服务重启而丢失。请重新提交。',
}

interface FailureState {
  reason: string
  message: string
}

export function App() {
  const [phase, setPhase] = useState<Phase>('idle')
  const [taskId, setTaskId] = useState('')
  // 顶部条要在分析进行中就显示仓库标识，而结果这时还没到，所以提交响应里的 repo 单独存。
  const [repo, setRepo] = useState('')
  const [events, setEvents] = useState<ProgressEvent[]>([])
  const [result, setResult] = useState<AnalysisResult | null>(null)
  const [failure, setFailure] = useState<FailureState | null>(null)
  const [disconnected, setDisconnected] = useState(false)
  // 重连计数：变化即触发 SSE 的 useEffect 重跑，比手动调用连接函数更不容易漏掉清理。
  const [reconnectNonce, setReconnectNonce] = useState(0)

  // 存 EventSource 的 close 而非放进 state：它的变化不该触发重渲染。
  const connectionRef = useRef<(() => void) | null>(null)

  const loadResult = useCallback(async (id: string) => {
    try {
      const loaded = await fetchResult(id)
      setResult(loaded)
      if (loaded.failed) {
        setPhase('failed')
        setFailure({ reason: 'analysis_failed', message: loaded.error || '分析失败' })
      } else {
        setPhase('succeeded')
      }
    } catch (cause) {
      setPhase('failed')
      if (cause instanceof ApiRequestError) {
        setFailure({ reason: cause.reason, message: cause.message })
      } else {
        setFailure({
          reason: 'network_error',
          message: cause instanceof NetworkError ? cause.message : '读取结果失败',
        })
      }
    }
  }, [])

  useEffect(() => {
    if (!taskId || phase !== 'analyzing') return

    setDisconnected(false)
    const connection = connectProgress(taskId, {
      onEvent: (event) => {
        setEvents((previous) => [...previous, event])
      },
      onComplete: () => {
        void loadResult(taskId)
      },
      onDisconnect: () => {
        // 不静默停在进度条上——界面显示断连并给重连入口（计划明确要求）。
        setDisconnected(true)
      },
    })
    connectionRef.current = connection.close

    return () => {
      // 卸载或依赖变化时关闭连接。close 是幂等的，所以不必判断当前状态。
      connection.close()
      connectionRef.current = null
    }
  }, [taskId, phase, reconnectNonce, loadResult])

  const handleSubmit = async (repoUrl: string) => {
    // 重复提交在分析中被阻止（计划的状态矩阵）。按钮已 disabled，这里是第二道——
    // 键盘回车与程序化调用绕不过它。
    if (phase === 'submitting' || phase === 'analyzing') return

    setPhase('submitting')
    setEvents([])
    setResult(null)
    setFailure(null)
    setDisconnected(false)

    try {
      const accepted = await submitAnalysis(repoUrl)
      setTaskId(accepted.task_id)
      setRepo(accepted.repo)
      setPhase('analyzing')
    } catch (cause) {
      setPhase('failed')
      if (cause instanceof ApiRequestError) {
        setFailure({ reason: cause.reason, message: cause.message })
      } else {
        setFailure({
          reason: 'network_error',
          message: cause instanceof NetworkError ? cause.message : '提交失败',
        })
      }
    }
  }

  const handleReconnect = () => {
    // 先关旧连接再重连：不关的话会有两个 EventSource 同时推事件，界面看到重复阶段。
    connectionRef.current?.()
    setReconnectNonce((value) => value + 1)
  }

  const busy = phase === 'submitting' || phase === 'analyzing'
  const ready = phase === 'succeeded' && result !== null
  const view = PHASE_VIEW[phase]

  // 结果未就绪的导航项置灰：置灰本身就是答案，不必再配一句提示。
  const navItems: NavItem[] = [
    { id: 'sec-overview', label: '概览', enabled: true },
    { id: 'sec-report', label: '报告', enabled: ready && result?.report !== null },
    { id: 'sec-review', label: '评审', enabled: ready && result?.review !== null },
    { id: 'sec-qa', label: '问答', enabled: ready },
  ]

  return (
    <AppShell
      repo={repo || result?.repo || ''}
      commitSha={result?.report?.commit_sha ?? ''}
      stageLabel={view.label}
      stageTone={view.tone}
      taskId={taskId}
      navItems={navItems}
    >
      <div className="anchor" id="sec-overview">
        <RepoInput
          disabled={busy}
          disabledReason={
            phase === 'submitting' ? '正在提交…' : '分析进行中，完成后可提交新仓库。'
          }
          onSubmit={handleSubmit}
        />
      </div>

      {failure ? (
        <div className="failure" role="alert">
          <h2 className="failure__title">分析未能完成</h2>
          <p className="failure__message">{failure.message}</p>
          {FAILURE_HINTS[failure.reason] ? (
            <p className="failure__hint">{FAILURE_HINTS[failure.reason]}</p>
          ) : null}
        </div>
      ) : null}

      {phase === 'analyzing' || (events.length > 0 && phase !== 'succeeded') ? (
        <ProgressPanel
          events={events}
          disconnected={disconnected}
          onReconnect={handleReconnect}
          focusOnMount={phase === 'analyzing'}
        />
      ) : null}

      {ready && result ? (
        <div className="results">
          <StatStrip result={result} />

          {result.report ? (
            <div className="anchor" id="sec-report">
              <ReportView report={result.report} focusOnMount />
            </div>
          ) : null}

          {result.module_failures.length > 0 ? (
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

          {result.review ? (
            <div className="anchor" id="sec-review">
              <ReviewFindings review={result.review} />
            </div>
          ) : null}

          {result.index?.cache_hit ? (
            <p className="results__cache-note">{result.index.note}</p>
          ) : null}

          <div className="anchor" id="sec-qa">
            <QaPanel taskId={result.task_id} />
          </div>
        </div>
      ) : null}
    </AppShell>
  )
}
