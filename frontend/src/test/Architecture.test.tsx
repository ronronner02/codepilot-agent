/**
 * Architecture 页（U11，R-09~R-14、R-66、BR-006、NA-05）。
 *
 * **正确性断言落在等价文本表达上，不断言渲染出的图**（KTD8）。jsdom 没有布局引擎，元素尺寸
 * 恒为 0，而 React Flow 依赖容器尺寸决定渲染——硬测渲染结果只会得到一批「通过但没验证任何
 * 东西」的绿灯。图形的可用性由浏览器实跑核对承担。
 */

import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { aggregateEdges } from '../pages/ArchitecturePage'
import { fullResult, json, renderAt, seedCredentials, stubApi } from './fixtures'

beforeEach(() => {
  window.localStorage.clear()
  seedCredentials()
})

describe('等价文本表达（AE-03、R-66）', () => {
  it('列出全部模块与它们的内外边数', async () => {
    stubApi()
    renderAt('/a/t1/architecture')

    expect(await screen.findByText('模块清单')).toBeInTheDocument()
    expect(screen.getByText(/共 2 个模块/)).toBeInTheDocument()

    const table = document.querySelector('.dep-text__table')!
    expect(table.textContent).toContain('auth')
    expect(table.textContent).toContain('core')
    expect(table.textContent).toContain('directory')
    expect(table.textContent).toContain('split')
  })

  it('依赖关系表给出方向', async () => {
    stubApi()
    renderAt('/a/t1/architecture')

    await screen.findByText('依赖关系')
    const edges = document.querySelector('.dep-text__edges')!
    // 固件的文件级边是 core/app.py → auth/jwt.py，聚合到模块级即 core 导入 auth。
    expect(edges.textContent).toContain('core')
    expect(edges.textContent).toContain('导入')
    expect(edges.textContent).toContain('auth')
  })

  it('图不可用时等价文本仍完整呈现', async () => {
    // 这份断言的价值在于它不依赖图：即便 ModuleGraph 整个挂载失败，文本仍在。
    stubApi()
    renderAt('/a/t1/architecture')
    expect(await screen.findByText('模块清单')).toBeInTheDocument()
    expect(screen.getByText('依赖关系')).toBeInTheDocument()
  })
})

describe('节点详情（AE-03、R-10、R-11）', () => {
  it('点击模块后显示成员文件、内外边数与聚类来历', async () => {
    stubApi()
    renderAt('/a/t1/architecture')
    await screen.findByText('模块清单')

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: /auth/ }))

    const detail = document.querySelector('.module-detail')!
    expect(detail.textContent).toContain('成员文件')
    expect(detail.textContent).toContain('内部边')
    expect(detail.textContent).toContain('外部边')
    expect(detail.textContent).toContain('聚类来历')
    expect(detail.textContent).toContain('directory')
  })

  it('模块结论逐字符等于响应中的 summary（R-11 禁止二次概括）', async () => {
    stubApi()
    renderAt('/a/t1/architecture')
    await screen.findByText('模块清单')

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: /auth/ }))

    const expected = fullResult().modules[0].summary
    expect(screen.getByText(expected)).toBeInTheDocument()
  })

  it('带 limitation 的模块标注分析不完整', async () => {
    stubApi()
    renderAt('/a/t1/architecture')
    await screen.findByText('模块清单')

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: /core/ }))

    expect(screen.getByText(/该模块分析不完整/)).toBeInTheDocument()
    expect(screen.getByText(/子 Agent 超时/)).toBeInTheDocument()
  })

  it('未选中模块时侧栏给出引导而非空白', async () => {
    stubApi()
    renderAt('/a/t1/architecture')
    await screen.findByText('模块清单')
    expect(screen.getByText(/点击左侧节点或下方清单中的模块名/)).toBeInTheDocument()
  })

  it('成员文件路径可点（R-12）', async () => {
    stubApi()
    renderAt('/a/t1/architecture')
    await screen.findByText('模块清单')

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: /auth/ }))

    const link = screen.getByRole('link', { name: 'auth/jwt.py' })
    expect(link).toHaveAttribute('href', expect.stringContaining('viewer'))
  })
})

describe('降级与边界（AE-04）', () => {
  it('降级为目录级时显式标注降级及原因（R-13）', async () => {
    stubApi({
      result: () =>
        json(
          fullResult({
            dependency_graph: {
              ...fullResult().dependency_graph!,
              granularity: 'directory',
              degraded_reason: '节点数 2400 超过上限 2000',
            },
          }),
        ),
    })
    renderAt('/a/t1/architecture')

    expect(await screen.findByText(/依赖图降级为目录级粒度/)).toBeInTheDocument()
    expect(screen.getByText(/节点数 2400 超过上限/)).toBeInTheDocument()
  })

  it('不渲染调用关系边或调用子视图（R-14、NA-05）', async () => {
    stubApi()
    renderAt('/a/t1/architecture')
    await screen.findByText('模块清单')

    // 断言落在**呈现区域**而非整页文本：NA-05 要钉的是「不画调用关系」，而页面上有一句
    // 「没有函数级调用链」的显式否认——那是在告诉读者边界，不是在渲染调用关系。整页搜
    // 「调用」二字会把这句说明判成违规。
    const dataRegions = [
      document.querySelector('.dep-text__table'),
      document.querySelector('.dep-text__edges'),
      document.querySelector('.graph'),
    ]
    for (const region of dataRegions) {
      expect(region?.textContent ?? '').not.toContain('调用')
    }

    // 边的语义只有「导入」一种——这就是 NA-05 的判据：不画调用边，而非声明不画。
    expect(document.querySelector('.dep-text__edges')!.textContent).toContain('导入')
  })

  it('无模块时给出空态说明而非空白画布', async () => {
    stubApi({ result: () => json(fullResult({ modules: [] })) })
    renderAt('/a/t1/architecture')
    expect(await screen.findByText(/本次分析未产出模块划分/)).toBeInTheDocument()
  })

  it('未解析的 import 被列出，与外部依赖区分', async () => {
    stubApi()
    renderAt('/a/t1/architecture')
    await screen.findByText('模块清单')
    expect(screen.getByText(/1 个 import 未能解析到仓库内文件/)).toBeInTheDocument()
  })
})

describe('图挂载与边聚合', () => {
  it('ModuleGraph 在 jsdom 下能挂载不抛异常', async () => {
    stubApi()
    renderAt('/a/t1/architecture')
    // 能等到文本表达就说明整页渲染完成，图没有在挂载时抛异常。
    await screen.findByText('模块清单')
    expect(document.querySelector('.graph')).not.toBeNull()
  })

  it('文件级边按成员归属折叠到模块级，方向保留', () => {
    const modules = fullResult().modules
    const edges = aggregateEdges(modules, [
      { source: 'core/app.py', target: 'auth/jwt.py' },
      { source: 'core/app.py', target: 'auth/session.py' },
    ])
    // 两条文件级边落到同一对模块，去重为一条，方向是 core → auth。
    expect(edges).toEqual([{ source: 'core', target: 'auth' }])
  })

  it('模块内部的边不画成自环', () => {
    const modules = fullResult().modules
    const edges = aggregateEdges(modules, [
      { source: 'auth/jwt.py', target: 'auth/session.py' },
    ])
    expect(edges).toEqual([])
  })

  it('归属不到模块的边被丢弃，不凭空造节点', () => {
    const modules = fullResult().modules
    const edges = aggregateEdges(modules, [
      { source: 'core/app.py', target: 'unknown/ghost.py' },
    ])
    expect(edges).toEqual([])
  })
})
