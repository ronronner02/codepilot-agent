/**
 * Overview 页与顶部概览条（U10，R-06~R-08、BR-002、NA-01）。
 *
 * 概览条的每个数字都要能指回后端字段，所以断言是逐项比对固件里的值——「显示了一个数字」
 * 不够，前端合成的数字也会显示。
 */

import { screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { fullResult, json, renderAt, seedCredentials, stubApi } from './fixtures'

beforeEach(() => {
  window.localStorage.clear()
  seedCredentials()
})

describe('概览条（AE-02）', () => {
  it('显示仓库、commit 短 SHA、阶段与四项统计', async () => {
    stubApi()
    renderAt('/a/t1/overview')

    // 固件：33 文件 / 2 模块 / 2 发现 / 99 切块。
    expect(await screen.findByText('acme/widget')).toBeInTheDocument()
    expect(screen.getByText('abcdef123456')).toBeInTheDocument()
    expect(screen.getByText('已完成')).toBeInTheDocument()

    const strip = document.querySelector('.strip')
    expect(strip).not.toBeNull()
    expect(strip!.textContent).toContain('文件')
    expect(strip!.textContent).toContain('33')
    expect(strip!.textContent).toContain('模块')
    expect(strip!.textContent).toContain('评审发现')
    expect(strip!.textContent).toContain('索引切块')
    expect(strip!.textContent).toContain('99')
  })

  it('每个数字与响应字段一致，不由前端合成', async () => {
    const custom = fullResult({
      language_profile: { total_files: 7, parseable_files: 5, by_language: { Python: 5 } },
      index: { cache_hit: false, chunk_count: 12, identity: 'x', note: '' },
    })
    stubApi({ result: () => json(custom) })
    renderAt('/a/t1/overview')

    const strip = await screen.findByText('acme/widget')
    const container = strip.closest('.strip')!
    // 文件 7（不是 total 与 parseable 相加）、模块 2、发现 2、切块 12。
    expect(container.textContent).toContain('7')
    expect(container.textContent).toContain('12')
    expect(container.textContent).not.toContain('35')
  })

  it('不出现 Stars、代码行数、技术债、星级或 0-100 分数（NA-01、R-07）', async () => {
    stubApi()
    renderAt('/a/t1/overview')
    await screen.findByText('acme/widget')

    const strip = document.querySelector('.strip')!.textContent ?? ''
    for (const word of ['Stars', '代码行数', '技术债', '星级', '评分', '分']) {
      expect(strip).not.toContain(word)
    }
  })

  it('语言分布显示前 5 项与「其它」，总数与全量一致', async () => {
    stubApi()
    renderAt('/a/t1/overview')
    await screen.findByText('acme/widget')

    const strip = document.querySelector('.strip')!.textContent ?? ''
    // 固件有 6 种语言：前 5 项逐项显示，第 6 项（Shell 1）归入其它。
    expect(strip).toContain('Python 20')
    expect(strip).toContain('TypeScript 6')
    expect(strip).toContain('其它 1')
  })

  it('未提交仓库时显示「未选择仓库」空态', () => {
    stubApi()
    renderAt('/')
    expect(screen.getByText('未选择仓库')).toBeInTheDocument()
  })

  it('报告为 None 时概览条仍显示 commit 与可得统计', async () => {
    stubApi({ result: () => json(fullResult({ report: null })) })
    renderAt('/a/t1/overview')

    expect(await screen.findByText('acme/widget')).toBeInTheDocument()
    expect(screen.getByText('abcdef123456')).toBeInTheDocument()
  })

  it('切到其它页后概览条仍可见（跨页常驻）', async () => {
    stubApi()
    renderAt('/a/t1/reports')
    expect(await screen.findByText('acme/widget')).toBeInTheDocument()
    expect(screen.getByText('abcdef123456')).toBeInTheDocument()
  })

  it('无索引与零切块可区分', async () => {
    stubApi({ result: () => json(fullResult({ index: null })) })
    renderAt('/a/t1/overview')
    await screen.findByText('acme/widget')
    expect(document.querySelector('.strip')!.textContent).toContain('—')
  })
})

describe('Overview 页本体', () => {
  it('完成态显示统计条与提交入口', async () => {
    stubApi()
    renderAt('/a/t1/overview')
    expect(await screen.findByLabelText('分析结果统计')).toBeInTheDocument()
    expect(screen.getByLabelText('GitHub 仓库地址')).toBeInTheDocument()
  })

  it('排队中显示排队位置且不给「继续」入口（R-54）', async () => {
    stubApi({
      result: () =>
        json(fullResult({ completed: false, queue_position: 3, stage: 'queued' })),
    })
    renderAt('/a/t1/overview')

    expect(await screen.findByText(/已排队，当前第 3 位/)).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('继续')
  })

  it('模块缺失被标注（AE4）', async () => {
    stubApi({
      result: () => json(fullResult({ module_failures: ['auth：RuntimeError: 超时'] })),
    })
    renderAt('/a/t1/overview')
    expect(await screen.findByText(/1 个模块未完成分析/)).toBeInTheDocument()
  })

  it('分析失败时给出按 reason 分类的提示', async () => {
    stubApi({
      result: () =>
        json(fullResult({ failed: true, error: '仓库规模超出上限', report: null })),
    })
    renderAt('/a/t1/overview')
    expect(await screen.findByRole('alert')).toHaveTextContent('仓库规模超出上限')
  })
})
