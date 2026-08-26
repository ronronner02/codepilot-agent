/**
 * 页面测试的共用固件。
 *
 * 抽出来的理由是十个页面测试都要同一套东西：一个完整的分析结果、一个 fetch 替身、一个带
 * Router 的渲染函数。各文件抄一份的话，加一个响应字段要改十处，而漏掉的那处会以「类型
 * 错误」而非「测试失败」的形式出现——那反而是好的，但改十处本身就是浪费。
 */

import { render } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { vi } from 'vitest'
import { App } from '../App'
import type {
  AnalysisResult,
  AnalysisSummary,
  FileContent,
  FileTree,
  SearchResult,
} from '../api/types'

export const CREDENTIALS_KEY = 'codepilot.credentials.v1'

/** 预置访客凭证。提交分析需要它（R-68）。 */
export function seedCredentials(): void {
  window.localStorage.setItem(
    CREDENTIALS_KEY,
    JSON.stringify({
      base_url: 'https://guest.example',
      api_key: 'guest-key-1234',
      model_flash: '',
      model_pro: '',
    }),
  )
}

/**
 * 一次内容丰富的分析结果。
 *
 * 数字对齐计划的 AE-02：33 文件 / 9 模块 / 6 发现 / 99 切块。这样概览条的逐项断言能直接
 * 比对，而不必在测试里重算。
 */
export function fullResult(overrides: Partial<AnalysisResult> = {}): AnalysisResult {
  return {
    task_id: 't1',
    repo: 'acme/widget',
    stage: 'done',
    completed: true,
    failed: false,
    error: '',
    queue_position: 0,
    commit_sha: 'abcdef1234567890',
    report: {
      repo: 'acme/widget',
      commit_sha: 'abcdef1234567890',
      summary: '总体印象文本',
      sections: [
        {
          key: 'module_breakdown',
          title: '模块划分',
          claims: [
            {
              text: '认证逻辑集中在 auth/jwt.py',
              citations: [{ path: 'auth/jwt.py', line: 45, end_line: 78 }],
            },
          ],
        },
      ],
      missing: { text: '本次分析无缺失部分', unparsed_files: 0, skipped_modules: 0 },
      validation_summary: '共 26 条结论：25 条引用完全有效。',
      unsupported_claims: [],
    },
    review: {
      target_files: ['auth/jwt.py', 'core/app.py'],
      outcomes: [
        {
          category: 'structural',
          status: 'executed',
          scope: '2 个文件；检查项：循环依赖',
          hit_count: 2,
          reason: '',
        },
        {
          category: 'security',
          status: 'skipped',
          scope: '2 个文件；模式集：硬编码密钥',
          hit_count: 0,
          reason: '未配置 LLM provider',
        },
        {
          category: 'error_handling',
          status: 'executed',
          scope: '2 个文件；检查项：裸 except',
          hit_count: 0,
          reason: '',
        },
      ],
      findings: [
        {
          category: 'structural',
          kind: 'circular_dependency',
          path: 'auth/jwt.py',
          line: 24,
          severity: 'high',
          message: '3 个文件构成循环依赖',
          evidence: '依赖图上的有向环',
        },
        {
          category: 'structural',
          kind: 'god_module',
          path: 'core/app.py',
          line: 0,
          severity: 'medium',
          message: '模块承担过多职责',
          evidence: '出度 18',
        },
      ],
    },
    index: { cache_hit: false, chunk_count: 99, identity: 'api:m:v1', note: '新建索引' },
    modules: [
      {
        name: 'auth',
        files: ['auth/jwt.py', 'auth/session.py'],
        internal_edges: 1,
        external_edges: 2,
        origin: 'directory',
        summary: '负责令牌签发与校验，会话状态存在 Redis。',
        limitation: '',
      },
      {
        name: 'core',
        files: ['core/app.py'],
        internal_edges: 0,
        external_edges: 3,
        origin: 'split',
        summary: '',
        limitation: '子 Agent 超时，分析未完成',
      },
    ],
    dependency_graph: {
      nodes: ['auth/jwt.py', 'auth/session.py', 'core/app.py'],
      edges: [{ source: 'core/app.py', target: 'auth/jwt.py' }],
      external: { requests: ['core/app.py'] },
      unresolved: [
        { target: '.legacy', path: 'core/app.py', line: 9, reason: '未找到对应文件' },
      ],
      cycles: [],
      granularity: 'file',
      degraded_reason: '',
    },
    language_profile: {
      total_files: 33,
      parseable_files: 30,
      by_language: {
        Python: 20,
        TypeScript: 6,
        Markdown: 3,
        YAML: 2,
        CSS: 1,
        Shell: 1,
      },
    },
    module_failures: [],
    ...overrides,
  }
}

export function summary(overrides: Partial<AnalysisSummary> = {}): AnalysisSummary {
  return {
    task_id: 't1',
    repo: 'acme/widget',
    stage: 'done',
    completed: true,
    failed: false,
    commit_sha: 'abcdef1234567890',
    created_at: 1_756_000_000,
    file_count: 33,
    finding_count: 2,
    ...overrides,
  }
}

/**
 * 一个文件的内容。
 *
 * 行数覆盖到 80：报告固件的引用指向 45-78 行，而只有 3 行的文件让「跳转到第 45 行并高亮」
 * 这条断言无从成立。真实文件也不会是这样——固件要能承载它服务的那些断言。
 */
export function fileContent(overrides: Partial<FileContent> = {}): FileContent {
  const lines = ['import jwt', '', 'def verify(token):']
  for (let n = 4; n <= 80; n += 1) {
    lines.push(n === 45 ? '    payload = jwt.decode(token)' : `    # line ${n}`)
  }
  return {
    path: 'auth/jwt.py',
    start_line: 1,
    end_line: 80,
    total_lines: 80,
    content: lines.join('\n'),
    truncated: false,
    truncated_note: '',
    ...overrides,
  }
}

export function fileTree(overrides: Partial<FileTree> = {}): FileTree {
  return {
    root: '.',
    entries: [
      { path: 'auth', is_dir: true, file_count: 2 },
      { path: 'README.md', is_dir: false, file_count: 0 },
    ],
    truncated_note: '',
    ...overrides,
  }
}

export function searchResult(overrides: Partial<SearchResult> = {}): SearchResult {
  return {
    query: 'token 校验',
    found: true,
    hits: [
      {
        path: 'auth/jwt.py',
        start_line: 45,
        end_line: 78,
        symbol: 'verify_token',
        content: 'def verify_token(raw):\n    return jwt.decode(raw)',
        distance: 0.21,
      },
    ],
    reason: '',
    note: '',
    ...overrides,
  }
}

/** 一个错误响应。reason 是稳定契约，界面按它分类呈现。 */
export function errorResponse(reason: string, message: string, status: number): Response {
  return new Response(JSON.stringify({ reason, message }), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

export interface StubHandlers {
  result?: () => Promise<Response> | Response
  list?: () => Promise<Response> | Response
  file?: () => Promise<Response> | Response
  tree?: () => Promise<Response> | Response
  search?: () => Promise<Response> | Response
  question?: () => Promise<Response> | Response
  submit?: () => Promise<Response> | Response
}

/** 按路径分派的 fetch 替身。返回调用记录，供断言「没发某类请求」。 */
export function stubApi(handlers: StubHandlers = {}): { calls: string[] } {
  const calls: string[] = []

  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString()
      const method = init?.method ?? 'GET'
      calls.push(`${method} ${url}`)

      if (method === 'POST' && url.endsWith('/questions')) {
        return handlers.question?.() ?? json({ question: 'q', found: false, answer: '', citations: [], note: '' })
      }
      if (method === 'POST' && url.includes('/search')) {
        return handlers.search?.() ?? json(searchResult())
      }
      if (method === 'POST') {
        return (
          handlers.submit?.() ??
          json({ task_id: 't1', repo: 'acme/widget', stage: 'queued', message: 'ok', queue_position: 0 })
        )
      }
      if (url.includes('/file?')) {
        return handlers.file?.() ?? json(fileContent())
      }
      if (url.includes('/tree?')) {
        if (handlers.tree) return handlers.tree()
        // 按请求的目录返回不同内容：根目录给 auth/ 与 README.md，auth/ 给两个文件。
        // 所有目录都返回同一份固件会让 auth 包含自己，展开即无限递归。
        const target = new URL(url, 'http://x').searchParams.get('path') ?? '.'
        if (target === '.') return json(fileTree())
        return json({
          root: target,
          entries: [
            { path: `${target}/jwt.py`, is_dir: false, file_count: 0 },
            { path: `${target}/session.py`, is_dir: false, file_count: 0 },
          ],
          truncated_note: '',
        })
      }
      if (/\/api\/analyses$/.test(url)) {
        return handlers.list?.() ?? json([summary()])
      }
      return handlers.result?.() ?? json(fullResult())
    }),
  )

  return { calls }
}

/** 在某个地址渲染工作台。直达 URL 的测试用它，不必先走一遍点击导航。 */
export function renderAt(path: string): ReturnType<typeof render> {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  )
}
