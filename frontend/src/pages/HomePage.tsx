/**
 * 首页（U9，R-41~R-46）。
 *
 * 仓库输入框加「最近分析」列表。复用 `RepoInput`（提交、禁用态、禁用原因均已实现）。
 *
 * **列表只列已完成的分析**（R-46）。在跑与排队中的不列，界面也不给「继续」「恢复」入口——
 * 任务状态在进程内存里，重启后未完成的任务无法恢复，给入口等于承诺一件做不到的事。
 *
 * **列表故障不阻塞新提交**：历史读取失败时显示错误说明而输入框仍可用。两者是独立能力，
 * 把它们绑在一起会让一个只读接口的故障挡住核心功能。
 */

import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Link } from 'react-router-dom'
import { ApiRequestError, NetworkError, fetchAnalyses } from '../api/client'
import { FailureNotice } from '../components/FailureNotice'
import { RepoInput } from '../components/RepoInput'
import type { AnalysisSummary } from '../api/types'
import { analysisPath } from '../routes/paths'
import { SETTINGS } from '../routes/paths'
import { isConfigured, loadCredentials } from '../settings/credentials'
import { useAnalysis } from '../state/analysis'
import { usePageHeading } from './usePageHeading'

function formatTime(seconds: number): string {
  if (!seconds) return '时间未知'
  return new Date(seconds * 1000).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function HomePage() {
  const headingRef = usePageHeading<HTMLHeadingElement>()
  const navigate = useNavigate()
  const { phase, failure, submit } = useAnalysis()
  const [history, setHistory] = useState<AnalysisSummary[] | null>(null)
  const [historyError, setHistoryError] = useState('')
  const [credentialsMissing, setCredentialsMissing] = useState(false)

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const listed = await fetchAnalyses()
        if (cancelled) return
        // 只列已终止的分析（R-46）：在跑与排队中的不列，因为它们随进程内存易失，
        // 列出来就等于暗示可以恢复。失败的**要**列——它确实发生过，凭空消失会让刚提交
        // 失败的用户找不到任何记录。列表里给失败标记区分。
        setHistory(
          listed
            .filter((item) => item.completed)
            .sort((a, b) => b.created_at - a.created_at),
        )
      } catch (cause) {
        if (cancelled) return
        setHistory([])
        setHistoryError(
          cause instanceof ApiRequestError || cause instanceof NetworkError
            ? cause.message
            : '读取历史分析失败',
        )
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const busy = phase === 'submitting' || phase === 'analyzing'

  const handleSubmit = async (repoUrl: string) => {
    // 未配置凭证时前端先拦（R-49 的前端侧）。后端也拦（R-68），但那要一次往返，而这里
    // 能立刻把用户引到设置页。
    if (!isConfigured(loadCredentials())) {
      setCredentialsMissing(true)
      return
    }
    setCredentialsMissing(false)
    const taskId = await submit(repoUrl)
    if (taskId) navigate(analysisPath(taskId, 'overview'))
  }

  return (
    <section className="page">
      <h1 className="page__title" ref={headingRef} tabIndex={-1}>
        分析一个仓库
      </h1>

      <RepoInput
        disabled={busy}
        disabledReason={phase === 'submitting' ? '正在提交…' : '分析进行中。'}
        onSubmit={(repoUrl) => void handleSubmit(repoUrl)}
      />

      {credentialsMissing ? (
        <div className="failure" role="alert">
          <p className="failure__message">
            还没有配置 LLM 凭证。本服务使用你自己的 API key 调用模型，服务端不保存它。
          </p>
          <Link className="failure__hint" to={SETTINGS}>
            去设置页填写凭证
          </Link>
        </div>
      ) : null}

      {/* 提交被拒时用户还在这一页（那一刻还没有 taskId 可跳转），失败要在这里呈现。 */}
      {failure ? <FailureNotice reason={failure.reason} message={failure.message} /> : null}

      <h2 className="page__section-title">最近分析</h2>

      {historyError ? (
        <p className="page__note" role="status">
          {historyError}（历史列表不可用不影响提交新分析）
        </p>
      ) : null}

      {history === null ? (
        <p className="page__note">正在载入…</p>
      ) : history.length === 0 ? (
        <p className="page__empty">暂无历史分析。</p>
      ) : (
        <ul className="history">
          {history.map((item) => (
            <li className="history__item" key={item.task_id}>
              <Link className="history__link" to={analysisPath(item.task_id, 'overview')}>
                {item.repo}
              </Link>
              <span className="history__meta">
                {item.commit_sha ? item.commit_sha.slice(0, 12) : 'commit 未知'}
              </span>
              <span className="history__meta">{formatTime(item.created_at)}</span>
              <span className="history__meta">{item.file_count} 个文件</span>
              <span className="history__meta">{item.finding_count} 条发现</span>
              {item.failed ? (
                // 失败的分析仍在列表里，但要看得出来——否则点进去是一个空报告页，
                // 而用户会以为界面坏了。
                <span className="history__failed">分析失败</span>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
