/**
 * Reports 页（U15，R-34~R-37、BR-002、NA-01）。
 *
 * 最容易走偏的一条：**不出现任何评分**。origin 的原始方案有评分条，而这一页是最容易「顺手加
 * 回去」的地方。通过率也必须取后端字段而非前端重算——重算会让界面与导出产生两个来源。
 */

import { screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { fullResult, json, renderAt, seedCredentials, stubApi } from './fixtures'

beforeEach(() => {
  window.localStorage.clear()
  seedCredentials()
})

describe('报告全文（AE-10）', () => {
  it('五节结构、引用、通过率与缺失说明齐备', async () => {
    stubApi()
    renderAt('/a/t1/reports')

    expect(await screen.findByRole('heading', { name: '架构报告' })).toBeInTheDocument()
    expect(screen.getByText('模块划分')).toBeInTheDocument()
    expect(screen.getByText('认证逻辑集中在 auth/jwt.py')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'auth/jwt.py:45-78' })).toBeInTheDocument()
    expect(screen.getByText('共 26 条结论：25 条引用完全有效。')).toBeInTheDocument()
    expect(screen.getByText('分析的缺失部分')).toBeInTheDocument()
  })

  it('通过率文案与响应的 validation_summary 一致，不由前端重算', async () => {
    stubApi({
      result: () =>
        json(
          fullResult({
            report: {
              ...fullResult().report!,
              validation_summary: '共 40 条结论：31 条引用完全有效。',
            },
          }),
        ),
    })
    renderAt('/a/t1/reports')

    expect(await screen.findByText('共 40 条结论：31 条引用完全有效。')).toBeInTheDocument()
    // 前端没有另算出一个不同的数字。
    expect(document.body.textContent).not.toContain('26 条结论')
  })

  it('页面不出现任何评分或 0-100 分数（R-35、NA-01）', async () => {
    stubApi()
    renderAt('/a/t1/reports')
    await screen.findByRole('heading', { name: '架构报告' })

    const text = document.body.textContent ?? ''
    for (const word of ['架构评分', '代码质量分', '安全分', '星级', '技术债', '/100', '总分']) {
      expect(text).not.toContain(word)
    }
  })

  it('被丢弃的无法核验结论显式呈现，不折叠隐藏（R-36）', async () => {
    stubApi({
      result: () =>
        json(
          fullResult({
            report: {
              ...fullResult().report!,
              unsupported_claims: [
                '[deps] 某结论 —— 引用路径不存在',
                '[flow] 另一结论 —— 行号越界',
              ],
            },
          }),
        ),
    })
    renderAt('/a/t1/reports')

    expect(await screen.findByText(/2 条结论因引用无法核验被丢弃/)).toBeInTheDocument()
  })

  it('缺失部分为空时仍显示该节', async () => {
    stubApi()
    renderAt('/a/t1/reports')
    await screen.findByRole('heading', { name: '架构报告' })
    expect(screen.getByText('本次分析无缺失部分')).toBeInTheDocument()
  })

  it('报告为 None 时显示说明而非空白页', async () => {
    stubApi({ result: () => json(fullResult({ report: null })) })
    renderAt('/a/t1/reports')
    expect(await screen.findByText(/本次分析未产出架构报告/)).toBeInTheDocument()
    // 评审部分仍在。
    expect(screen.getByText('评审执行情况')).toBeInTheDocument()
  })
})

describe('评审执行情况表（R-34）', () => {
  it('三类各自的状态、命中数与范围/原因并列', async () => {
    stubApi()
    renderAt('/a/t1/reports')

    const table = await screen.findByRole('table')
    expect(table.textContent).toContain('结构类')
    expect(table.textContent).toContain('安全可疑模式')
    expect(table.textContent).toContain('错误处理')
    expect(table.textContent).toContain('已执行')
    expect(table.textContent).toContain('未执行')
    expect(table.textContent).toContain('未配置 LLM provider')
  })

  it('未执行时命中数显示「—」而非 0', async () => {
    stubApi()
    renderAt('/a/t1/reports')

    const table = await screen.findByRole('table')
    const rows = table.querySelectorAll('tbody tr')
    const securityRow = [...rows].find((row) => row.textContent?.includes('安全可疑模式'))
    expect(securityRow).toBeDefined()
    // 0 会被读成「查过了没问题」。
    expect(securityRow!.textContent).toContain('—')
  })

  it('无评审记录时给出说明', async () => {
    stubApi({ result: () => json(fullResult({ review: null })) })
    renderAt('/a/t1/reports')
    expect(await screen.findByText(/本次分析未产出评审执行记录/)).toBeInTheDocument()
  })

  it('模块缺失另有标注', async () => {
    stubApi({
      result: () => json(fullResult({ module_failures: ['core：超时', 'auth：解析失败'] })),
    })
    renderAt('/a/t1/reports')
    expect(await screen.findByText(/2 个模块未完成分析/)).toBeInTheDocument()
  })
})

describe('导出入口（U19、U20 的前端侧）', () => {
  it('给出三种格式的下载入口', async () => {
    stubApi()
    renderAt('/a/t1/reports')

    expect(await screen.findByRole('link', { name: 'Markdown' })).toHaveAttribute(
      'href',
      expect.stringContaining('format=md'),
    )
    expect(screen.getByRole('link', { name: 'HTML' })).toHaveAttribute(
      'href',
      expect.stringContaining('format=html'),
    )
    expect(screen.getByRole('link', { name: 'PDF' })).toHaveAttribute(
      'href',
      expect.stringContaining('format=pdf'),
    )
  })

  it('三个入口指向同一端点的不同 format', async () => {
    // R-40 的「失败时明确报错」是后端契约，由 tests/test_export_endpoint.py 断言。
    // 前端不预先解释一个还没发生的失败。
    stubApi()
    renderAt('/a/t1/reports')
    await screen.findByRole('heading', { name: 'Reports', level: 1 })
    const hrefs = ['Markdown', 'HTML', 'PDF'].map(
      (n) => screen.getByRole('link', { name: n }).getAttribute('href') ?? '',
    )
    expect(hrefs.every((h) => h.includes('/export?format='))).toBe(true)
    expect(new Set(hrefs).size).toBe(3)
  })
})
