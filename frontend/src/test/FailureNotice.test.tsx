/**
 * 失败呈现的共用组件（AE6、AE-18、R-53、R-68）。
 *
 * 这个组件被首页与 Overview 页共用，是十二个 reason 唯一的呈现收口。此前它只有经由页面
 * 的间接断言，且那些断言只比对后端 message 原文——hint 映射整张表没有任何保护，加错、
 * 加重复、给错语义都不会让任何测试变红。
 *
 * 最值得钉住的是 network_error 与 client_offline 的区分：前者是后端克隆时的网络失败
 * （已自动重试三次并退还限流配额），后者是请求没到后端。把「不占用限流配额」挂到后者上
 * 会把人指向错误的下一步，而两者的 message 都长得像网络问题，光看文案分不出来。
 */

import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { FAILURE_HINTS, FailureNotice } from '../components/FailureNotice'
import { errorResponse, fullResult, json, renderAt, seedCredentials, stubApi } from './fixtures'

function renderNotice(reason: string, message = '后端给出的原始说明') {
  return render(
    <MemoryRouter>
      <FailureNotice reason={reason} message={message} />
    </MemoryRouter>,
  )
}

/** 后端会产出的 reason（backend/ingest/guards.py 的枚举 + routes/files/search/export 的拒绝）。 */
const BACKEND_REASONS = [
  'invalid_url',
  'not_found',
  'no_access',
  'too_large',
  'network_error',
  'credentials_required',
  'rate_limited',
  'queue_full',
  'disk_quota',
  'task_not_found',
]

/** 前端自身产出的 reason（state/analysis.tsx、pages/ViewerPage.tsx）。 */
const FRONTEND_REASONS = ['client_offline', 'analysis_failed']

beforeEach(() => {
  window.localStorage.clear()
  seedCredentials()
})

describe('reason 到提示的映射', () => {
  it.each([...BACKEND_REASONS, ...FRONTEND_REASONS])('%s 有对应提示且非空', (reason) => {
    expect(FAILURE_HINTS[reason]).toBeTruthy()
  })

  it('每个 reason 的提示互不相同', () => {
    // 重复的提示等于没分类：用户看到同一句话，无从判断该改地址、换仓库还是重试。
    const hints = Object.values(FAILURE_HINTS)
    expect(new Set(hints).size).toBe(hints.length)
  })

  it('渲染时同时给出后端原文与本地提示', () => {
    renderNotice('too_large', '可解析文件数 23997 超出上限 1500')
    const alert = screen.getByRole('alert')
    // 原文里的实际数字不能被提示语盖掉——它才是用户判断「换哪个仓库」的依据。
    expect(alert).toHaveTextContent('23997')
    expect(alert).toHaveTextContent('1500')
    expect(alert).toHaveTextContent(FAILURE_HINTS.too_large)
  })

  it('未知 reason 仍显示原文而非空白或崩溃', () => {
    // 后端新增一类拒绝而前端还没跟上时，至少要把后端说明呈现出来。
    renderNotice('some_new_reason', '一种前端还不认识的失败')
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('一种前端还不认识的失败')
    expect(alert).toHaveTextContent('分析未能完成')
  })
})

describe('两类网络失败不共用语义', () => {
  it('network_error 说明配额已退还，可以直接重试', () => {
    renderNotice('network_error', 'GnuTLS recv error (-110)')
    expect(screen.getByRole('alert')).toHaveTextContent('不占用限流配额')
  })

  it('client_offline 不声称重试过，也不提配额', () => {
    renderNotice('client_offline', '无法连接后端')
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('无法连接后端服务')
    // 这两条是反向断言：请求没到后端，就不存在克隆重试，也没有配额被占用或退还。
    expect(alert).not.toHaveTextContent('限流配额')
    expect(alert).not.toHaveTextContent('自动重试')
  })

  it('两者提示分别指向不同的下一步', () => {
    expect(FAILURE_HINTS.network_error).not.toBe(FAILURE_HINTS.client_offline)
  })
})

describe('凭证缺失给出去设置页的入口（R-68）', () => {
  it('credentials_required 渲染指向设置页的链接', async () => {
    renderNotice('credentials_required', '未提供 LLM 凭证')
    const link = screen.getByRole('link', { name: '去设置页填写凭证' })
    expect(link).toHaveAttribute('href', '/settings')
    // 链接可键盘聚焦——失败提示是键盘用户遇到的第一个可操作元素。
    await userEvent.tab()
    expect(link).toHaveFocus()
  })

  it('其它 reason 不渲染该链接', () => {
    for (const reason of ['rate_limited', 'network_error', 'too_large', 'client_offline']) {
      const { unmount } = renderNotice(reason)
      expect(screen.queryByRole('link', { name: '去设置页填写凭证' })).not.toBeInTheDocument()
      unmount()
    }
  })
})

describe('首页与 Overview 页呈现一致', () => {
  it('首页提交被限流时显示限流提示', async () => {
    stubApi({
      submit: () => errorResponse('rate_limited', '提交过于频繁：3 次已用尽，请在 1800 秒后重试', 429),
    })
    renderAt('/')

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('GitHub 仓库地址'), 'https://github.com/acme/widget')
    await user.click(screen.getByRole('button', { name: '开始分析' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('1800 秒后重试')
    expect(alert).toHaveTextContent(FAILURE_HINTS.rate_limited)
  })

  it('Overview 页分析失败时用同一份提示表', async () => {
    stubApi({
      result: () =>
        json(fullResult({ failed: true, completed: false, error: '克隆失败：网络中断' })),
    })
    renderAt('/a/t1/overview')

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('克隆失败：网络中断')
    expect(alert).toHaveTextContent(FAILURE_HINTS.analysis_failed)
  })

  it('两页的失败标题一致，不各写一套文案', async () => {
    stubApi({ result: () => errorResponse('task_not_found', '任务不存在：t9', 404) })
    renderAt('/a/t9/overview')
    expect(await screen.findByRole('alert')).toHaveTextContent('分析未能完成')
  })
})
