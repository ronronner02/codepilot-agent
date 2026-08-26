/**
 * 测试环境准备。
 *
 * jsdom 不实现 EventSource（它是浏览器 API）。测试要验证 SSE 的连接、清理与断连处理，
 * 所以这里放一个可控的替身：测试能主动推事件、触发错误，并断言 close 被调用。
 *
 * 用替身而非跑真实 SSE 服务：要验证的是前端的生命周期管理（卸载后回调、重复连接），
 * 那与服务端行为无关，而起服务会让测试变慢且不确定。
 */

import '@testing-library/jest-dom/vitest'
import { afterEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'

/** 一个受测试控制的 EventSource 替身。 */
export class MockEventSource {
  static instances: MockEventSource[] = []

  readonly url: string
  onerror: ((event: Event) => void) | null = null
  closed = false
  /** close 被调用的次数。用来断言清理逻辑的幂等性。 */
  closeCount = 0

  private listeners = new Map<string, Set<(event: MessageEvent<string>) => void>>()

  constructor(url: string) {
    this.url = url
    MockEventSource.instances.push(this)
  }

  addEventListener(type: string, handler: (event: MessageEvent<string>) => void): void {
    const set = this.listeners.get(type) ?? new Set()
    set.add(handler)
    this.listeners.set(type, set)
  }

  removeEventListener(type: string, handler: (event: MessageEvent<string>) => void): void {
    this.listeners.get(type)?.delete(handler)
  }

  close(): void {
    this.closed = true
    this.closeCount += 1
  }

  /** 测试用：推一条 progress 事件。 */
  emit(payload: unknown): void {
    const event = new MessageEvent('progress', { data: JSON.stringify(payload) })
    for (const handler of this.listeners.get('progress') ?? []) {
      handler(event)
    }
  }

  /** 测试用：推一条无法解析的数据，验证单条坏事件不中断整个流。 */
  emitRaw(data: string): void {
    const event = new MessageEvent('progress', { data })
    for (const handler of this.listeners.get('progress') ?? []) {
      handler(event)
    }
  }

  /** 测试用：触发连接错误。 */
  fail(): void {
    this.onerror?.(new Event('error'))
  }

  static latest(): MockEventSource {
    const instance = MockEventSource.instances.at(-1)
    if (!instance) throw new Error('尚未创建任何 EventSource')
    return instance
  }

  static reset(): void {
    MockEventSource.instances = []
  }
}

vi.stubGlobal('EventSource', MockEventSource)

/**
 * ResizeObserver 的空实现。
 *
 * jsdom 不提供它，而 React Flow（U11 的节点图）在挂载时就会构造一个——缺了它组件直接抛异常，
 * 整个 Architecture 页的测试都跑不起来。
 *
 * **这个 mock 只让组件挂载不抛，不让图的渲染变得可断言。** jsdom 没有布局引擎，所有元素尺寸
 * 恒为 0，而 React Flow 依赖容器尺寸决定渲染什么。图的正确性断言落在同页的等价文本表达上
 * （KTD8），图形本身的可用性由浏览器实跑核对承担。
 */
class MockResizeObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

vi.stubGlobal('ResizeObserver', MockResizeObserver)

// DOMMatrixReadOnly 同理：React Flow 的缩放平移会用到它。
if (typeof globalThis.DOMMatrixReadOnly === 'undefined') {
  class MockDOMMatrix {
    m22 = 1
    constructor(_transform?: string) {}
  }
  vi.stubGlobal('DOMMatrixReadOnly', MockDOMMatrix)
}

afterEach(() => {
  cleanup()
  MockEventSource.reset()
  vi.restoreAllMocks()
})
