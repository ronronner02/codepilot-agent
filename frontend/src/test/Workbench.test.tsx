/**
 * 工作台外壳与结果统计（PRD 2026-08-25-002 的 AE-01 至 AE-09）。
 *
 * 与 App.test.tsx 分开：那份测的是状态机与 SSE 生命周期，这份测的是外壳、导航与统计口径。
 * 两者共用同一批固件，但关注点不同，混在一个文件里会让失败信号指向不明。
 *
 * 这里有两条断言是**防回归**而非验功能：
 *
 * - **切页后其它页必须已卸载**（U1 改写后的形态）。原本钉的是「三个区块标题共存」——那是
 *   锚点滚动形态下的防回归，防的是「有人把锚点改成视图切换」。多页形态推翻了那个前提，
 *   这条断言的职责因此反转为防止有人把多页改回锚点滚动。同一个防回归意图，相反的形态。
 * - role="status" / role="alert" 的数量不得增加（统计区不得挂播报角色）。
 */

import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { App } from '../App'
import type { AnalysisResult, ProgressEvent } from '../api/types'
import { MockEventSource } from './setup'

const REPO = 'https://github.com/acme/widget'

function progress(overrides: Partial<ProgressEvent> = {}): ProgressEvent {
  return { stage: 'parsing', label: '解析代码', detail: '', percent: 20, failed: false, ...overrides }
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
              citations: [
                { path: 'pkg/core.py', line: 10, end_line: 42 },
                { path: 'pkg/util.py', line: 3, end_line: null },
              ],
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
        { category: 'structural', status: 'executed', scope: '1 个文件', hit_count: 3, reason: '' },
        {
          category: 'security',
          status: 'skipped',
          scope: '1 个文件',
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
        {
          category: 'error_handling',
          kind: 'bare_except',
          path: 'pkg/b.py',
          line: 12,
          severity: 'medium',
          message: '裸 except 吞掉异常',
          evidence: 'except 子句无类型',
        },
        {
          category: 'structural',
          kind: 'long_module',
          path: 'pkg/c.py',
          line: 1,
          severity: 'low',
          message: '模块过长',
          evidence: '行数超阈值',
        },
      ],
    },
    index: { cache_hit: false, chunk_count: 318, identity: 'api:m:v1', note: '新建索引' },
    queue_position: 0,
    commit_sha: 'abcdef123456789',
    modules: [],
    dependency_graph: null,
    language_profile: null,
    module_failures: [],
    ...overrides,
  }
}

function stubFetch(overrides: Partial<AnalysisResult> = {}): { calls: string[] } {
  const calls: string[] = []
  const json = (body: unknown): Response =>
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })

  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString()
      const method = init?.method ?? 'GET'
      calls.push(`${method} ${url}`)
      if (method === 'POST') {
        return json({
          task_id: 't1',
          repo: 'acme/widget',
          stage: 'queued',
          message: 'ok',
          queue_position: 0,
        })
      }
      // 历史列表：首页挂载时会拉一次。
      if (/\/api\/analyses$/.test(url)) {
        return json([])
      }
      return json(result(overrides))
    }),
  )
  return { calls }
}

/** U1 之后必须包 Router。生产入口的 BrowserRouter 在 main.tsx。 */
function renderApp(initialPath = '/'): ReturnType<typeof render> {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <App />
    </MemoryRouter>,
  )
}

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

beforeEach(() => {
  window.localStorage.clear()
  seedCredentials()
})

/**
 * 跑到完成态。统计条在 Overview 页，提交后即停在那里，不必再切页。
 *
 * 等待点从「架构报告」改成统计区：报告迁到 Reports 页了，而这个 helper 的用途是给统计
 * 断言准备状态。
 */
async function completeAnalysis(overrides: Partial<AnalysisResult> = {}): Promise<string[]> {
  const { calls } = stubFetch(overrides)
  renderApp()
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('GitHub 仓库地址'), REPO)
  await user.click(screen.getByRole('button', { name: '开始分析' }))
  await screen.findByRole('heading', { name: '分析进度' })
  await act(async () => {
    MockEventSource.latest().emit(progress({ stage: 'done', label: '完成', percent: 100 }))
  })
  await screen.findByLabelText('分析结果统计')
  return calls
}

describe('工作台外壳', () => {
  it('未提交时顶部条走空态（AE-08）', () => {
    stubFetch()
    renderApp()
    expect(screen.getByText('未选择仓库')).toBeInTheDocument()
    expect(screen.getByText('未开始')).toBeInTheDocument()
    expect(screen.getByText('尚无任务')).toBeInTheDocument()
  })

  it('阶段文案只出现一处，侧栏底部改放任务标识', async () => {
    await completeAnalysis()
    // 顶部条吸顶常驻，侧栏再显示同一个状态是纯冗余。
    expect(screen.getAllByText('已完成')).toHaveLength(1)
    expect(screen.getByText('任务 t1')).toBeInTheDocument()
  })

  it('分析完成后顶部条显示仓库与 commit 前 12 位（AE-09）', async () => {
    await completeAnalysis()
    // 顶部条的仓库标识与报告内的元信息各出现一次。
    expect(screen.getAllByText('acme/widget').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('abcdef123456')).toBeInTheDocument()
    expect(screen.getByText('已完成')).toBeInTheDocument()
  })

  it('结果未就绪时依赖结果的导航项置灰并标注原因（R-03）', () => {
    stubFetch()
    renderApp()
    // Overview / MCP / Settings 是例外，未就绪时仍可点（R-03）。
    expect(screen.getByRole('link', { name: 'Overview' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'MCP' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Settings' })).toBeInTheDocument()

    for (const label of ['Architecture', 'AI Chat', 'Code Search', 'Reports']) {
      const item = screen.getByRole('button', { name: new RegExp(label) })
      expect(item).toBeDisabled()
      // AE-01 的「标注原因」由 title/aria-label 满足，不做可见文案（owner 决定）。
      expect(item).toHaveAttribute('title', '需先完成一次分析')
      expect(item.textContent).toBe(label)
    }
  })

  it('切页后当前项高亮，且其它页已从 DOM 卸载（AE-01）', async () => {
    await completeAnalysis()
    const user = userEvent.setup()

    // 切到 Reports：报告出现。
    await user.click(screen.getByRole('link', { name: 'Reports' }))
    expect(await screen.findByRole('heading', { name: '架构报告' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Reports' })).toHaveAttribute(
      'aria-current',
      'page',
    )

    // 防回归（语义反转）：多页形态下 Overview 的内容必须已卸载。原断言钉「三个标题共存」，
    // 那是锚点滚动的前提；这条钉「切页即卸载」，防的是有人把多页改回锚点滚动。
    expect(screen.queryByLabelText('分析结果统计')).not.toBeInTheDocument()

    // 再切到 AI Chat：报告随之卸载。
    await user.click(screen.getByRole('link', { name: 'AI Chat' }))
    expect(await screen.findByRole('heading', { name: '代码问答' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '架构报告' })).not.toBeInTheDocument()
  })
})

describe('结果统计', () => {
  it('四项统计口径正确且不发额外请求（AE-02）', async () => {
    const calls = await completeAnalysis()
    const stats = screen.getByLabelText('分析结果统计')

    // 发现 3 条、引用 2 条、切块 318、未完成模块 0。
    expect(stats).toHaveTextContent('3评审发现')
    expect(stats).toHaveTextContent('2可核验引用')
    expect(stats).toHaveTextContent('318索引切块')
    expect(stats).toHaveTextContent('0未完成模块')

    // 统计全部由已有结果派生：取结果只发一次，统计不额外拉数据。
    // 收窄到结果端点：首页挂载会拉一次历史列表（GET /api/analyses），那与统计无关。
    const resultCalls = calls.filter((call) => /GET .*\/api\/analyses\/[^/]+$/.test(call))
    expect(resultCalls.length).toBe(1)
  })

  it('严重度分布按高中低分段并带计数（AE-02）', async () => {
    await completeAnalysis()
    const stats = screen.getByLabelText('分析结果统计')
    expect(stats).toHaveTextContent('高危 1')
    expect(stats).toHaveTextContent('中危 1')
    expect(stats).toHaveTextContent('低危 1')
  })

  it('图例文案不与评审徽标文本相同', async () => {
    await completeAnalysis()
    // 图例在 Overview 的统计条上，用「高危 N」。
    expect(screen.getByLabelText('分析结果统计')).toHaveTextContent('高危 1')

    // 徽标「高」在评审页的发现列表里——多页形态下它与统计条不同屏，所以要切页才能断言。
    // 这条断言原本的意图是「两处文案不同」，而不同屏反而让它更强：两个文案不可能互相干扰。
    const user = userEvent.setup()
    await user.click(screen.getByRole('link', { name: 'Structural' }))
    expect(await screen.findByText('高')).toBeInTheDocument()
  })

  it('零发现时统计显示 0 且不渲染空分布条（AE-03）', async () => {
    await completeAnalysis({
      review: { target_files: [], outcomes: [], findings: [] },
    })
    const stats = screen.getByLabelText('分析结果统计')
    expect(stats).toHaveTextContent('0评审发现')
    expect(stats).toHaveTextContent('0 条发现')
    expect(stats.querySelector('.sev__track')).toBeNull()
  })

  it('无索引与零切块可区分', async () => {
    await completeAnalysis({ index: null })
    expect(screen.getByLabelText('分析结果统计')).toHaveTextContent('—索引切块')
  })

  it('未知严重度落进「其它」而非被丢弃', async () => {
    await completeAnalysis({
      review: {
        target_files: [],
        outcomes: [],
        findings: [
          {
            category: 'structural',
            kind: 'x',
            path: 'p.py',
            line: 1,
            severity: 'critical',
            message: 'm',
            evidence: 'e',
          },
        ],
      },
    })
    expect(screen.getByLabelText('分析结果统计')).toHaveTextContent('其它 1')
  })

  it('统计区不呈现执行过程信息（AE-07）', async () => {
    await completeAnalysis()
    const stats = screen.getByLabelText('分析结果统计')
    for (const word of ['耗时', 'token', '扇出', '工具调用']) {
      expect(stats.textContent ?? '').not.toContain(word)
    }
  })

  it('统计区不新增播报角色（AE-05）', async () => {
    await completeAnalysis()
    const stats = screen.getByLabelText('分析结果统计')
    expect(stats.querySelector('[role="status"]')).toBeNull()
    expect(stats.querySelector('[role="alert"]')).toBeNull()
    expect(stats.querySelector('[aria-live]')).toBeNull()
  })
})
