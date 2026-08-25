/**
 * SSE 客户端。连接、重连与清理。
 *
 * 计划把这里点名为前端最容易出竞态的地方，具体是三种：
 *
 * **卸载后回调仍触发。** EventSource 的 onmessage 是异步的，组件卸载后仍可能有一次
 * 回调在途。React 会对卸载后的 setState 报警，而更实际的问题是那次更新会写进已废弃的
 * 状态。对策是 closed 标志——close() 之后所有回调直接返回。
 *
 * **重复连接。** 连接失败后重连，若旧连接没关就会有两个 EventSource 同时推事件，
 * 界面会看到重复或乱序的阶段。对策是每次连接前先关旧的，且 connect 是幂等的。
 *
 * **自动重连掩盖真实断连。** EventSource 原生会自动重连，但它不告诉调用方「正在重连」
 * ——界面会停在进度条上看不出异常。计划明确要求「SSE 连接断开时呈现断连状态并提供重连」，
 * 所以这里禁用隐式重连：onerror 时关闭连接并回调 onDisconnect，让界面决定要不要重连。
 */

import type { ProgressEvent } from './types'

export interface SseHandlers {
  onEvent: (event: ProgressEvent) => void
  /** 服务端正常结束（流关闭且已收到终止事件）。 */
  onComplete: () => void
  /** 连接异常断开。带上是否收到过事件——首次连接就失败与中途断开的处理不同。 */
  onDisconnect: (receivedAny: boolean) => void
}

export interface SseConnection {
  close: () => void
}

/**
 * 连上任务的进度流。
 *
 * 返回的 close() 必须在组件卸载时调用。它是幂等的——重复调用无害，这让清理逻辑不必
 * 判断当前状态。
 */
export function connectProgress(taskId: string, handlers: SseHandlers): SseConnection {
  let closed = false
  let receivedAny = false
  let terminated = false

  const source = new EventSource(`/api/analyses/${encodeURIComponent(taskId)}/events`)

  const close = (): void => {
    if (closed) return
    closed = true
    source.close()
  }

  source.addEventListener('progress', (raw) => {
    // 卸载后到达的回调直接丢弃：写进已废弃的状态既触发 React 警告，也会让界面显示
    // 属于上一次分析的进度。
    if (closed) return

    let event: ProgressEvent
    try {
      event = JSON.parse((raw as MessageEvent<string>).data) as ProgressEvent
    } catch {
      // 单条事件解析失败不该中断整个流——后续事件仍有价值。
      return
    }

    receivedAny = true
    handlers.onEvent(event)

    if (event.stage === 'done' || event.stage === 'failed') {
      // 收到终止事件即主动关闭，不等服务端断流。这让 onComplete 的时机确定，
      // 也避免 EventSource 在流关闭时触发 onerror 而被误判为异常断连。
      terminated = true
      close()
      handlers.onComplete()
    }
  })

  source.onerror = () => {
    if (closed) return
    // 已收到终止事件后的 error 是正常的流关闭，不是断连。
    if (terminated) {
      close()
      return
    }
    // 禁用 EventSource 的隐式重连：它会静默重试而不告知调用方，界面就停在进度条上
    // 看不出异常。关掉并上报，由界面提供重连入口。
    close()
    handlers.onDisconnect(receivedAny)
  }

  return { close }
}
