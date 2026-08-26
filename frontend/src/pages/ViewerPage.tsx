/**
 * 代码查看器（U12，R-15~R-22）。
 *
 * 三栏：文件树 + 带行号代码 + 该文件的评审发现。当前文件与目标行号从查询参数读——那让引用
 * 下钻成为一次普通的路由跳转（U13），不需要跨页传状态。
 *
 * **右栏不触发任何 LLM 调用**（R-21、NA-07）：它只按 path 过滤已有的 `review.findings`。
 * 本页的请求只有两类——文件树与文件内容，都是纯文件系统读取。
 *
 * 路径校验失败时不回显拒绝细节（origin 的 Exception Handling）：这个端点在公网上，逐段说明
 * 「哪一段是符号链接」等于提供探测反馈。
 */

import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ApiRequestError, NetworkError, fetchFile } from '../api/client'
import { CodePane } from '../components/CodePane'
import { FileFindings } from '../components/FileFindings'
import { FileTree } from '../components/FileTree'
import type { FileContent } from '../api/types'
import { useAnalysis } from '../state/analysis'
import { usePageHeading } from './usePageHeading'

/** 读取失败的分类文案。按 reason 而非 message 分类（reason 是稳定契约）。 */
const READ_ERRORS: Record<string, string> = {
  invalid_path: '无法读取该路径。',
  file_not_found: '文件不存在。它可能在分析之后被删除了。',
  not_a_file: '这是一个目录，不是文件。',
  file_too_large: '文件体积超出查看上限，通常是生成物或数据文件。',
  workspace_cleared: '代码副本已清理，无法查看源文件。',
  workspace_unavailable: '代码副本尚未就绪，请等待克隆与解析完成。',
  read_failed: '读取失败。',
}

export function ViewerPage() {
  const headingRef = usePageHeading<HTMLHeadingElement>()
  const { result, taskId, ready } = useAnalysis()
  const [params, setParams] = useSearchParams()
  const currentPath = params.get('path') ?? ''
  const targetLine = Number(params.get('line') ?? '0') || 0

  const [file, setFile] = useState<FileContent | null>(null)
  const [error, setError] = useState<{ reason: string; message: string } | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!taskId || !currentPath) {
      setFile(null)
      return
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    void (async () => {
      try {
        const loaded = await fetchFile(taskId, currentPath)
        if (!cancelled) setFile(loaded)
      } catch (cause) {
        if (cancelled) return
        setFile(null)
        if (cause instanceof ApiRequestError) {
          setError({ reason: cause.reason, message: cause.message })
        } else {
          setError({
            reason: 'client_offline',
            message: cause instanceof NetworkError ? cause.message : '读取失败',
          })
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [taskId, currentPath])

  const openFile = (path: string) => {
    // 换文件时清掉行号：上一个文件的目标行在新文件里没有意义。
    setParams({ path })
  }

  if (!ready || !taskId) {
    return (
      <section className="page">
        <h1 className="page__title" ref={headingRef} tabIndex={-1}>
          代码查看器
        </h1>
        <p className="page__empty">分析结果尚未就绪。完成一次分析后可浏览源文件。</p>
      </section>
    )
  }

  const cleared =
    error?.reason === 'workspace_cleared' || error?.reason === 'workspace_unavailable'

  return (
    <section className="page page--viewer">
      <h1 className="page__title" ref={headingRef} tabIndex={-1}>
        代码查看器
      </h1>

      <div className="viewer">
        <FileTree
          taskId={taskId}
          currentPath={currentPath}
          onOpen={openFile}
          onError={(reason, message) => setError({ reason, message })}
        />

        <div className="viewer__main">
          {loading ? <p className="page__note">载入中…</p> : null}

          {error ? (
            <p className="page__note" role="status">
              {READ_ERRORS[error.reason] ?? error.message}
            </p>
          ) : null}

          {file ? (
            <CodePane file={file} highlightLine={targetLine} />
          ) : !error && !loading ? (
            <p className="page__empty">从左侧文件树选一个文件查看内容。</p>
          ) : null}
        </div>

        <FileFindings
          taskId={taskId}
          path={currentPath}
          review={result?.review ?? null}
          workspaceCleared={cleared}
        />
      </div>
    </section>
  )
}
