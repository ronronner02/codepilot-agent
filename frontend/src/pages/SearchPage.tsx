/**
 * Code Search 页（U16，R-24~R-26）。
 *
 * **「未建索引」与「未找到」必须可区分**（R-25、AE-08）。两者在界面上都是「没有结果」，
 * 但下一步动作完全不同：前者要先跑一次分析，后者要换个问法。后端用两个 reason 区分，
 * 这里按 reason 分支呈现，文案不同且都非空。
 *
 * 距离原样显示，不换算成百分比——换算会造出一个后端没有的数字（BR-002）。
 */

import { useState } from 'react'
import { ApiRequestError, NetworkError, searchCode } from '../api/client'
import { CitationLink } from '../components/CitationLink'
import type { SearchResult } from '../api/types'
import { useAnalysis } from '../state/analysis'
import { usePageHeading } from './usePageHeading'

type State =
  | { kind: 'idle' }
  | { kind: 'searching' }
  | { kind: 'done'; result: SearchResult }
  | { kind: 'index_missing'; message: string }
  | { kind: 'error'; message: string }

export function SearchPage() {
  const headingRef = usePageHeading<HTMLHeadingElement>()
  const { taskId, ready } = useAnalysis()
  const [query, setQuery] = useState('')
  const [state, setState] = useState<State>({ kind: 'idle' })
  const [localError, setLocalError] = useState('')

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    const trimmed = query.trim()
    if (!trimmed) {
      // 本地拦住空查询：不发请求，也不让后端替我们做这个判断（省一次往返）。
      setLocalError('请输入检索内容')
      return
    }
    setLocalError('')
    setState({ kind: 'searching' })
    try {
      setState({ kind: 'done', result: await searchCode(taskId, trimmed) })
    } catch (cause) {
      if (cause instanceof ApiRequestError) {
        setState(
          cause.reason === 'index_missing'
            ? { kind: 'index_missing', message: cause.message }
            : { kind: 'error', message: cause.message },
        )
      } else {
        setState({
          kind: 'error',
          message: cause instanceof NetworkError ? cause.message : '检索失败，请重试。',
        })
      }
    }
  }

  if (!ready || !taskId) {
    return (
      <section className="page">
        <h1 className="page__title" ref={headingRef} tabIndex={-1}>
          Code Search
        </h1>
        <p className="page__empty">
          分析结果尚未就绪。
        </p>
      </section>
    )
  }

  return (
    <section className="page">
      <h1 className="page__title" ref={headingRef} tabIndex={-1}>
        Code Search
      </h1>
      {/* 不加「怎么用」的说明：输入框的 placeholder 已经是一个示例查询。 */}
      <form className="search__form" onSubmit={handleSubmit} noValidate>
        <label className="search__label" htmlFor="search-query">
          检索内容
        </label>
        <div className="search__row">
          <input
            id="search-query"
            className="search__field"
            type="text"
            value={query}
            placeholder="校验用户 token 的地方"
            onChange={(event) => {
              setQuery(event.target.value)
              if (localError) setLocalError('')
            }}
            disabled={state.kind === 'searching'}
            aria-invalid={localError ? true : undefined}
            aria-describedby={localError ? 'search-error' : undefined}
          />
          <button type="submit" disabled={state.kind === 'searching'}>
            {state.kind === 'searching' ? '检索中…' : '检索'}
          </button>
        </div>
        {localError ? (
          <p className="search__error" id="search-error" role="alert">
            {localError}
          </p>
        ) : null}
      </form>

      <div aria-live="polite">
        {state.kind === 'index_missing' ? (
          <p className="search__state" role="status">
            该仓库尚未建立向量索引，无法语义检索。{state.message}
          </p>
        ) : null}

        {state.kind === 'error' ? (
          <p className="search__state" role="alert">
            {state.message}
          </p>
        ) : null}

        {state.kind === 'done' && !state.result.found ? (
          // 与「未建索引」的文案必须不同（AE-08 明确两者不得混同）。
          <p className="search__state" role="status">
            未检索到相关代码。
          </p>
        ) : null}

        {state.kind === 'done' && state.result.found ? (
          <ul className="search__hits">
            {state.result.hits.map((hit, index) => (
              <li className="search__hit" key={`${hit.path}-${hit.start_line}-${index}`}>
                <div className="search__hit-head">
                  <CitationLink
                    taskId={taskId}
                    path={hit.path}
                    line={hit.start_line}
                    endLine={hit.end_line}
                  />
                  {hit.symbol ? (
                    <span className="search__hit-symbol">{hit.symbol}</span>
                  ) : null}
                  <span className="search__hit-distance">距离 {hit.distance.toFixed(3)}</span>
                </div>
                <pre className="search__hit-code">{hit.content}</pre>
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    </section>
  )
}
