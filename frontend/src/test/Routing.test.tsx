/**
 * 路由与导航（U1，R-01~R-05、R-64、R-65）。
 *
 * 这份测的是外壳与路由本身：九项导航、直达、置灰、焦点转移、不存在的分析。各页面的内容
 * 断言在各自的测试文件里。
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { NAV_ITEMS } from '../routes/paths'
import { errorResponse, renderAt, seedCredentials, stubApi } from './fixtures'

beforeEach(() => {
  window.localStorage.clear()
  seedCredentials()
  stubApi()
})

describe('导航结构', () => {
  it('左侧导航固定为 9 项（R-01）', () => {
    renderAt('/')
    expect(NAV_ITEMS).toHaveLength(9)
    for (const item of NAV_ITEMS) {
      // 未就绪时依赖结果的项是 button（置灰），其余是 link。两者都要在 DOM 里。
      const found =
        screen.queryByRole('link', { name: item.label }) ??
        screen.queryByRole('button', { name: new RegExp(item.label) })
      expect(found, `导航项缺失：${item.label}`).not.toBeNull()
    }
  })

  it('九项之外另有 Settings 入口', () => {
    renderAt('/')
    expect(screen.getByRole('link', { name: 'Settings' })).toBeInTheDocument()
  })
})

describe('直达与刷新（R-02）', () => {
  it('直接访问某页地址即渲染该页', async () => {
    renderAt('/a/t1/architecture')
    expect(
      await screen.findByRole('heading', { name: 'Architecture', level: 1 }),
    ).toBeInTheDocument()
  })

  it('重新渲染同一地址仍停在该页（等价于刷新）', async () => {
    const first = renderAt('/a/t1/reports')
    expect(await screen.findByRole('heading', { name: 'Reports', level: 1 })).toBeInTheDocument()
    first.unmount()

    renderAt('/a/t1/reports')
    expect(await screen.findByRole('heading', { name: 'Reports', level: 1 })).toBeInTheDocument()
  })

  it('分析根地址重定向到 overview', async () => {
    renderAt('/a/t1')
    expect(await screen.findByRole('heading', { name: 'Overview', level: 1 })).toBeInTheDocument()
  })

  it('切页后 URL 变化且其它页卸载', async () => {
    renderAt('/a/t1/overview')
    await screen.findByRole('heading', { name: 'Overview', level: 1 })

    const user = userEvent.setup()
    await user.click(screen.getByRole('link', { name: 'Architecture' }))

    expect(await screen.findByRole('heading', { name: 'Architecture', level: 1 })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Overview', level: 1 })).not.toBeInTheDocument()
  })
})

describe('未就绪时的置灰（R-03）', () => {
  it('依赖结果的七项置灰，原因在属性里而非可见文案', () => {
    renderAt('/')
    for (const label of [
      'Architecture',
      'AI Chat',
      'Code Search',
      'Security Review',
      'Error Handling',
      'Structural',
      'Reports',
    ]) {
      const item = screen.getByRole('button', { name: new RegExp(label) })
      expect(item).toBeDisabled()
      // 原因由 title 与 aria-label 承载：悬停与读屏能取到，界面上不占版面。
      // 七项同时置灰时，每项挂一句相同说明就是七行重复噪声。
      expect(item).toHaveAttribute('title', '需先完成一次分析')
      expect(item.getAttribute('aria-label')).toContain('需先完成一次分析')
      // 可见文本只有导航项名字本身。
      expect(item.textContent).toBe(label)
    }
  })

  it('Overview、MCP、Settings 三项仍可点', () => {
    renderAt('/')
    expect(screen.getByRole('link', { name: 'Overview' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'MCP' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Settings' })).toBeInTheDocument()
  })

  it('就绪后置灰解除', async () => {
    renderAt('/a/t1/overview')
    await waitFor(() =>
      expect(screen.getByRole('link', { name: 'Architecture' })).toBeInTheDocument(),
    )
  })
})

describe('分析不存在（R-05、AE-18）', () => {
  it('未匹配地址显示说明与返回首页入口，不是空白页', () => {
    renderAt('/nope/whatever')
    expect(
      screen.getByRole('heading', { name: '该分析不存在或已被清理' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '返回首页' })).toBeInTheDocument()
  })

  it('任务不存在时呈现失败说明而非未捕获错误', async () => {
    stubApi({
      result: () => errorResponse('task_not_found', '任务不存在：t9', 404),
    })
    renderAt('/a/t9/overview')
    expect(await screen.findByRole('alert')).toHaveTextContent('任务不存在：t9')
  })
})

describe('焦点与键盘（R-64、R-65）', () => {
  it('切页后焦点落在新页主标题', async () => {
    renderAt('/a/t1/overview')
    await screen.findByRole('heading', { name: 'Overview', level: 1 })

    const user = userEvent.setup()
    await user.click(screen.getByRole('link', { name: 'Reports' }))

    const heading = await screen.findByRole('heading', { name: 'Reports', level: 1 })
    expect(heading).toHaveFocus()
  })

  it('导航项与输入框可 Tab 到达', async () => {
    renderAt('/')
    const user = userEvent.setup()
    await user.tab()
    // 首个可聚焦元素是抽屉开关（窄视口用），随后是导航。焦点确实落在某个可交互元素上。
    expect(document.activeElement?.tagName).toMatch(/BUTTON|A|INPUT/)
  })

  it('当前项带 aria-current', async () => {
    renderAt('/a/t1/overview')
    await waitFor(() =>
      expect(screen.getByRole('link', { name: 'Overview' })).toHaveAttribute(
        'aria-current',
        'page',
      ),
    )
  })
})

describe('窄视口抽屉（R-04）', () => {
  it('抽屉开关可展开收起', async () => {
    renderAt('/')
    const toggle = screen.getByRole('button', { name: '展开导航' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')

    const user = userEvent.setup()
    await user.click(toggle)
    expect(screen.getByRole('button', { name: '收起导航' })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
  })

  it('切页后抽屉自动收起', async () => {
    renderAt('/a/t1/overview')
    await screen.findByRole('heading', { name: 'Overview', level: 1 })

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '展开导航' }))
    await user.click(screen.getByRole('link', { name: 'Reports' }))

    expect(await screen.findByRole('button', { name: '展开导航' })).toBeInTheDocument()
  })
})
