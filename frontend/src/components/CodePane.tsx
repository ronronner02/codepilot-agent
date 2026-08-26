/**
 * 代码正文（U12，R-16、R-18、R-22）。
 *
 * 带行号，跳转目标行高亮。行号与代码用两列而非在每行文本前拼一个数字：拼进去的话用户复制
 * 代码会连行号一起复制。
 *
 * **截断必须可见**（R-18）。端点返回截断说明时显示「已截断，共 M 行」，不静默——静默截断
 * 会让读者以为文件就这么长，而据此得出的结论是错的。
 *
 * 不做语法高亮：那需要再引一个库（Prism/Shiki 都是几十 KB 起），而本期的目的是「点进去能
 * 核验引用」，纯文本加行号已经够。
 */

import { useEffect, useRef } from 'react'
import type { FileContent } from '../api/types'

interface Props {
  file: FileContent
  /** 要高亮并滚到可见的行。0 表示不定位（问题属于文件整体，R-22）。 */
  highlightLine: number
}

export function CodePane({ file, highlightLine }: Props) {
  const targetRef = useRef<HTMLDivElement>(null)
  const lines = file.content.length > 0 ? file.content.split('\n') : []

  useEffect(() => {
    if (highlightLine > 0) {
      // scrollIntoView 在 jsdom 下可能缺失，可选调用即可——高亮不依赖滚动成功。
      targetRef.current?.scrollIntoView?.({ block: 'center' })
    }
  }, [highlightLine, file.path])

  return (
    <div className="code">
      <div className="code__head">
        <code className="code__path">{file.path}</code>
        <span className="code__meta">共 {file.total_lines} 行</span>
      </div>

      {file.truncated ? (
        <p className="code__truncated" role="status">
          已截断，共 {file.total_lines} 行。{file.truncated_note}
        </p>
      ) : null}

      {lines.length === 0 ? (
        <p className="code__empty">这是一个空文件。</p>
      ) : (
        <div className="code__body">
          {lines.map((text, index) => {
            const lineNumber = file.start_line + index
            const isTarget = lineNumber === highlightLine
            return (
              <div
                key={lineNumber}
                className={isTarget ? 'code__line code__line--on' : 'code__line'}
                ref={isTarget ? targetRef : undefined}
              >
                <span className="code__gutter" aria-hidden="true">
                  {lineNumber}
                </span>
                <span className="code__text">{text}</span>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
