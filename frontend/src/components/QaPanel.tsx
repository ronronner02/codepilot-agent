/**
 * 单轮问答。
 *
 * **未找到时不呈现空回答框**（计划的测试场景）。后端把 found 与 answer 分开正是为此：
 * 未找到时 answer 是说明文字，把它塞进「回答」区并配一个空引用列表，会让用户以为系统
 * 答了但答得很差——而实际是没找到相关代码，那是两种不同的情况。
 */

import { useId, useState } from 'react'
import { ApiRequestError, NetworkError, askQuestion } from '../api/client'
import type { Answer } from '../api/types'
import { loadCredentials } from '../settings/credentials'
import { CitationLink } from './CitationLink'

interface Props {
  taskId: string
}

export function QaPanel({ taskId }: Props) {
  const [question, setQuestion] = useState('')
  const [answer, setAnswer] = useState<Answer | null>(null)
  const [asking, setAsking] = useState(false)
  const [error, setError] = useState('')
  const inputId = useId()
  const errorId = useId()

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    const trimmed = question.trim()
    if (!trimmed) {
      setError('请输入问题')
      return
    }

    setAsking(true)
    setError('')
    setAnswer(null)
    try {
      // 问答同样用访客凭证（BR-004）。embedding 侧仍用服务端配置，那是后端的分流。
      setAnswer(await askQuestion(taskId, trimmed, loadCredentials()))
    } catch (cause) {
      if (cause instanceof ApiRequestError) {
        setError(
          cause.reason === 'analysis_incomplete'
            ? '分析尚未完成，暂时不能提问。'
            : cause.message,
        )
      } else if (cause instanceof NetworkError) {
        setError(cause.message)
      } else {
        setError('提问失败，请重试。')
      }
    } finally {
      setAsking(false)
    }
  }

  return (
    <section className="qa" aria-labelledby="qa-heading">
      <h2 className="qa__heading" id="qa-heading">
        代码问答
      </h2>
      <p className="qa__note">单轮问答，不保留上下文。回答的引用来自检索结果，非模型自行生成。</p>

      <form className="qa__form" onSubmit={handleSubmit} noValidate>
        <label className="qa__label" htmlFor={inputId}>
          关于这个仓库的问题
        </label>
        <div className="qa__row">
          <input
            id={inputId}
            className="qa__field"
            type="text"
            value={question}
            placeholder="认证逻辑在哪里实现的"
            onChange={(event) => {
              setQuestion(event.target.value)
              if (error) setError('')
            }}
            disabled={asking}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? errorId : undefined}
          />
          <button type="submit" disabled={asking}>
            {asking ? '检索中…' : '提问'}
          </button>
        </div>
        {error ? (
          <p className="qa__error" id={errorId} role="alert">
            {error}
          </p>
        ) : null}
      </form>

      <div aria-live="polite">
        {asking ? <p className="qa__pending">正在检索相关代码…</p> : null}

        {answer && !answer.found ? (
          // 未找到：只显示说明，不显示回答框与引用区。
          <div className="qa__not-found" role="status">
            <p className="qa__not-found-text">{answer.answer}</p>
            <p className="qa__not-found-hint">
              可以换个更具体的问法，或确认这个仓库是否确实包含相关实现。
            </p>
          </div>
        ) : null}

        {answer && answer.found ? (
          <div className="qa__answer">
            <p className="qa__answer-text">{answer.answer}</p>
            {answer.citations.length > 0 ? (
              <div className="qa__citations">
                <h3 className="qa__citations-title">引用</h3>
                <ul>
                  {answer.citations.map((citation, index) => (
                    <li key={`${citation.path}-${index}`}>
                      {/* 引用可点跳转到查看器（U13、R-28）。 */}
                      <CitationLink
                        taskId={taskId}
                        path={citation.path}
                        line={citation.start_line}
                        endLine={citation.end_line}
                      />
                      {citation.symbol ? (
                        <span className="qa__citation-symbol">{citation.symbol}</span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {answer.note ? <p className="qa__answer-note">{answer.note}</p> : null}
          </div>
        ) : null}
      </div>
    </section>
  )
}
