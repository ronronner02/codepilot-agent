/**
 * 应用外层：状态提供者 + 外壳 + 路由（U1）。
 *
 * U1 之前这里是一个渲染五区块的状态机。现在职责收窄为三件：把分析状态供给全树、渲染外壳、
 * 把路由挂进主内容区。分析状态本身搬到 `state/analysis.tsx`——那让「结果由顶层持有、九页
 * 共读」这条决定有一个明确的落点，而不是散在这个组件的十来个 useState 里。
 *
 * `BrowserRouter` 不在这里而在 `main.tsx`：测试要用 `MemoryRouter` 换掉它，套在组件内部就
 * 换不掉了（会出现嵌套 Router 的运行时错误）。
 */

import { AppShell } from './components/AppShell'
import { OverviewStrip } from './components/OverviewStrip'
import { AppRoutes } from './routes'
import { AnalysisProvider, useAnalysis } from './state/analysis'
import type { Phase } from './state/analysis'

/** 顶部条与侧栏共用的阶段呈现。tone 只决定配色，不决定文案。 */
const PHASE_VIEW: Record<Phase, { label: string; tone: 'idle' | 'busy' | 'ok' | 'bad' }> = {
  idle: { label: '未开始', tone: 'idle' },
  submitting: { label: '提交中', tone: 'busy' },
  analyzing: { label: '分析中', tone: 'busy' },
  succeeded: { label: '已完成', tone: 'ok' },
  failed: { label: '失败', tone: 'bad' },
}

function Workbench() {
  const { phase, taskId, repo, result, ready } = useAnalysis()
  const view = PHASE_VIEW[phase]

  return (
    <AppShell
      taskId={taskId}
      ready={ready}
      stageTone={view.tone}
      topbar={
        <OverviewStrip
          repo={repo || result?.repo || ''}
          stageLabel={view.label}
          result={result}
        />
      }
    >
      <AppRoutes />
    </AppShell>
  )
}

export function App() {
  return (
    <AnalysisProvider>
      <Workbench />
    </AnalysisProvider>
  )
}
