/**
 * AI Chat 页（U16，R-27~R-29）。
 *
 * 复用 `QaPanel`（found/answer 分离呈现、未找到时不显示引用区均已实现）。本页新增的只有
 * 单轮语义的显式说明（R-29）——界面明说每次提问独立处理、不保留上下文，否则用户会按多轮
 * 对话的预期使用它，然后困惑于「它忘了我上一句问什么」。
 */

import { QaPanel } from '../components/QaPanel'
import { useAnalysis } from '../state/analysis'
import { usePageHeading } from './usePageHeading'

export function ChatPage() {
  const headingRef = usePageHeading<HTMLHeadingElement>()
  const { taskId, ready } = useAnalysis()

  return (
    <section className="page">
      <h1 className="page__title" ref={headingRef} tabIndex={-1}>
        AI Chat
      </h1>

      {ready && taskId ? (
        <>
          {/* 单轮语义的说明（R-29）由 QaPanel 自己给出，这里不再重复一遍。 */}
          <QaPanel taskId={taskId} />
        </>
      ) : (
        <p className="page__empty">
          分析结果尚未就绪。
        </p>
      )}
    </section>
  )
}
