/**
 * 三个评审页（U14，R-30~R-33、NA-09）。
 *
 * 核心是三态可区分：未执行 / 执行失败 / 已执行零命中。三者的文案必须不同且都非空——把它们
 * 混成一句「无发现」是最常见的退化，而三者对读者的含义正相反。
 */

import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import type { CategoryOutcome, Finding } from '../api/types'
import { fullResult, json, renderAt, seedCredentials, stubApi } from './fixtures'

beforeEach(() => {
  window.localStorage.clear()
  seedCredentials()
})

function withReview(outcomes: CategoryOutcome[], findings: Finding[]): void {
  stubApi({
    result: () =>
      json(fullResult({ review: { target_files: ['a.py'], outcomes, findings } })),
  })
}

const EXECUTED_CLEAN: CategoryOutcome = {
  category: 'structural',
  status: 'executed',
  scope: '12 个文件；检查项：循环依赖、上帝模块',
  hit_count: 0,
  reason: '',
}

const SKIPPED: CategoryOutcome = {
  category: 'security',
  status: 'skipped',
  scope: '12 个文件；模式集：硬编码密钥',
  hit_count: 0,
  reason: '未配置 LLM provider',
}

const FAILED: CategoryOutcome = {
  category: 'error_handling',
  status: 'failed',
  scope: '12 个文件',
  hit_count: 0,
  reason: 'LLM 返回无法解析的 JSON',
}

describe('三态可区分（AE-09、NA-09）', () => {
  it('已执行零命中显示「已执行，零命中」并给出覆盖范围（R-32）', async () => {
    withReview([EXECUTED_CLEAN], [])
    renderAt('/a/t1/structural')

    expect(await screen.findByText('已执行，零命中')).toBeInTheDocument()
    expect(screen.getByText(/12 个文件；检查项：循环依赖/)).toBeInTheDocument()
    expect(screen.getByText(/范围之外的文件未被检查/)).toBeInTheDocument()
  })

  it('未执行显示原因，且明说不等于零发现（R-31）', async () => {
    withReview([SKIPPED], [])
    renderAt('/a/t1/security')

    expect(await screen.findByText('未执行')).toBeInTheDocument()
    expect(screen.getByText('未配置 LLM provider')).toBeInTheDocument()
    expect(screen.getByText(/未执行不等于零发现/)).toBeInTheDocument()
    // 三态的文案互不相同。
    expect(screen.queryByText('已执行，零命中')).not.toBeInTheDocument()
  })

  it('执行失败与未执行可区分', async () => {
    withReview([FAILED], [])
    renderAt('/a/t1/error-handling')

    expect(await screen.findByText('执行失败')).toBeInTheDocument()
    expect(screen.getByText('LLM 返回无法解析的 JSON')).toBeInTheDocument()
    expect(screen.getByText(/部分文件可能已被检查过/)).toBeInTheDocument()
    expect(screen.queryByText('未执行')).not.toBeInTheDocument()
  })

  it('三种状态的文案都非空', async () => {
    for (const [outcome, route] of [
      [EXECUTED_CLEAN, '/a/t1/structural'],
      [SKIPPED, '/a/t1/security'],
      [FAILED, '/a/t1/error-handling'],
    ] as const) {
      withReview([outcome], [])
      const view = renderAt(route)
      const state = await screen.findByRole('status')
      expect((state.textContent ?? '').trim().length).toBeGreaterThan(10)
      view.unmount()
    }
  })
})

describe('有发现时的呈现（R-30）', () => {
  it('按严重度分组并显示各组计数', async () => {
    withReview(
      [{ ...EXECUTED_CLEAN, hit_count: 3 }],
      [
        { category: 'structural', kind: 'a', path: 'a.py', line: 1, severity: 'high', message: 'm1', evidence: 'e1' },
        { category: 'structural', kind: 'b', path: 'a.py', line: 2, severity: 'high', message: 'm2', evidence: 'e2' },
        { category: 'structural', kind: 'c', path: 'a.py', line: 3, severity: 'low', message: 'm3', evidence: 'e3' },
      ],
    )
    renderAt('/a/t1/structural')

    expect(await screen.findByText(/共 3 条发现/)).toBeInTheDocument()
    expect(screen.getByText(/高 2 条/)).toBeInTheDocument()
    expect(screen.getByText(/低 1 条/)).toBeInTheDocument()
  })

  it('每页只显示该类别的发现', async () => {
    withReview(
      [EXECUTED_CLEAN, { ...SKIPPED, status: 'executed', hit_count: 1 }],
      [
        { category: 'structural', kind: 'a', path: 'a.py', line: 1, severity: 'high', message: '结构问题', evidence: 'e' },
        { category: 'security', kind: 'b', path: 'a.py', line: 2, severity: 'high', message: '安全问题', evidence: 'e' },
      ],
    )
    renderAt('/a/t1/structural')

    expect(await screen.findByText('结构问题')).toBeInTheDocument()
    expect(screen.queryByText('安全问题')).not.toBeInTheDocument()
  })

  it('未知严重度归入中并标注原值', async () => {
    withReview(
      [{ ...EXECUTED_CLEAN, hit_count: 1 }],
      [
        { category: 'structural', kind: 'a', path: 'a.py', line: 1, severity: 'critical', message: 'm', evidence: 'e' },
      ],
    )
    renderAt('/a/t1/structural')

    expect(await screen.findByText('中')).toBeInTheDocument()
    expect(screen.getByText('原值 critical')).toBeInTheDocument()
  })

  it('发现的行号可点跳转（AE-07）', async () => {
    withReview(
      [{ ...EXECUTED_CLEAN, hit_count: 1 }],
      [
        { category: 'structural', kind: 'a', path: 'a.py', line: 24, severity: 'high', message: 'm', evidence: 'e' },
      ],
    )
    renderAt('/a/t1/structural')

    const link = await screen.findByRole('link', { name: 'a.py:24' })
    expect(link).toHaveAttribute('href', expect.stringContaining('line=24'))
  })
})

describe('三页各自可达', () => {
  it('三个路由的标题与导航标签一致', async () => {
    for (const [route, title] of [
      ['/a/t1/security', 'Security Review'],
      ['/a/t1/error-handling', 'Error Handling'],
      ['/a/t1/structural', 'Structural'],
    ] as const) {
      stubApi()
      const view = renderAt(route)
      expect(
        await screen.findByRole('heading', { name: title, level: 1 }),
      ).toBeInTheDocument()
      view.unmount()
    }
  })

  it('切页在三页之间可用', async () => {
    stubApi()
    renderAt('/a/t1/structural')
    await screen.findByRole('heading', { name: 'Structural', level: 1 })

    const user = userEvent.setup()
    await user.click(screen.getByRole('link', { name: 'Security Review' }))
    expect(
      await screen.findByRole('heading', { name: 'Security Review', level: 1 }),
    ).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Structural', level: 1 })).not.toBeInTheDocument()
  })

  it('无该类别记录时给出说明', async () => {
    withReview([], [])
    renderAt('/a/t1/security')
    expect(await screen.findByText(/本次分析未产出该类别的检查记录/)).toBeInTheDocument()
  })

  it('未就绪时给出空态', async () => {
    stubApi({ result: () => json(fullResult({ completed: false })) })
    renderAt('/a/t1/security')
    expect(await screen.findByText(/分析结果尚未就绪/)).toBeInTheDocument()
  })
})
