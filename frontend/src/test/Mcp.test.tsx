/**
 * MCP 配置说明页（U18，R-58~R-60、NA-06）。
 *
 * NA-06 的判定方式是文本搜索：**页面不得有运行状态呈现**。MCP 走 stdio 由客户端拉起子进程，
 * 后端观测不到——显示状态灯等于显示假信息。
 */

import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderAt, stubApi } from './fixtures'

beforeEach(() => {
  window.localStorage.clear()
  stubApi()
})

const TOOLS = [
  'list_repos',
  'read_repo_structure',
  'read_file',
  'find_definition',
  'find_references',
  'query_dependencies',
  'search_code',
]

describe('工具清单（AE-16、R-58）', () => {
  it('列出 7 个工具的名称', async () => {
    renderAt('/mcp')
    await screen.findByRole('heading', { name: 'MCP', level: 1 })

    for (const name of TOOLS) {
      expect(screen.getByText(name), `缺少工具：${name}`).toBeInTheDocument()
    }
    // 恰好 7 个，不多不少——与 backend/mcp_server/server.py 的定义对应。
    expect(document.querySelectorAll('.mcp__tool')).toHaveLength(7)
  })

  it('每个工具都有非空的用途说明', async () => {
    renderAt('/mcp')
    await screen.findByRole('heading', { name: 'MCP', level: 1 })

    for (const item of document.querySelectorAll('.mcp__tool')) {
      const purpose = item.querySelector('dd')?.textContent ?? ''
      expect(purpose.trim().length).toBeGreaterThan(10)
    }
  })
})

describe('配置片段（AE-16、R-59）', () => {
  it('给出两个客户端可用的配置片段', async () => {
    renderAt('/mcp')
    await screen.findByRole('heading', { name: 'MCP', level: 1 })

    const block = document.querySelector('.mcp__config-body')!.textContent ?? ''
    expect(block).toContain('mcpServers')
    expect(block).toContain('backend.mcp_server.server')
    expect(block).toContain('cwd')

    // 两个客户端的文件位置都说明了。
    const page = document.body.textContent ?? ''
    expect(page).toContain('.claude.json')
    expect(page).toContain('.cursor/mcp.json')
  })

  it('配置片段可一键复制', async () => {
    renderAt('/mcp')
    // **顺序要紧：userEvent.setup() 会接管 navigator.clipboard**（它要支持自己的剪贴板
    // 交互 API）。在 setup 之前装替身会被它覆盖掉，断言随后数到 0 次调用。
    const user = userEvent.setup()
    // 显式标注参数类型，否则 mock.calls 的元素类型被推成空元组，取 [0] 是类型错误。
    const writeText = vi.fn(async (_text: string) => {})
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    })

    await user.click(await screen.findByRole('button', { name: '复制' }))

    expect(writeText).toHaveBeenCalledOnce()
    expect(writeText.mock.calls[0][0]).toContain('mcpServers')
    expect(await screen.findByText('已复制')).toBeInTheDocument()
  })

  it('剪贴板不可用时给出说明而非静默失败', async () => {
    renderAt('/mcp')
    const user = userEvent.setup()
    Object.defineProperty(navigator, 'clipboard', {
      value: {
        writeText: async () => {
          throw new Error('not allowed')
        },
      },
      configurable: true,
    })

    await user.click(await screen.findByRole('button', { name: '复制' }))

    expect(await screen.findByText(/无法访问剪贴板/)).toBeInTheDocument()
  })
})

describe('不显示运行状态（R-59、NA-06）', () => {
  it('页面无状态灯、无 Running 字样、无连接状态', async () => {
    renderAt('/mcp')
    await screen.findByRole('heading', { name: 'MCP', level: 1 })

    const text = document.body.textContent ?? ''
    for (const word of ['Running', '运行中', '已连接', '未连接', '连接状态', '在线', '离线']) {
      expect(text, `不该出现状态呈现：${word}`).not.toContain(word)
    }
    // 也不该有状态点之类的装饰元素。
    expect(document.querySelector('.page .status-dot')).toBeNull()
  })
})

describe('前置条件说明（R-60）', () => {
  it('说明工具读取已分析仓库的本地数据，需先完成一次分析', async () => {
    renderAt('/mcp')
    await screen.findByRole('heading', { name: 'MCP', level: 1 })

    expect(screen.getByText(/需要先完成一次分析/)).toBeInTheDocument()
    expect(document.body.textContent).toContain('已分析仓库的符号表')
  })

  it('分析未就绪时该页仍可访问（R-03 的例外）', async () => {
    renderAt('/mcp')
    expect(await screen.findByRole('heading', { name: 'MCP', level: 1 })).toBeInTheDocument()
    // 导航项本身不置灰。
    expect(screen.getByRole('link', { name: 'MCP' })).toBeInTheDocument()
  })
})
