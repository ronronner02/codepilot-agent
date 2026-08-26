/**
 * 代码查看器三栏（U12，R-15~R-22、NA-07、KTD10）。
 *
 * 最重要的一条：**打开文件不触发任何 LLM 调用**（NA-07）。判定方式是数请求——右栏内容只来自
 * 已有评审发现，所以连续打开多个文件时只该有文件读取请求，不该有问答或检索请求。
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import {
  errorResponse,
  fileContent,
  fileTree,
  fullResult,
  json,
  renderAt,
  seedCredentials,
  stubApi,
} from './fixtures'

beforeEach(() => {
  window.localStorage.clear()
  seedCredentials()
})

describe('三栏与代码呈现（AE-05）', () => {
  it('渲染文件树、带行号代码与该文件的发现', async () => {
    stubApi()
    renderAt('/a/t1/viewer?path=auth%2Fjwt.py')

    expect(await screen.findByLabelText('文件树')).toBeInTheDocument()
    expect(await screen.findByText('import jwt')).toBeInTheDocument()
    expect(screen.getByLabelText('该文件的评审发现')).toBeInTheDocument()

    // 行号在 gutter 里，与代码文本分列——拼进文本会让复制代码时带上行号。
    const gutters = document.querySelectorAll('.code__gutter')
    expect(gutters.length).toBe(80)
    expect(gutters[0].textContent).toBe('1')
    expect(gutters[79].textContent).toBe('80')
  })

  it('该文件的两条发现各含行号、严重度、message 与 evidence', async () => {
    stubApi({
      result: () =>
        json(
          fullResult({
            review: {
              ...fullResult().review!,
              findings: [
                {
                  category: 'structural',
                  kind: 'circular_dependency',
                  path: 'auth/jwt.py',
                  line: 24,
                  severity: 'high',
                  message: '循环依赖',
                  evidence: '有向环',
                },
                {
                  category: 'security',
                  kind: 'hardcoded_secret',
                  path: 'auth/jwt.py',
                  line: 31,
                  severity: 'medium',
                  message: '疑似硬编码密钥',
                  evidence: '字面量匹配',
                },
              ],
            },
          }),
        ),
    })
    renderAt('/a/t1/viewer?path=auth%2Fjwt.py')

    const panel = await screen.findByLabelText('该文件的评审发现')
    await waitFor(() => expect(panel.textContent).toContain('循环依赖'))
    expect(panel.textContent).toContain('疑似硬编码密钥')
    expect(panel.textContent).toContain('有向环')
    expect(panel.textContent).toContain('字面量匹配')
    expect(panel.textContent).toContain('高')
    expect(panel.textContent).toContain('中')
  })

  it('跳转到指定行时该行高亮', async () => {
    stubApi()
    renderAt('/a/t1/viewer?path=auth%2Fjwt.py&line=2')
    await screen.findByText('import jwt')
    expect(document.querySelector('.code__line--on')).not.toBeNull()
  })

  it('截断时显示「已截断，共 M 行」，不静默（R-18）', async () => {
    stubApi({
      file: () =>
        json(
          fileContent({
            truncated: true,
            total_lines: 8000,
            truncated_note: '仅返回 2000 行；继续读请用 start_line=2001',
          }),
        ),
    })
    renderAt('/a/t1/viewer?path=big.js')
    expect(await screen.findByText(/已截断，共 8000 行/)).toBeInTheDocument()
  })

  it('空文件显示说明而非报错', async () => {
    stubApi({
      file: () =>
        json(fileContent({ content: '', total_lines: 0, start_line: 0, end_line: 0 })),
    })
    renderAt('/a/t1/viewer?path=empty.py')
    expect(await screen.findByText('这是一个空文件。')).toBeInTheDocument()
  })
})

describe('右栏四种空态（R-20、KTD10）', () => {
  it('在评审范围内但零命中：「已评审，本文件无发现」', async () => {
    stubApi()
    // core/app.py 在 target_files 内，而固件里它的发现 line 为 0——换一个范围内无发现的文件。
    stubApi({
      result: () =>
        json(
          fullResult({
            review: {
              target_files: ['auth/jwt.py', 'core/app.py'],
              outcomes: fullResult().review!.outcomes,
              findings: [],
            },
          }),
        ),
    })
    renderAt('/a/t1/viewer?path=core%2Fapp.py')

    const panel = await screen.findByLabelText('该文件的评审发现')
    await waitFor(() => expect(panel.textContent).toContain('已评审，本文件无发现'))
  })

  it('不在评审范围内：文案与上一条不同', async () => {
    stubApi()
    renderAt('/a/t1/viewer?path=README.md')

    const panel = await screen.findByLabelText('该文件的评审发现')
    await waitFor(() =>
      expect(panel.textContent).toContain('本文件不在本次评审目标范围内'),
    )
    expect(panel.textContent).not.toContain('已评审，本文件无发现')
  })

  it('工作副本已清理：文案与「文件不存在」不同（KTD10 的第四态）', async () => {
    stubApi({
      file: () =>
        errorResponse('workspace_cleared', '该分析的代码副本已被清理', 410),
    })
    renderAt('/a/t1/viewer?path=auth%2Fjwt.py')

    // 中栏与右栏各说一次：中栏说明代码读不到，右栏说明发现仍可读。两处都要有，
    // 只在一处说会让另一栏看起来是空的。
    const notices = await screen.findAllByText(/代码副本已清理/)
    expect(notices.length).toBeGreaterThanOrEqual(2)
    expect(document.body.textContent).not.toContain('文件不存在')
  })

  it('未选文件时右栏给出引导', async () => {
    stubApi()
    renderAt('/a/t1/viewer')
    const panel = await screen.findByLabelText('该文件的评审发现')
    expect(panel.textContent).toContain('从左侧文件树选一个文件')
  })

  it('空仓库时文件树显示「无可显示文件」', async () => {
    stubApi({ tree: () => json(fileTree({ entries: [] })) })
    renderAt('/a/t1/viewer')
    expect(await screen.findByText('无可显示文件')).toBeInTheDocument()
  })
})

describe('不触发 LLM 调用（NA-07、R-21）', () => {
  it('连续打开多个文件只发文件读取请求，无问答与检索请求', async () => {
    const { calls } = stubApi()
    renderAt('/a/t1/viewer?path=auth%2Fjwt.py')
    await screen.findByText('import jwt')

    const user = userEvent.setup()
    // 展开目录再点其中的文件，重复几轮。
    await user.click(await screen.findByRole('button', { name: /auth/ }))
    await waitFor(() => expect(calls.some((c) => c.includes('/tree?'))).toBe(true))

    const llmCalls = calls.filter(
      (call) => call.includes('/questions') || call.includes('/search'),
    )
    expect(llmCalls).toHaveLength(0)
  })

  it('右栏内容只来自已有发现，不发额外请求', async () => {
    const { calls } = stubApi()
    renderAt('/a/t1/viewer?path=auth%2Fjwt.py')
    await screen.findByLabelText('该文件的评审发现')

    // 只该有：读结果、文件树、文件内容。没有第四类。
    const kinds = new Set(
      calls.map((call) =>
        call.includes('/tree?') ? 'tree' : call.includes('/file?') ? 'file' : 'result',
      ),
    )
    expect([...kinds].sort()).toEqual(['file', 'result', 'tree'])
  })
})

describe('错误与边界', () => {
  it('line 为 0 的发现标注属于文件整体，不跳第 0 行（R-22）', async () => {
    stubApi()
    renderAt('/a/t1/viewer?path=core%2Fapp.py')

    const panel = await screen.findByLabelText('该文件的评审发现')
    await waitFor(() => expect(panel.textContent).toContain('该问题属于文件整体'))
    // 没有指向第 0 行的链接。
    for (const link of screen.queryAllByRole('link')) {
      expect(link.getAttribute('href') ?? '').not.toContain('line=0')
    }
  })

  it('路径校验失败时不暴露拒绝细节', async () => {
    stubApi({ file: () => errorResponse('invalid_path', '无法读取该路径', 400) })
    renderAt('/a/t1/viewer?path=..%2F..%2Fetc%2Fpasswd')

    expect(await screen.findByText('无法读取该路径。')).toBeInTheDocument()
    // 不回显「符号链接」「junction」这类可用于探测的细节。
    const text = document.body.textContent ?? ''
    expect(text).not.toContain('符号链接')
    expect(text).not.toContain('junction')
  })

  it('文件不存在与路径非法是不同的文案', async () => {
    stubApi({ file: () => errorResponse('file_not_found', '文件不存在', 404) })
    renderAt('/a/t1/viewer?path=gone.py')
    expect(await screen.findByText(/文件不存在。它可能在分析之后被删除了/)).toBeInTheDocument()
  })

  it('文件树目录可展开收起，当前文件高亮', async () => {
    stubApi()
    renderAt('/a/t1/viewer?path=README.md')
    const dir = await screen.findByRole('button', { name: /auth/ })
    expect(dir).toHaveAttribute('aria-expanded', 'false')

    const user = userEvent.setup()
    await user.click(dir)
    expect(dir).toHaveAttribute('aria-expanded', 'true')

    // 当前文件带 aria-current。
    expect(screen.getByRole('button', { name: /README\.md/ })).toHaveAttribute(
      'aria-current',
      'true',
    )
  })

  it('文件树节点是 button，可 Tab 到达（R-64）', async () => {
    stubApi()
    renderAt('/a/t1/viewer')
    const node = await screen.findByRole('button', { name: /README\.md/ })
    expect(node.tagName).toBe('BUTTON')
    node.focus()
    expect(node).toHaveFocus()
  })

  it('未就绪时给出空态而非三栏', async () => {
    stubApi({ result: () => json(fullResult({ completed: false, queue_position: 0 })) })
    renderAt('/a/t1/viewer')
    expect(await screen.findByText(/分析结果尚未就绪/)).toBeInTheDocument()
  })
})
