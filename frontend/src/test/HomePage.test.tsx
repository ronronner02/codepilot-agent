/**
 * 首页与最近分析列表（U9，R-41~R-46、NA-01、NA-10）。
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import {
  CREDENTIALS_KEY,
  errorResponse,
  json,
  renderAt,
  seedCredentials,
  stubApi,
  summary,
} from './fixtures'

beforeEach(() => {
  window.localStorage.clear()
  seedCredentials()
})

describe('最近分析列表（AE-11）', () => {
  it('三条记录各含仓库、commit 短 SHA、时间、文件数、发现数', async () => {
    stubApi({
      list: () =>
        json([
          summary({ task_id: 'a', repo: 'acme/one', file_count: 33, finding_count: 6 }),
          summary({ task_id: 'b', repo: 'acme/two', created_at: 1_755_000_000 }),
          summary({ task_id: 'c', repo: 'acme/three', created_at: 1_754_000_000 }),
        ]),
    })
    renderAt('/')

    expect(await screen.findByRole('link', { name: 'acme/one' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'acme/two' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'acme/three' })).toBeInTheDocument()

    const items = screen.getAllByRole('listitem')
    const first = items.find((node) => node.textContent?.includes('acme/one'))
    expect(first).toBeDefined()
    expect(first!.textContent).toContain('abcdef123456')
    expect(first!.textContent).toContain('33 个文件')
    expect(first!.textContent).toContain('6 条发现')
  })

  it('列表按时间倒序，最新在最前（R-45）', async () => {
    stubApi({
      list: () =>
        json([
          summary({ task_id: 'old', repo: 'acme/old', created_at: 1_700_000_000 }),
          summary({ task_id: 'new', repo: 'acme/new', created_at: 1_800_000_000 }),
        ]),
    })
    renderAt('/')

    await screen.findByRole('link', { name: 'acme/new' })
    const links = screen.getAllByRole('link').map((node) => node.textContent)
    expect(links.indexOf('acme/new')).toBeLessThan(links.indexOf('acme/old'))
  })

  it('列表不含星级、评分或 0-100 的数字（NA-01）', async () => {
    stubApi({ list: () => json([summary()]) })
    renderAt('/')
    await screen.findByRole('link', { name: 'acme/widget' })

    const text = document.body.textContent ?? ''
    for (const word of ['评分', '星级', '技术债', 'Stars', '代码行数', '/100']) {
      expect(text).not.toContain(word)
    }
  })

  it('在跑与排队中的任务不出现，界面无「继续」入口（R-46、NA-10）', async () => {
    stubApi({
      list: () =>
        json([
          summary({ task_id: 'done', repo: 'acme/done', completed: true }),
          summary({ task_id: 'running', repo: 'acme/running', completed: false, stage: 'parsing' }),
          summary({ task_id: 'queued', repo: 'acme/queued', completed: false, stage: 'queued' }),
        ]),
    })
    renderAt('/')

    await screen.findByRole('link', { name: 'acme/done' })
    expect(screen.queryByRole('link', { name: 'acme/running' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'acme/queued' })).not.toBeInTheDocument()

    const text = document.body.textContent ?? ''
    expect(text).not.toContain('继续')
    expect(text).not.toContain('恢复')
  })

  it('失败的分析仍在列表但带失败标记', async () => {
    // 失败的分析 completed 也为 true。隐藏它会让刚提交失败的用户找不到任何记录；
    // 不标记则点进去是空报告页，用户会以为界面坏了。
    stubApi({
      list: () =>
        json([
          summary({ task_id: 'ok', repo: 'acme/ok' }),
          summary({ task_id: 'bad', repo: 'acme/bad', failed: true }),
        ]),
    })
    renderAt('/')

    await screen.findByRole('link', { name: 'acme/bad' })
    const items = screen.getAllByRole('listitem')
    const failed = items.find((node) => node.textContent?.includes('acme/bad'))
    expect(failed!.textContent).toContain('分析失败')

    const ok = items.find((node) => node.textContent?.includes('acme/ok'))
    expect(ok!.textContent).not.toContain('分析失败')
  })

  it('列表为空时显示说明而非空白区域', async () => {
    stubApi({ list: () => json([]) })
    renderAt('/')
    expect(await screen.findByText(/暂无历史分析/)).toBeInTheDocument()
  })

  it('列表请求失败时给出说明，输入框仍可用', async () => {
    stubApi({ list: () => errorResponse('http_500', '服务端错误', 500) })
    renderAt('/')

    expect(await screen.findByText(/历史列表不可用不影响提交新分析/)).toBeInTheDocument()
    expect(screen.getByLabelText('GitHub 仓库地址')).toBeEnabled()
    expect(screen.getByRole('button', { name: '开始分析' })).toBeEnabled()
  })

  it('点击列表项进入该次分析的 Overview 页', async () => {
    stubApi({ list: () => json([summary({ task_id: 'zz9', repo: 'acme/pick' })]) })
    renderAt('/')

    const user = userEvent.setup()
    await user.click(await screen.findByRole('link', { name: 'acme/pick' }))

    expect(await screen.findByRole('heading', { name: 'Overview', level: 1 })).toBeInTheDocument()
  })
})

describe('提交入口', () => {
  it('提交成功后进入该分析的 Overview 页', async () => {
    stubApi({})
    renderAt('/')

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('GitHub 仓库地址'), 'https://github.com/acme/widget')
    await user.click(screen.getByRole('button', { name: '开始分析' }))

    expect(await screen.findByRole('heading', { name: 'Overview', level: 1 })).toBeInTheDocument()
  })

  it('未配置凭证时被前端拦截并给出设置页入口（R-49）', async () => {
    window.localStorage.removeItem(CREDENTIALS_KEY)
    const { calls } = stubApi({})
    renderAt('/')

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('GitHub 仓库地址'), 'https://github.com/acme/widget')
    await user.click(screen.getByRole('button', { name: '开始分析' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/还没有配置 LLM 凭证/)
    expect(screen.getByRole('link', { name: '去设置页填写凭证' })).toBeInTheDocument()
    // 拦在前端就不该发请求。
    expect(calls.filter((call) => call.startsWith('POST'))).toHaveLength(0)
  })

  it('提交被限流时呈现可重试提示（R-53 的前端侧）', async () => {
    stubApi({
      submit: () =>
        errorResponse('rate_limited', '提交过于频繁：3 次已用尽，请在 1800 秒后重试', 429),
    })
    renderAt('/')

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('GitHub 仓库地址'), 'https://github.com/acme/widget')
    await user.click(screen.getByRole('button', { name: '开始分析' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/1800 秒后重试/)
    // 仍停在首页，不跳到一个没有 taskId 的分析页。
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: '分析一个仓库' })).toBeInTheDocument(),
    )
  })
})
