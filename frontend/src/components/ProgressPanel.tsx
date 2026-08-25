/**
 * 进度区。
 *
 * `aria-live="polite"` 播报阶段变化（计划的可访问交互要求）。用 polite 而非 assertive：
 * 进度更新频繁，assertive 会打断用户正在听的内容。
 *
 * 播报的是阶段标签而非每条 detail：detail 变化太频繁（模块分析阶段每个模块一条），
 * 全播报会变成噪声。所以只有 label 在 live 区内，detail 放在 live 区外。
 */

import { useEffect, useRef } from 'react'
import type { ProgressEvent } from '../api/types'

interface Props {
  events: ProgressEvent[]
  disconnected: boolean
  onReconnect: () => void
  /** 提交后焦点移到这里（计划要求），完成后由 App 移到报告标题。 */
  focusOnMount: boolean
}

export function ProgressPanel({ events, disconnected, onReconnect, focusOnMount }: Props) {
  const headingRef = useRef<HTMLHeadingElement>(null)
  const latest = events.at(-1)

  useEffect(() => {
    if (focusOnMount) headingRef.current?.focus()
  }, [focusOnMount])

  /**
   * 断连提示。
   *
   * **必须在「还没有任何事件」的路径上也渲染。** 首个事件到达前就断连是常见情形——后端
   * 没起、任务标识失效、代理挂了。早返回分支若忽略它，用户会永远停在「正在启动分析…」
   * 上而看不出异常，那正是计划要求避免的「静默停在进度条上」。
   */
  const disconnectNotice = disconnected ? (
    <div className="progress__disconnected" role="alert">
      <p>进度连接已断开。分析可能仍在后台进行。</p>
      <button type="button" onClick={onReconnect}>
        重新连接
      </button>
    </div>
  ) : null

  if (!latest) {
    return (
      <section className="progress" aria-labelledby="progress-heading">
        <h2 className="progress__heading" id="progress-heading" ref={headingRef} tabIndex={-1}>
          分析进度
        </h2>
        <p className="progress__status" role="status">
          {disconnected ? '尚未收到进度就已断开连接。' : '正在启动分析…'}
        </p>
        {disconnectNotice}
      </section>
    )
  }

  const percent = latest.percent
  return (
    <section className="progress" aria-labelledby="progress-heading">
      <h2 className="progress__heading" id="progress-heading" ref={headingRef} tabIndex={-1}>
        分析进度
      </h2>

      {/* role="status" 隐含 aria-live="polite"：它就是状态消息，用角色比裸属性更语义化，
          也让读屏在不同实现下行为一致。只有阶段标签在这里——detail 每个模块一条，
          全播报会变成噪声。 */}
      <p className="progress__status" role="status">
        {latest.label}
      </p>

      {percent === null ? (
        // 给不出百分比时用不确定态，不假装精确。
        <div className="progress__bar progress__bar--indeterminate" role="presentation" />
      ) : (
        <div
          className="progress__bar"
          role="progressbar"
          aria-valuenow={percent}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label={`分析进度 ${percent}%`}
        >
          <div className="progress__fill" style={{ width: `${percent}%` }} />
        </div>
      )}

      {latest.detail ? <p className="progress__detail">{latest.detail}</p> : null}

      {/* 计划明确要求：断连要有可见状态与重连入口，不静默停在进度条上。 */}
      {disconnectNotice}

      <details className="progress__history">
        <summary>已完成的阶段（{events.length}）</summary>
        <ol className="progress__list">
          {events.map((event, index) => (
            <li
              key={`${event.stage}-${index}`}
              className={event.failed ? 'progress__item progress__item--failed' : 'progress__item'}
            >
              <span className="progress__item-label">{event.label}</span>
              {event.detail ? <span className="progress__item-detail">{event.detail}</span> : null}
            </li>
          ))}
        </ol>
      </details>
    </section>
  )
}
