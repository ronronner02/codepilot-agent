/**
 * 引用下钻贯通（U13，R-12、R-23、R-26、R-28、R-33、R-37）。
 *
 * 四处引用（报告结论、评审发现、检索命中、问答引用）都要能点进查看器并定位。这份测试的重点
 * 是**四处都通**——单处能点不代表统一收口成立，而漏掉的那一处不会报错，只会点不动。
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { formatLocation } from '../components/CitationLink'
import { viewerPath } from '../routes/paths'
import { fullResult, json, renderAt, searchResult, seedCredentials, stubApi } from './fixtures'

beforeEach(() => {
  window.localStorage.clear()
  seedCredentials()
})

describe('地址构造', () => {
  it('带行号时跳转地址含 line 参数', () => {
    expect(viewerPath('t1', 'auth/jwt.py', 45)).toContain('line=45')
    expect(viewerPath('t1', 'auth/jwt.py', 45)).toContain('path=auth%2Fjwt.py')
  })

  it('line 为 0 时不带 line 参数（R-22）', () => {
    const path = viewerPath('t1', 'core/app.py', 0)
    expect(path).not.toContain('line=')
    expect(path).toContain('path=core%2Fapp.py')
  })

  it('引用文本形态完整含路径与行范围（R-37）', () => {
    expect(formatLocation('auth/jwt.py', 45, 78)).toBe('auth/jwt.py:45-78')
    expect(formatLocation('auth/jwt.py', 45, 45)).toBe('auth/jwt.py:45')
    expect(formatLocation('auth/jwt.py', 45, null)).toBe('auth/jwt.py:45')
    expect(formatLocation('auth/jwt.py', 0)).toBe('auth/jwt.py')
  })
})

describe('报告结论的引用（AE-07）', () => {
  it('点击后进入查看器并定位到起始行', async () => {
    stubApi()
    renderAt('/a/t1/reports')
    await screen.findByRole('heading', { name: '架构报告' })

    const user = userEvent.setup()
    // 固件的引用是 auth/jwt.py:45-78。
    await user.click(screen.getByRole('link', { name: 'auth/jwt.py:45-78' }))

    expect(await screen.findByRole('heading', { name: '代码查看器' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('import jwt')).toBeInTheDocument())
    expect(document.querySelector('.code__line--on')).not.toBeNull()
  })

  it('长引用路径用等宽形态完整呈现、不截断（AE-19 回归）', async () => {
    const long = 'very/deeply/nested/package/module/submodule/implementation_detail.py'
    stubApi({
      result: () =>
        json(
          fullResult({
            report: {
              ...fullResult().report!,
              sections: [
                {
                  key: 'k',
                  title: '模块划分',
                  claims: [
                    { text: '结论', citations: [{ path: long, line: 100, end_line: 240 }] },
                  ],
                },
              ],
            },
          }),
        ),
    })
    renderAt('/a/t1/reports')

    const link = await screen.findByRole('link', { name: `${long}:100-240` })
    // 完整路径与行号都在文本里，没有省略号。
    expect(link.textContent).toBe(`${long}:100-240`)
    expect(link.textContent).not.toContain('…')
    expect(link.className).toContain('citation')
  })
})

describe('评审发现的引用（AE-07）', () => {
  it('点击某条发现跳到查看器并定位', async () => {
    stubApi()
    renderAt('/a/t1/structural')
    await screen.findByRole('heading', { name: 'Structural', level: 1 })

    const user = userEvent.setup()
    await user.click(screen.getByRole('link', { name: 'auth/jwt.py:24' }))

    expect(await screen.findByRole('heading', { name: '代码查看器' })).toBeInTheDocument()
  })

  it('line 为 0 的发现不产生指向第 0 行的链接', async () => {
    stubApi()
    renderAt('/a/t1/structural')
    await screen.findByRole('heading', { name: 'Structural', level: 1 })

    // core/app.py 的发现 line 为 0：呈现为「属于文件整体」。
    expect(screen.getByText('该问题属于文件整体')).toBeInTheDocument()
    for (const link of screen.getAllByRole('link')) {
      expect(link.getAttribute('href') ?? '').not.toContain('line=0')
    }
  })
})

describe('节点详情与检索的引用', () => {
  it('模块成员文件可点（AE-03）', async () => {
    stubApi()
    renderAt('/a/t1/architecture')
    await screen.findByText('模块清单')

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: /auth/ }))
    await user.click(screen.getByRole('link', { name: 'auth/session.py' }))

    expect(await screen.findByRole('heading', { name: '代码查看器' })).toBeInTheDocument()
  })

  it('检索结果可点，定位到命中行范围（AE-08）', async () => {
    stubApi({ search: () => json(searchResult()) })
    renderAt('/a/t1/search')
    await screen.findByRole('heading', { name: 'Code Search', level: 1 })

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('检索内容'), 'token 校验')
    await user.click(screen.getByRole('button', { name: '检索' }))

    const link = await screen.findByRole('link', { name: 'auth/jwt.py:45-78' })
    expect(link).toHaveAttribute('href', expect.stringContaining('line=45'))
  })

  it('问答引用可点（AE-20）', async () => {
    stubApi({
      question: () =>
        json({
          question: 'q',
          found: true,
          answer: '校验逻辑在 auth/jwt.py:45-78。',
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
    await user.type(screen.getByLabelText('关于这个仓库的问题'), '怎么校验 token')
    await user.click(screen.getByRole('button', { name: '提问' }))

    const link = await screen.findByRole('link', { name: 'auth/jwt.py:45-78' })
    expect(link).toHaveAttribute('href', expect.stringContaining('line=45'))
  })
})

describe('目标已清理时的跳转', () => {
  it('跳转仍发生，查看器显示「代码副本已清理」而非静默失败', async () => {
    stubApi({
      file: () =>
        new Response(
          JSON.stringify({ reason: 'workspace_cleared', message: '代码副本已被清理' }),
          { status: 410, headers: { 'Content-Type': 'application/json' } },
        ),
    })
    renderAt('/a/t1/reports')
    await screen.findByRole('heading', { name: '架构报告' })

    const user = userEvent.setup()
    await user.click(screen.getByRole('link', { name: 'auth/jwt.py:45-78' }))

    expect(await screen.findByRole('heading', { name: '代码查看器' })).toBeInTheDocument()
    expect(await screen.findAllByText(/代码副本已清理/)).not.toHaveLength(0)
  })
})
