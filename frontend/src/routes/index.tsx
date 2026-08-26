/**
 * 路由表（U1，R-01、R-02、R-05）。
 *
 * 分析相关的页面挂在 `/a/:taskId/:page` 下。`AnalysisRoute` 是它们共同的父层：它负责在
 * 直达 URL 时按 taskId 载入结果，九个页面组件因此不必各写一遍载入逻辑。
 *
 * **兜底路由不返回空白页**（R-05）。未匹配的地址显示「该分析不存在或已被清理」并给返回首页
 * 的入口——空白页会让用户以为界面崩了。
 */

import { useEffect } from 'react'
import { Link, Navigate, Route, Routes, useParams } from 'react-router-dom'
import { ArchitecturePage } from '../pages/ArchitecturePage'
import { ChatPage } from '../pages/ChatPage'
import { HomePage } from '../pages/HomePage'
import { McpPage } from '../pages/McpPage'
import { OverviewPage } from '../pages/OverviewPage'
import { ReportsPage } from '../pages/ReportsPage'
import { ReviewPage } from '../pages/ReviewPage'
import { SearchPage } from '../pages/SearchPage'
import { SettingsPage } from '../pages/SettingsPage'
import { ViewerPage } from '../pages/ViewerPage'
import { useAnalysis } from '../state/analysis'
import { HOME, MCP, SETTINGS } from './paths'

/**
 * 分析页的父层：按 URL 里的 taskId 载入结果。
 *
 * 载入放在这里而非各页面里，是因为九个页面对「结果从哪来」的需求完全相同。放各页面里的话，
 * 切页会各触发一次载入，而 `load` 的去重逻辑要在九处都正确。
 */
function AnalysisRoute({ children }: { children: React.ReactNode }) {
  const { taskId = '' } = useParams()
  const { load } = useAnalysis()

  useEffect(() => {
    if (taskId) load(taskId)
  }, [taskId, load])

  return <>{children}</>
}

/** 未匹配地址与不存在的分析（R-05）。 */
function NotFound() {
  return (
    <section className="page">
      <h1 className="page__title" tabIndex={-1}>
        该分析不存在或已被清理
      </h1>
      <p className="page__note">
        地址可能拼错了，或这次分析已因服务重启而丢失。分析任务的状态在进程内存里，重启后
        未完成的任务无法恢复。
      </p>
      <Link className="page__link" to={HOME}>
        返回首页
      </Link>
    </section>
  )
}

export function AppRoutes() {
  return (
    <Routes>
      <Route path={HOME} element={<HomePage />} />
      <Route path={SETTINGS} element={<SettingsPage />} />
      <Route path={MCP} element={<McpPage />} />

      <Route
        path="/a/:taskId"
        element={
          <AnalysisRoute>
            <Navigate to="overview" replace />
          </AnalysisRoute>
        }
      />
      <Route
        path="/a/:taskId/overview"
        element={
          <AnalysisRoute>
            <OverviewPage />
          </AnalysisRoute>
        }
      />
      <Route
        path="/a/:taskId/architecture"
        element={
          <AnalysisRoute>
            <ArchitecturePage />
          </AnalysisRoute>
        }
      />
      <Route
        path="/a/:taskId/chat"
        element={
          <AnalysisRoute>
            <ChatPage />
          </AnalysisRoute>
        }
      />
      <Route
        path="/a/:taskId/search"
        element={
          <AnalysisRoute>
            <SearchPage />
          </AnalysisRoute>
        }
      />
      <Route
        path="/a/:taskId/security"
        element={
          <AnalysisRoute>
            <ReviewPage category="security" title="Security Review" />
          </AnalysisRoute>
        }
      />
      <Route
        path="/a/:taskId/error-handling"
        element={
          <AnalysisRoute>
            <ReviewPage category="error_handling" title="Error Handling" />
          </AnalysisRoute>
        }
      />
      <Route
        path="/a/:taskId/structural"
        element={
          <AnalysisRoute>
            <ReviewPage category="structural" title="Structural" />
          </AnalysisRoute>
        }
      />
      <Route
        path="/a/:taskId/reports"
        element={
          <AnalysisRoute>
            <ReportsPage />
          </AnalysisRoute>
        }
      />
      <Route
        path="/a/:taskId/viewer"
        element={
          <AnalysisRoute>
            <ViewerPage />
          </AnalysisRoute>
        }
      />

      <Route path="*" element={<NotFound />} />
    </Routes>
  )
}
