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
import { MemoryRouter } from 'react-router-dom'
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
    queue_position: 0,
    commit_sha: 'abcdef123456789',
    modules: [],
    dependency_graph: null,
    language_profile: null,
    module_failures: [],
    ...overrides,
  }
}

/** 装一个 fetch 替身。返回被调用的请求列表，供断言「没发第二次请求」。 */
function stubFetch(handlers: {
  submit?: () => Promise<Response> | Response
  result?: () => Promise<Response> | Response
  question?: () => Promise<Response> | Response
  list?: () => Promise<Response> | Response
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
          json({
            task_id: 't1',
            repo: 'acme/widget',
            stage: 'queued',
            message: 'ok',
            queue_position: 0,
          })
        )
      }
      // 历史列表端点：路径恰好是 /api/analyses（没有 task 段）。首页挂载时会拉它。
      if (/\/api\/analyses$/.test(url)) {
        return handlers.list?.() ?? json([])
      }
      return handlers.result?.() ?? json(result())
    }),
  )
  return { calls }
}

/**
 * 渲染工作台。
 *
 * **U1 之后必须包一层 Router。** 生产入口的 `BrowserRouter` 在 `main.tsx`，测试用
 * `MemoryRouter` 换掉它——套在 `App` 内部就换不掉，会出现嵌套 Router 的运行时错误。
 *
 * `initialEntries` 让「直达某页 URL」成为一次普通渲染，不必先走一遍点击导航。
 */
function renderApp(initialPath = '/'): ReturnType<typeof render> {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <App />
    </MemoryRouter>,
  )
}

/**
 * 切到某个导航页。
 *
 * 这是 U1 测试改写的统一手法：在原断言之前插入一次导航，断言体本身不动。多页形态下任一
 * 时刻只有一页在 DOM，而原测试是在「三个区块共存」的前提下写的。
 */
async function gotoNav(label: string): Promise<void> {
  const user = userEvent.setup()
  await user.click(screen.getByRole('link', { name: label }))
}

/**
 * 预置访客凭证。
 *
 * U5 之后提交分析需要凭证（R-68），而 U17 的前端拦截会在未配置时直接阻止提交并引导到设置页
 * ——那会让本文件里每个提交用例都停在那道拦截上。真实用户也是先配一次凭证再用，所以这里
 * 在每个用例前把它配好。「未配置时被拦截」由 Settings 的测试专门断言。
 */
function seedCredentials(): void {
  window.localStorage.setItem(
    'codepilot.credentials.v1',
    JSON.stringify({
      base_url: 'https://guest.example',
      api_key: 'guest-key',
      model_flash: '',
      model_pro: '',
    }),
  )
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

// 每个用例前配好凭证。见 seedCredentials 的说明。
beforeEach(() => {
  window.localStorage.clear()
  seedCredentials()
})

describe('提交', () => {
  beforeEach(() => {
    stubFetch({})
  })

  it('空地址被表单阻止并提示，不发请求', async () => {
    const { calls } = stubFetch({})
    renderApp()
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '开始分析' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('请输入 GitHub 仓库地址')
    // 收窄到 POST：U9 之后首页挂载会拉一次历史列表（GET），而原断言要钉的是「没发提交
    // 请求」。断言全部请求为 0 会把一个无关的只读请求算作违反。
    expect(calls.filter((call) => call.startsWith('POST'))).toHaveLength(0)
  })

  it('只有空白的地址同样被阻止', async () => {
    const { calls } = stubFetch({})
    renderApp()
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('GitHub 仓库地址'), '   ')
    await user.click(screen.getByRole('button', { name: '开始分析' }))

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(calls.filter((call) => call.startsWith('POST'))).toHaveLength(0)
  })

  it('提交后进入分析中，出现进度区', async () => {
    renderApp()
    await submitRepo()
    expect(await screen.findByRole('heading', { name: '分析进度' })).toBeInTheDocument()
  })

  it('输入错误后重新输入会清掉提示', async () => {
    renderApp()
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
    renderApp()
    await submitRepo()

    const button = await screen.findByRole('button', { name: '分析中…' })
    expect(button).toBeDisabled()
    expect(screen.getByText(/分析进行中/)).toBeInTheDocument()
  })

  it('分析中再次提交不发起第二次请求', async () => {
    const { calls } = stubFetch({})
    renderApp()
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
    renderApp()
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
    renderApp()
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })

    await emitProgress(progress({ label: '解析代码' }))
    // role="status" 隐含 aria-live="polite"，这是标记状态区的标准做法。
    const live = await screen.findByRole('status')
    expect(live).toHaveTextContent('解析代码')
  })

  it('给出百分比时渲染 progressbar，缺失时用不确定态', async () => {
    stubFetch({})
    renderApp()
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
    renderApp()
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })

    await emitRawProgress('{不是合法 JSON')
    await emitProgress(progress({ label: '解析代码' }))

    expect(await screen.findByRole('status')).toHaveTextContent('解析代码')
  })

  it('失败的模块在历史里标出，不影响整体推进', async () => {
    stubFetch({})
    renderApp()
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
    const view = renderApp()
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
    const view = renderApp()
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
    renderApp()
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
    renderApp()
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })

    await failConnection()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('进度连接已断开')
    expect(screen.getByRole('status')).toHaveTextContent('尚未收到进度就已断开连接')
  })

  it('重连建立新连接且不留旧连接', async () => {
    stubFetch({})
    renderApp()
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
    renderApp()
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })

    const source = MockEventSource.latest()
    await emitProgress(progress({ stage: 'done', label: '完成', percent: 100 }))

    await waitFor(() => expect(source.closed).toBe(true))
  })

  it('终止后的连接错误不被误判为断连', async () => {
    stubFetch({})
    renderApp()
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
    renderApp()
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
    renderApp()
    await submitRepo()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('23997')
    expect(alert).toHaveTextContent('1500')
  })

  it('网络失败与业务错误分开呈现', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch') }))
    renderApp()
    await submitRepo()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('无法连接后端')
  })

  it('非 JSON 的错误响应不掩盖 HTTP 状态', async () => {
    stubFetch({
      submit: () => new Response('<html>502 Bad Gateway</html>', { status: 502 }),
    })
    renderApp()
    await submitRepo()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('502')
  })
})

describe('报告渲染', () => {
  /**
   * 跑到完成态再切到 Reports 页。
   *
   * **U1 的改写手法：原断言前插入一次导航，断言体不动。** 多页形态下报告在 Reports 页而非
   * 与进度同屏——「架构报告」标题的等待因此从「完成后自动出现」变成「切页后出现」。
   */
  async function completeAnalysis(overrides: Partial<AnalysisResult> = {}): Promise<void> {
    stubFetch({ result: () => new Response(JSON.stringify(result(overrides)), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }) })
    renderApp()
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })
    await emitProgress(progress({ stage: 'done', label: '完成', percent: 100 }))
    await gotoNav('Reports')
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

  it('切页后焦点移到新页标题', async () => {
    await completeAnalysis()
    // **语义随多页形态改变。** U1 之前焦点落在「架构报告」——那时它是完成态下新出现的
    // 区块。多页形态下 R-65 要求焦点落在**新页**主标题，因为切页换掉的是一整片 DOM，
    // 键盘与读屏用户需要知道内容变了。报告标题此刻是页内的二级标题，不是焦点目标。
    expect(screen.getByRole('heading', { name: 'Reports', level: 1 })).toHaveFocus()
  })

  it('后端标记失败时呈现失败而非报告', async () => {
    stubFetch({
      result: () =>
        new Response(
          JSON.stringify(result({ failed: true, error: '仓库不存在：a/b', report: null })),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
    })
    renderApp()
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })
    await emitProgress(progress({ stage: 'failed', label: '失败', failed: true }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('仓库不存在：a/b')
    expect(screen.queryByRole('heading', { name: '架构报告' })).not.toBeInTheDocument()
  })
})

describe('评审呈现', () => {
  /**
   * 跑到完成态再切到某个评审页。
   *
   * **一处改写在这里分成了三页。** U1 之前三类检查同屏呈现，所以「零命中与未执行可区分」
   * 一条用例就能同时断言 structural 与 security。多页形态下每页只显示一个类别（R-30），
   * 那条断言因此拆成两条——每条切到对应的页再断言原来那半个断言体。断言体不变，覆盖不减。
   */
  async function completeAndGoto(nav: string): Promise<void> {
    stubFetch({})
    renderApp()
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })
    await emitProgress(progress({ stage: 'done', label: '完成', percent: 100 }))
    await gotoNav(nav)
  }

  it('发现带严重度与位置', async () => {
    await completeAndGoto('Structural')
    expect(screen.getByText('3 个文件构成循环依赖')).toBeInTheDocument()
    expect(screen.getByText('高')).toBeInTheDocument()
  })

  it('已执行零命中的类别给出覆盖范围（R17）', async () => {
    await completeAndGoto('Structural')
    // structural 已执行且命中 1 条——命中数出现在计数行里。
    expect(screen.getByText(/共 1 条发现/)).toBeInTheDocument()
  })

  it('未执行的类别显式说明且与零命中可区分（R17）', async () => {
    await completeAndGoto('Security Review')
    expect(screen.getByText('未执行')).toBeInTheDocument()
    expect(screen.getByText('未配置 LLM provider')).toBeInTheDocument()
    // 关键区分：未执行不得呈现为「零发现」。
    expect(screen.getByText(/未执行不等于零发现/)).toBeInTheDocument()
  })

  it('评审执行情况表同时给出三类状态', async () => {
    // 三类并列的呈现迁到 Reports 页的执行情况表——原「三类同屏」的覆盖由它承担。
    await completeAndGoto('Reports')
    expect(screen.getByText('已执行')).toBeInTheDocument()
    expect(screen.getByText('未执行')).toBeInTheDocument()
    expect(screen.getByText('未配置 LLM provider')).toBeInTheDocument()
  })

  it('判断依据与结论分开显示', async () => {
    await completeAndGoto('Structural')
    expect(screen.getByText('依赖图上的有向环')).toBeInTheDocument()
  })
})

describe('问答', () => {
  /** 同上：完成态后切到 AI Chat 页，原断言体不动。 */
  async function complete(): Promise<void> {
    renderApp()
    await submitRepo()
    await screen.findByRole('heading', { name: '分析进度' })
    await emitProgress(progress({ stage: 'done', label: '完成', percent: 100 }))
    await gotoNav('AI Chat')
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
