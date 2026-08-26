/**
 * Code Search 与 AI Chat（U16，R-24~R-29）。
 *
 * 核心是「未建索引」与「未找到」不得混同（R-25、AE-08）：两者在界面上都是「没有结果」，但
 * 下一步动作完全不同——前者要先跑分析，后者要换问法。
 */

import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import {
  errorResponse,
  fullResult,
  json,
  renderAt,
  searchResult,
  seedCredentials,
  stubApi,
} from './fixtures'

beforeEach(() => {
  window.localStorage.clear()
  seedCredentials()
})

async function search(query = 'token 校验'): Promise<void> {
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('检索内容'), query)
  await user.click(screen.getByRole('button', { name: '检索' }))
}

describe('检索命中（AE-08）', () => {
  it('显示路径、行范围、符号名与代码片段', async () => {
    stubApi({ search: () => json(searchResult()) })
    renderAt('/a/t1/search')
    await screen.findByRole('heading', { name: 'Code Search', level: 1 })
    await search()

    expect(await screen.findByRole('link', { name: 'auth/jwt.py:45-78' })).toBeInTheDocument()
    expect(screen.getByText('verify_token')).toBeInTheDocument()
    expect(screen.getByText(/def verify_token/)).toBeInTheDocument()
  })

  it('距离原样显示，不换算成百分比', async () => {
    stubApi({ search: () => json(searchResult()) })
    renderAt('/a/t1/search')
    await screen.findByRole('heading', { name: 'Code Search', level: 1 })
    await search()

    expect(await screen.findByText(/距离 0\.210/)).toBeInTheDocument()
    // 不出现一个后端没有的百分数。
    expect(document.body.textContent).not.toContain('79%')
    expect(document.body.textContent).not.toContain('相关度')
  })
})

describe('两种空结果可区分（R-25、AE-08）', () => {
  it('未建索引时的文案指向「先跑分析」', async () => {
    stubApi({
      search: () => errorResponse('index_missing', '该仓库尚未建立向量索引', 409),
    })
    renderAt('/a/t1/search')
    await screen.findByRole('heading', { name: 'Code Search', level: 1 })
    await search()

    expect(
      await screen.findByText(/该仓库尚未建立向量索引，无法语义检索/),
    ).toBeInTheDocument()
    // 不得呈现为「未检索到相关代码」。
    expect(screen.queryByText(/未检索到相关代码/)).not.toBeInTheDocument()
  })

  it('未找到时的文案指向「换问法」，与上一条不同', async () => {
    stubApi({
      search: () => json(searchResult({ found: false, hits: [], reason: 'no_match' })),
    })
    renderAt('/a/t1/search')
    await screen.findByRole('heading', { name: 'Code Search', level: 1 })
    await search()

    expect(await screen.findByText(/未检索到相关代码/)).toBeInTheDocument()
    expect(screen.queryByText(/尚未建立向量索引/)).not.toBeInTheDocument()
  })

  it('向量化失败时给出错误说明，不冒充「未找到」', async () => {
    stubApi({
      search: () => errorResponse('embedding_failed', '向量化失败：上游超时', 502),
    })
    renderAt('/a/t1/search')
    await screen.findByRole('heading', { name: 'Code Search', level: 1 })
    await search()

    expect(await screen.findByText(/向量化失败：上游超时/)).toBeInTheDocument()
    expect(screen.queryByText(/未检索到相关代码/)).not.toBeInTheDocument()
  })
})

describe('输入与失败', () => {
  it('空查询被前端阻止，不发请求', async () => {
    const { calls } = stubApi()
    renderAt('/a/t1/search')
    await screen.findByRole('heading', { name: 'Code Search', level: 1 })

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '检索' }))

    expect(await screen.findByText('请输入检索内容')).toBeInTheDocument()
    expect(calls.filter((call) => call.includes('/search'))).toHaveLength(0)
  })

  it('检索失败后输入框仍可用', async () => {
    stubApi({ search: () => errorResponse('http_500', '服务端错误', 500) })
    renderAt('/a/t1/search')
    await screen.findByRole('heading', { name: 'Code Search', level: 1 })
    await search()

    await screen.findByText('服务端错误')
    expect(screen.getByLabelText('检索内容')).toBeEnabled()
    expect(screen.getByRole('button', { name: '检索' })).toBeEnabled()
  })

  it('未就绪时给出空态说明', async () => {
    stubApi({ result: () => json(fullResult({ completed: false })) })
    renderAt('/a/t1/search')
    expect(await screen.findByText(/分析结果尚未就绪/)).toBeInTheDocument()
  })
})

describe('AI Chat（AE-20）', () => {
  it('界面说明每次提问独立处理、不保留上下文（R-29）', async () => {
    stubApi()
    renderAt('/a/t1/chat')
    // R-29 的单轮语义说明由 QaPanel 给出，ChatPage 不重复一遍。
    expect(await screen.findByText(/单轮问答，不保留上下文/)).toBeInTheDocument()
  })

  it('未找到时不显示引用区', async () => {
    stubApi({
      question: () =>
        json({
          question: 'q',
          found: false,
          answer: '未在该仓库中找到与问题相关的代码。',
          citations: [],
          note: 'below_threshold',
        }),
    })
    renderAt('/a/t1/chat')
    await screen.findByRole('heading', { name: '代码问答' })

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('关于这个仓库的问题'), '怎么部署')
    await user.click(screen.getByRole('button', { name: '提问' }))

    expect(await screen.findByText(/未在该仓库中找到与问题相关的代码/)).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '引用' })).not.toBeInTheDocument()
  })

  it('找到时引用含路径、起止行与符号名', async () => {
    stubApi({
      question: () =>
        json({
          question: 'q',
          found: true,
          answer: '在 auth/jwt.py。',
          citations: [
            {
              path: 'auth/jwt.py',
              start_line: 45,
              end_line: 78,
              symbol: 'verify_token',
              distance: 0.2,
            },
          ],
          note: '',
        }),
    })
    renderAt('/a/t1/chat')
    await screen.findByRole('heading', { name: '代码问答' })

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('关于这个仓库的问题'), '怎么校验')
    await user.click(screen.getByRole('button', { name: '提问' }))

    expect(await screen.findByRole('heading', { name: '引用' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'auth/jwt.py:45-78' })).toBeInTheDocument()
    expect(screen.getByText('verify_token')).toBeInTheDocument()
  })

  it('提问请求带上访客凭证（BR-004）', async () => {
    let body = ''
    stubApi({
      question: () => json({ question: 'q', found: false, answer: '', citations: [], note: '' }),
    })
    // 包一层记录请求体的 fetch。
    const original = globalThis.fetch
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET') === 'POST' && String(input).endsWith('/questions')) {
        body = String(init?.body ?? '')
      }
      return original(input, init)
    }) as typeof fetch

    renderAt('/a/t1/chat')
    await screen.findByRole('heading', { name: '代码问答' })

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('关于这个仓库的问题'), '怎么校验')
    await user.click(screen.getByRole('button', { name: '提问' }))

    await screen.findByText(/正在检索相关代码|未在该仓库/, {}, { timeout: 3000 }).catch(() => {})
    expect(body).toContain('guest-key-1234')
  })

  it('分析未完成时呈现「分析尚未完成」而非通用错误', async () => {
    stubApi({
      question: () => errorResponse('analysis_incomplete', '分析尚未完成', 409),
    })
    renderAt('/a/t1/chat')
    await screen.findByRole('heading', { name: '代码问答' })

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('关于这个仓库的问题'), '怎么校验')
    await user.click(screen.getByRole('button', { name: '提问' }))

    expect(await screen.findByText('分析尚未完成，暂时不能提问。')).toBeInTheDocument()
  })

  it('未就绪时给出空态', async () => {
    stubApi({ result: () => json(fullResult({ completed: false })) })
    renderAt('/a/t1/chat')
    expect(await screen.findByText(/分析结果尚未就绪/)).toBeInTheDocument()
  })
})
