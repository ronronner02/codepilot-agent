/**
 * 分析结果的持有与共享（U1 的「状态归属」决定）。
 *
 * **结果由路由顶层持有并按 task_id 缓存，九个页面读同一份。** 每页各自拉取的话，切一次页
 * 就多一次请求，而同一份数据的两个副本还会在「切页瞬间旧数据仍在」时显示不一致。
 *
 * **SSE 生命周期也放在这里。** 它必须跨页存活：分析进行中切到别的页再切回来，进度不该
 * 重来一遍（U1 的测试场景明写这一条）。放在某个页面组件里就会随该页卸载而断开。
 *
 * 访客凭证不进这个状态树——它由 settings 模块独立持有（U17）。混进来会让凭证跟着分析结果
 * 一起被传给九个页面，而其中大部分页面不该看到它。
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import type { ReactNode } from 'react'
import { ApiRequestError, NetworkError, fetchResult, submitAnalysis } from '../api/client'
import { connectProgress } from '../api/sse'
import type { AnalysisResult, ProgressEvent } from '../api/types'
import { loadCredentials } from '../settings/credentials'

export type Phase = 'idle' | 'submitting' | 'analyzing' | 'succeeded' | 'failed'

export interface FailureState {
  reason: string
  message: string
}

export interface AnalysisContextValue {
  phase: Phase
  taskId: string
  /** 提交响应里的仓库标识。分析进行中结果还没到，顶部条要靠它显示仓库名。 */
  repo: string
  events: ProgressEvent[]
  result: AnalysisResult | null
  failure: FailureState | null
  disconnected: boolean
  /** 结果已就绪且未失败。导航置灰与页面空态都读它（R-03）。 */
  ready: boolean
  submit: (repoUrl: string) => Promise<string | null>
  /** 载入某次已有分析（直达 URL 或从历史列表点进来）。 */
  load: (taskId: string) => void
  reconnect: () => void
}

const AnalysisContext = createContext<AnalysisContextValue | null>(null)

export function useAnalysis(): AnalysisContextValue {
  const value = useContext(AnalysisContext)
  if (value === null) {
    throw new Error('useAnalysis 必须在 AnalysisProvider 内使用')
  }
  return value
}

export function AnalysisProvider({ children }: { children: ReactNode }) {
  const [phase, setPhase] = useState<Phase>('idle')
  const [taskId, setTaskId] = useState('')
  const [repo, setRepo] = useState('')
  const [events, setEvents] = useState<ProgressEvent[]>([])
  const [result, setResult] = useState<AnalysisResult | null>(null)
  const [failure, setFailure] = useState<FailureState | null>(null)
  const [disconnected, setDisconnected] = useState(false)
  // 变化即触发 SSE 的 effect 重跑。比手动调用连接函数更不容易漏掉清理。
  const [reconnectNonce, setReconnectNonce] = useState(0)

  const connectionRef = useRef<(() => void) | null>(null)
  // 已载入过的 task_id。防止同一次直达 URL 在每次渲染都重新拉取。
  const loadedRef = useRef<string>('')

  const loadResult = useCallback(async (id: string) => {
    try {
      const loaded = await fetchResult(id)
      setResult(loaded)
      setRepo(loaded.repo)
      if (loaded.failed) {
        setPhase('failed')
        setFailure({ reason: 'analysis_failed', message: loaded.error || '分析失败' })
      } else if (loaded.completed) {
        setPhase('succeeded')
      } else {
        // 未完成：可能在跑，也可能在排队。两种都进 analyzing，由 SSE 继续推进。
        setPhase('analyzing')
      }
    } catch (cause) {
      setPhase('failed')
      if (cause instanceof ApiRequestError) {
        setFailure({ reason: cause.reason, message: cause.message })
      } else {
        // client_offline 而非 network_error：后者是后端克隆时的网络失败（会自动重试并退还
        // 限流配额），这里是请求根本没到后端，两者的下一步动作不同。
        setFailure({
          reason: 'client_offline',
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
        // 不静默停在进度条上——界面显示断连并给重连入口。
        setDisconnected(true)
      },
    })
    connectionRef.current = connection.close

    return () => {
      // close 是幂等的，所以不必判断当前状态。
      connection.close()
      connectionRef.current = null
    }
  }, [taskId, phase, reconnectNonce, loadResult])

  const submit = useCallback(
    async (repoUrl: string): Promise<string | null> => {
      // 重复提交在提交中与分析中被阻止。按钮已 disabled，这里是第二道——键盘回车与
      // 程序化调用绕不过它。
      if (phase === 'submitting' || phase === 'analyzing') return null

      setPhase('submitting')
      setEvents([])
      setResult(null)
      setFailure(null)
      setDisconnected(false)

      try {
        const accepted = await submitAnalysis(repoUrl, loadCredentials())
        loadedRef.current = accepted.task_id
        setTaskId(accepted.task_id)
        setRepo(accepted.repo)
        setPhase('analyzing')
        return accepted.task_id
      } catch (cause) {
        setPhase('failed')
        if (cause instanceof ApiRequestError) {
          setFailure({ reason: cause.reason, message: cause.message })
        } else {
          setFailure({
            reason: 'client_offline',
            message: cause instanceof NetworkError ? cause.message : '提交失败',
          })
        }
        return null
      }
    },
    [phase],
  )

  const load = useCallback(
    (id: string) => {
      // 已是当前任务就不重拉：直达 URL 的页面组件会在每次渲染时调用这个函数。
      if (!id || loadedRef.current === id) return
      loadedRef.current = id
      setTaskId(id)
      setEvents([])
      setResult(null)
      setFailure(null)
      setPhase('submitting')
      void loadResult(id)
    },
    [loadResult],
  )

  const reconnect = useCallback(() => {
    // 先关旧连接再重连：不关的话会有两个 EventSource 同时推事件，界面看到重复阶段。
    connectionRef.current?.()
    setReconnectNonce((value) => value + 1)
  }, [])

  const value = useMemo<AnalysisContextValue>(
    () => ({
      phase,
      taskId,
      repo,
      events,
      result,
      failure,
      disconnected,
      ready: phase === 'succeeded' && result !== null,
      submit,
      load,
      reconnect,
    }),
    [phase, taskId, repo, events, result, failure, disconnected, submit, load, reconnect],
  )

  return <AnalysisContext.Provider value={value}>{children}</AnalysisContext.Provider>
}
