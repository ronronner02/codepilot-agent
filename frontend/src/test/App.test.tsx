/**
 * 界面行为（R22、AE6）。
 *
 * 覆盖计划列出的八个场景。重点在两类：
 *
 * **状态机的边界。** 重复提交被阻止、四类失败可区分——这些是「界面显示了不该显示的东西」
 * 类缺陷，只有行为测试能抓。
 *
 * **SSE 的生命周期。** 卸载后回调、重复连接、断连呈现。计划把这里点名为最容易出竞态的
 * 地方，而竞态在手动点击时往往碰不到。
 */

import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { App } from '../App'
import type { AnalysisResult, ProgressEvent } from '../api/types'
import { MockEventSource } from './setup'

const REPO = 'https://github.com/acme/widget'

function progress(overrides: Partial<ProgressEvent> = {}): ProgressEvent {
  return {
    stage: 'parsing',
    label: '解析代码',
    detail: '',
    percent: 20,
    failed: false,
    ...overrides,
  }
}

function result(overrides: Partial<AnalysisResult> = {}): AnalysisResult {
  return {
    task_id: 't1',
    repo: 'acme/widget',
    stage: 'done',
    completed: true,
    failed: false,
    error: '',
    report: {
      repo: 'acme/widget',
      commit_sha: 'abcdef123456789',
      summary: '总体印象文本',
      sections: [
        {
          key: 'module_breakdown',
          title: '模块划分',
          claims: [
            {
              text: '核心逻辑集中在 pkg/core.py',
              citations: [{ path: 'pkg/core.py', line: 10, end_line: 42 }],
            },
          ],
        },
      ],
      missing: { text: '本次分析无缺失部分', unparsed_files: 0, skipped_modules: 0 },
      validation_summary: '共 1 条结论：1 条引用完全有效。',
      unsupported_claims: [],
    },
    review: {
      target_files: ['pkg/core.py'],
      outcomes: [
        {
          category: 'structural',
          status: 'executed',
          scope: '1 个文件；检查项：循环依赖',
          hit_count: 1,
          reason: '',
        },
        {
          category: 'security',
          status: 'skipped',
          scope: '1 个文件；模式集：硬编码密钥',
          hit_count: 0,
          reason: '未配置 LLM provider',
        },
      ],
      findings: [
        {
          category: 'structural',
          kind: 'circular_dependency',
          path: 'pkg/a.py',
          line: 0,
          severity: 'high',
          message: '3 个文件构成循环依赖',
          evidence: '依赖图上的有向环',
        },
      ],
    },
    index: { cache_hit: false, chunk_count: 12, identity: 'api:m:v1', note: '新建索引' },
    module_failures: [],
    ...overrides,
  }
}

/** 装一个 fetch 替身。返回被调用的请求列表，供断言「没发第二次请求」。 */
function stubFetch(handlers: {
  submit?: () => Promise<Response> | Response
  result?: () => Promise<Response> | Response
  question?: () => Promise<Response> | Response
}): { calls: string[] } {
  const calls: string[] = []
  const json = (body: unknown, status = 200): Response =>
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    })

  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString()
      const method = init?.method ?? 'GET'
      calls.push(`${method} ${url}`)

      if (method === 'POST' && url.endsWith('/questions')) {
        return handlers.question?.() ?? json({})
      }
      if (method === 'POST') {
        return (
          handlers.submit?.() ??
          json({ task_id: 't1', repo: 'acme/widget', stage: 'queued', message: 'ok' })
        )
      }
      return handlers.result?.() ?? json(result())
    }),
  )
  return { calls }
}

/**
 * 推一条 SSE 事件。
 *
 * 必须包 act()：事件来自非 React 的事件源（EventSource），它触发的 setState 不经过
 * React 的事件系统。不包的话 React 不会处理这次更新，DOM 永远不变——表现为 findByText
 * 超时，而组件代码其实是对的。
 */
async function emitProgress(payload: ProgressEvent): Promise<void> {
  await act(async () => {
    MockEventSource.latest().emit(payload)
  })
}

async function emitRawProgress(data: string): Promise<void> {
  await act(async () => {
    MockEventSource.latest().emitRaw(data)
  })
}

async function failConnection(source = MockEventSource.latest()): Promise<void> {
  await act(async () => {
    source.fail()
  })
}

async function submitRepo(url = REPO): Promise<void> {
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('GitHub 仓库地址'), url)
  await user.click(screen.getByRole('button', { name: '开始分析' }))
}

describe('提交', () => {
  beforeEach(() => {
    stubFetch({})
  })

  it('空地址被表单阻止并提示，不发请求', async () => {
    const { calls } = stubFetch({})
    render(<App />)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '开始分析' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('请输入 GitHub 仓库地址')
    expect(calls).toHaveLength(0)
  })

  it('只有空白的地址同样被阻止', async () => {
    const { calls } = stubFetch({})
    render(<App />)
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('GitHub 仓库地址'), '   ')
    await user.click(screen.getByRole('button', { name: '开始分析' }))

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(calls).toHaveLength(0)
  })

  it('提交后进入分析中，出现进度区', async () => {
    render(<App />)
    await submitRepo()
    expect(await screen.findByRole('heading', { name: '分析进度' })).toBeInTheDocument()
  })

  it('输入错误后重新输入会清掉提示', async () => {
    render(<App />)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '开始分析' }))
    expect(await screen.findByRole('alert')).toBeInTheDocument()

    await user.type(screen.getByLabelText('GitHub 仓库地址'), 'h')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})

describe('重复提交', () => {
  it('分析中按钮被禁用，且给出禁用原因', async () => {
    stubFetch({})
    render(<App />)
    await submitRepo()

    const button = await screen.findByRole('button', { name: '分析中…' })
    expect(button).toBeDisabled()
    expect(screen.getByText(/分析进行中/)).toBeInTheDocument()
  })

  it('分析中再次提交不发起第二次请求', async () => {
    const { calls } = stubFetch({})
    render(<App />)
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })

    const submitCalls = calls.filter((c) => c.startsWith('POST')).length
    const user = userEvent.setup()
    // 按钮已禁用，点击不会触发；这里额外用回车走一遍表单提交路径。
    await user.keyboard('{Enter}')

    expect(calls.filter((c) => c.startsWith('POST')).length).toBe(submitCalls)
  })
})

describe('进度', () => {
  it('进度区随 SSE 事件更新阶段文本', async () => {
    stubFetch({})
    render(<App />)
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })

    await emitProgress(progress({ stage: 'skeleton_ready', label: '结构骨架就绪' }))
    // 用 role="status" 定位当前阶段：同一文本也会出现在历史列表里，按文本查会有歧义。
    expect(await screen.findByRole('status')).toHaveTextContent('结构骨架就绪')

    await emitProgress(
      progress({ stage: 'analyzing_modules', label: '分析模块', detail: 'm0 分析完成（1/3）' }),
    )
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('分析模块'))
    expect(screen.getByText('m0 分析完成（1/3）', { selector: '.progress__detail' })).toBeInTheDocument()
  })

  it('阶段文本在 aria-live 区内，供读屏播报', async () => {
    stubFetch({})
    render(<App />)
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })

    await emitProgress(progress({ label: '解析代码' }))
    // role="status" 隐含 aria-live="polite"，这是标记状态区的标准做法。
    const live = await screen.findByRole('status')
    expect(live).toHaveTextContent('解析代码')
  })

  it('给出百分比时渲染 progressbar，缺失时用不确定态', async () => {
    stubFetch({})
    render(<App />)
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })

    await emitProgress(progress({ percent: 42 }))
    const bar = await screen.findByRole('progressbar')
    expect(bar).toHaveAttribute('aria-valuenow', '42')

    await emitProgress(progress({ percent: null, label: '汇总报告' }))
    await waitFor(() => expect(screen.queryByRole('progressbar')).not.toBeInTheDocument())
  })

  it('单条坏事件不中断整个流', async () => {
    stubFetch({})
    render(<App />)
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })

    await emitRawProgress('{不是合法 JSON')
    await emitProgress(progress({ label: '解析代码' }))

    expect(await screen.findByRole('status')).toHaveTextContent('解析代码')
  })

  it('失败的模块在历史里标出，不影响整体推进', async () => {
    stubFetch({})
    render(<App />)
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })

    await emitProgress(
      progress({ stage: 'analyzing_modules', label: '分析模块', detail: 'm1 分析失败（2/3）', failed: true }),
    )

    const history = await screen.findByText(/已完成的阶段/)
    expect(history).toBeInTheDocument()
    // 详情区与历史列表都会出现这段文本，限定到详情区。
    expect(
      screen.getByText('m1 分析失败（2/3）', { selector: '.progress__detail' }),
    ).toBeInTheDocument()
  })
})

describe('SSE 生命周期', () => {
  it('卸载时关闭连接', async () => {
    stubFetch({})
    const view = render(<App />)
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })

    const source = MockEventSource.latest()
    expect(source.closed).toBe(false)

    view.unmount()
    expect(source.closed).toBe(true)
  })

  it('卸载后到达的事件不触发状态更新告警', async () => {
    stubFetch({})
    const warn = vi.spyOn(console, 'error').mockImplementation(() => {})
    const view = render(<App />)
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })

    const source = MockEventSource.latest()
    view.unmount()
    // 卸载后仍推一条：closed 标志应让它被丢弃。不包 act——正是要验证它不引发更新。
    source.emit(progress({ label: '不该出现' }))

    expect(warn).not.toHaveBeenCalled()
  })

  it('连接中断时呈现断连状态与重连入口', async () => {
    stubFetch({})
    render(<App />)
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })

    await failConnection()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('进度连接已断开')
    expect(within(alert).getByRole('button', { name: '重新连接' })).toBeInTheDocument()
  })

  it('首个事件到达前断连也呈现提示', async () => {
    // 后端没起、任务标识失效都走这条路径。早返回分支若忽略 disconnected，
    // 用户会永远停在「正在启动分析…」上——正是计划要求避免的静默停滞。
    stubFetch({})
    render(<App />)
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })

    await failConnection()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('进度连接已断开')
    expect(screen.getByRole('status')).toHaveTextContent('尚未收到进度就已断开连接')
  })

  it('重连建立新连接且不留旧连接', async () => {
    stubFetch({})
    render(<App />)
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })

    const first = MockEventSource.latest()
    await failConnection(first)
    const alert = await screen.findByRole('alert')

    const user = userEvent.setup()
    await user.click(within(alert).getByRole('button', { name: '重新连接' }))

    await waitFor(() => expect(MockEventSource.instances.length).toBe(2))
    expect(first.closed).toBe(true)
    expect(MockEventSource.latest().closed).toBe(false)
  })

  it('收到终止事件后主动关闭连接', async () => {
    stubFetch({})
    render(<App />)
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })

    const source = MockEventSource.latest()
    await emitProgress(progress({ stage: 'done', label: '完成', percent: 100 }))

    await waitFor(() => expect(source.closed).toBe(true))
  })

  it('终止后的连接错误不被误判为断连', async () => {
    stubFetch({})
    render(<App />)
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })

    const source = MockEventSource.latest()
    source.emit(progress({ stage: 'done', label: '完成', percent: 100 }))
    await waitFor(() => expect(source.closed).toBe(true))
    await failConnection(source)

    expect(screen.queryByText('进度连接已断开')).not.toBeInTheDocument()
  })
})

describe('失败的分类呈现（AE6）', () => {
  const cases: Array<[string, string, string]> = [
    ['invalid_url', '地址格式不正确', '地址格式不正确'],
    ['not_found', '仓库不存在：a/b', '仓库不存在或不可访问'],
    ['no_access', '无访问权限：a/b', '没有访问权限'],
    ['too_large', '可解析文件数 23997 超出上限 1500', '规模超出本系统的处理上限'],
  ]

  it.each(cases)('%s 呈现可区分的提示', async (reason, message, expectedHint) => {
    stubFetch({
      submit: () =>
        new Response(JSON.stringify({ reason, message }), {
          status: 400,
          headers: { 'Content-Type': 'application/json' },
        }),
    })
    render(<App />)
    await submitRepo()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(message)
    expect(alert).toHaveTextContent(expectedHint)
  })

  it('四类提示互不相同', () => {
    const hints = cases.map(([, , hint]) => hint)
    expect(new Set(hints).size).toBe(hints.length)
  })

  it('超规模错误保留实际数字', async () => {
    stubFetch({
      submit: () =>
        new Response(
          JSON.stringify({ reason: 'too_large', message: '可解析文件数 23997 超出上限 1500' }),
          { status: 413, headers: { 'Content-Type': 'application/json' } },
        ),
    })
    render(<App />)
    await submitRepo()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('23997')
    expect(alert).toHaveTextContent('1500')
  })

  it('网络失败与业务错误分开呈现', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch') }))
    render(<App />)
    await submitRepo()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('无法连接后端')
  })

  it('非 JSON 的错误响应不掩盖 HTTP 状态', async () => {
    stubFetch({
      submit: () => new Response('<html>502 Bad Gateway</html>', { status: 502 }),
    })
    render(<App />)
    await submitRepo()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('502')
  })
})

describe('报告渲染', () => {
  async function completeAnalysis(overrides: Partial<AnalysisResult> = {}): Promise<void> {
    stubFetch({ result: () => new Response(JSON.stringify(result(overrides)), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }) })
    render(<App />)
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })
    await emitProgress(progress({ stage: 'done', label: '完成', percent: 100 }))
    await screen.findByRole('heading', { name: '架构报告' })
  }

  it('引用路径与行号可见', async () => {
    await completeAnalysis()
    // 可核验性的前提：路径与行号要在页面上，不能藏在 tooltip 里。
    expect(screen.getByText('pkg/core.py:10-42')).toBeInTheDocument()
  })

  it('结论文本与总体印象都渲染', async () => {
    await completeAnalysis()
    expect(screen.getByText('核心逻辑集中在 pkg/core.py')).toBeInTheDocument()
    expect(screen.getByText('总体印象文本')).toBeInTheDocument()
  })

  it('缺失部分始终呈现', async () => {
    await completeAnalysis()
    expect(screen.getByText('分析的缺失部分')).toBeInTheDocument()
    expect(screen.getByText('本次分析无缺失部分')).toBeInTheDocument()
  })

  it('无法核验的结论显式呈现', async () => {
    await completeAnalysis({
      report: { ...result().report!, unsupported_claims: ['[deps] 某结论 —— 引用路径不存在'] },
    })
    expect(screen.getByText(/1 条结论因引用无法核验被丢弃/)).toBeInTheDocument()
  })

  it('未完成的模块被标注（AE4）', async () => {
    await completeAnalysis({ module_failures: ['auth：RuntimeError: 超时'] })
    expect(screen.getByText(/1 个模块未完成分析/)).toBeInTheDocument()
  })

  it('完成后焦点移到报告标题', async () => {
    await completeAnalysis()
    expect(screen.getByRole('heading', { name: '架构报告' })).toHaveFocus()
  })

  it('后端标记失败时呈现失败而非报告', async () => {
    stubFetch({
      result: () =>
        new Response(
          JSON.stringify(result({ failed: true, error: '仓库不存在：a/b', report: null })),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
    })
    render(<App />)
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })
    await emitProgress(progress({ stage: 'failed', label: '失败', failed: true }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('仓库不存在：a/b')
    expect(screen.queryByRole('heading', { name: '架构报告' })).not.toBeInTheDocument()
  })
})

describe('评审呈现', () => {
  async function complete(): Promise<void> {
    stubFetch({})
    render(<App />)
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })
    await emitProgress(progress({ stage: 'done', label: '完成', percent: 100 }))
    await screen.findByRole('heading', { name: '代码评审' })
  }

  it('发现带严重度与位置', async () => {
    await complete()
    expect(screen.getByText('3 个文件构成循环依赖')).toBeInTheDocument()
    expect(screen.getByText('高')).toBeInTheDocument()
  })

  it('零命中与未执行可区分（R17）', async () => {
    await complete()
    // structural 已执行且命中 1 条；security 未执行并给出原因。
    expect(screen.getByText('已执行，命中 1 条')).toBeInTheDocument()
    expect(screen.getByText('未执行')).toBeInTheDocument()
    expect(screen.getByText('未配置 LLM provider')).toBeInTheDocument()
  })

  it('判断依据与结论分开显示', async () => {
    await complete()
    expect(screen.getByText('依赖图上的有向环')).toBeInTheDocument()
  })
})

describe('问答', () => {
  async function complete(): Promise<void> {
    render(<App />)
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })
    await emitProgress(progress({ stage: 'done', label: '完成', percent: 100 }))
    await screen.findByRole('heading', { name: '代码问答' })
  }

  it('未找到时呈现说明，不呈现空回答框', async () => {
    stubFetch({
      question: () =>
        new Response(
          JSON.stringify({
            question: 'q',
            found: false,
            answer: '未在该仓库中找到与问题相关的代码。',
            citations: [],
            note: 'below_threshold',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
    })
    await complete()

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('关于这个仓库的问题'), '怎么部署到 K8s')
    await user.click(screen.getByRole('button', { name: '提问' }))

    expect(await screen.findByText(/未在该仓库中找到与问题相关的代码/)).toBeInTheDocument()
    // 未找到时不该出现引用区。
    expect(screen.queryByRole('heading', { name: '引用' })).not.toBeInTheDocument()
  })

  it('找到时呈现回答与引用', async () => {
    stubFetch({
      question: () =>
        new Response(
          JSON.stringify({
            question: 'q',
            found: true,
            answer: '校验逻辑在 pkg/auth.py:10-18。',
            citations: [
              { path: 'pkg/auth.py', start_line: 10, end_line: 18, symbol: 'verify_token', distance: 0.21 },
            ],
            note: '',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
    })
    await complete()

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('关于这个仓库的问题'), '怎么校验 token')
    await user.click(screen.getByRole('button', { name: '提问' }))

    expect(await screen.findByRole('heading', { name: '引用' })).toBeInTheDocument()
    expect(screen.getByText('pkg/auth.py:10-18')).toBeInTheDocument()
    expect(screen.getByText('verify_token')).toBeInTheDocument()
  })

  it('空问题被阻止', async () => {
    const { calls } = stubFetch({})
    await complete()
    const before = calls.length

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '提问' }))

    expect(await screen.findByText('请输入问题')).toBeInTheDocument()
    expect(calls.length).toBe(before)
  })

  it('分析未完成的错误给出可读提示', async () => {
    stubFetch({
      question: () =>
        new Response(
          JSON.stringify({ reason: 'analysis_incomplete', message: '分析尚未完成' }),
          { status: 409, headers: { 'Content-Type': 'application/json' } },
        ),
    })
    await complete()

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('关于这个仓库的问题'), 'q')
    await user.click(screen.getByRole('button', { name: '提问' }))

    expect(await screen.findByText(/分析尚未完成，暂时不能提问/)).toBeInTheDocument()
  })
})
