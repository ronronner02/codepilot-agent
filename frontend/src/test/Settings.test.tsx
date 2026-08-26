/**
 * 设置页与凭证边界（U17，R-47~R-52、R-67、NA-02）。
 *
 * 最要紧的一条：**完整 key 不出现在界面任何位置**（R-51）。断言方式是在整页文本里搜完整
 * key——「输入框是 password 类型」不够，掩码遮的是显示而非 DOM 内容。
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { maskKey } from '../settings/credentials'
import { CREDENTIALS_KEY, renderAt, seedCredentials, stubApi } from './fixtures'

const FULL_KEY = 'sk-verysecret-abcd1234'

beforeEach(() => {
  window.localStorage.clear()
  stubApi()
})

describe('凭证不回显（R-51、AE-21）', () => {
  it('重新打开设置页时 key 输入框为空，只显示掩码', async () => {
    window.localStorage.setItem(
      CREDENTIALS_KEY,
      JSON.stringify({ base_url: 'https://x.example', api_key: FULL_KEY, model_flash: '', model_pro: '' }),
    )
    renderAt('/settings')

    const input = await screen.findByLabelText('API key')
    expect(input).toHaveValue('')
    // 占位符是掩码形态，尾四位可见。
    expect(input).toHaveAttribute('placeholder', maskKey(FULL_KEY))
    expect(maskKey(FULL_KEY)).toContain('1234')
  })

  it('完整 key 不出现在整页文本里', async () => {
    window.localStorage.setItem(
      CREDENTIALS_KEY,
      JSON.stringify({ base_url: 'https://x.example', api_key: FULL_KEY, model_flash: '', model_pro: '' }),
    )
    renderAt('/settings')
    await screen.findByLabelText('API key')

    expect(document.body.innerHTML).not.toContain(FULL_KEY)
    expect(document.body.textContent).not.toContain(FULL_KEY)
  })

  it('短 key 全部掩掉，不泄露大半', () => {
    expect(maskKey('abc123')).toBe('••••••')
    expect(maskKey('abcd1234')).toBe('••••1234')
    expect(maskKey('')).toBe('')
  })
})

describe('保存与清除', () => {
  it('保存后凭证写入 localStorage', async () => {
    renderAt('/settings')

    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('base_url'), 'https://api.example')
    await user.type(screen.getByLabelText('API key'), FULL_KEY)
    await user.click(screen.getByRole('button', { name: '保存' }))

    await screen.findByText(/凭证只存在这台浏览器里/, { selector: '.settings__notice' })
    const stored = JSON.parse(window.localStorage.getItem(CREDENTIALS_KEY) ?? '{}')
    expect(stored.base_url).toBe('https://api.example')
    expect(stored.api_key).toBe(FULL_KEY)
  })

  it('key 输入框留空即保持原值不变', async () => {
    window.localStorage.setItem(
      CREDENTIALS_KEY,
      JSON.stringify({ base_url: 'https://old.example', api_key: FULL_KEY, model_flash: '', model_pro: '' }),
    )
    renderAt('/settings')

    const user = userEvent.setup()
    await user.clear(await screen.findByLabelText('base_url'))
    await user.type(screen.getByLabelText('base_url'), 'https://new.example')
    await user.click(screen.getByRole('button', { name: '保存' }))

    await screen.findByText(/凭证只存在这台浏览器里/, { selector: '.settings__notice' })
    const stored = JSON.parse(window.localStorage.getItem(CREDENTIALS_KEY) ?? '{}')
    expect(stored.base_url).toBe('https://new.example')
    expect(stored.api_key).toBe(FULL_KEY)
  })

  it('清除后 localStorage 移除，状态回到未配置（R-52）', async () => {
    seedCredentials()
    renderAt('/settings')

    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: '清除设置' }))

    expect(await screen.findByText(/已清除。提交分析将按未配置处理/)).toBeInTheDocument()
    expect(window.localStorage.getItem(CREDENTIALS_KEY)).toBeNull()
    expect(screen.getByText(/当前状态：未配置/)).toBeInTheDocument()
  })

  it('部分填写时说明缺哪一项', async () => {
    renderAt('/settings')

    const user = userEvent.setup()
    // 只填 key，不填 base_url。
    await user.type(await screen.findByLabelText('API key'), FULL_KEY)
    await user.click(screen.getByRole('button', { name: '保存' }))

    expect(await screen.findByText(/还缺 base_url/)).toBeInTheDocument()
    expect(screen.getByText(/提交分析会被拒绝/)).toBeInTheDocument()
  })

  it('已配置时状态行说明可提交', async () => {
    seedCredentials()
    renderAt('/settings')
    expect(await screen.findByText(/当前状态：已配置，可提交分析/)).toBeInTheDocument()
  })
})

describe('不暴露 embedding 配置（R-67、AE-23）', () => {
  it('只有四个输入项', async () => {
    renderAt('/settings')
    await screen.findByLabelText('API key')

    const inputs = document.querySelectorAll('.settings__form input')
    expect(inputs).toHaveLength(4)
    expect(screen.getByLabelText('base_url')).toBeInTheDocument()
    expect(screen.getByLabelText('flash 档模型名')).toBeInTheDocument()
    expect(screen.getByLabelText('pro 档模型名')).toBeInTheDocument()
  })

  it('表单里不出现 embedding 相关配置项', async () => {
    renderAt('/settings')
    await screen.findByLabelText('API key')

    const form = document.querySelector('.settings__form')!.textContent ?? ''
    for (const word of ['embedding', 'Embedding', '向量模型', 'EMBEDDING']) {
      expect(form).not.toContain(word)
    }
  })

  it('页面说明向量索引由服务端统一构建', async () => {
    renderAt('/settings')
    expect(await screen.findByText(/向量索引由服务端统一构建/)).toBeInTheDocument()
  })
})

describe('存储不可用（隐私模式）', () => {
  it('localStorage 写入抛异常时给出说明而非静默失败', async () => {
    // **jsdom 的 localStorage 方法不可被单独覆写**——`vi.spyOn` 与直接赋值都不生效（实测：
    // 调用仍走原实现）。要模拟「写入抛异常」只能整体替换这个对象。
    //
    // 模拟的是 Safari 隐私窗口：读取可用而写入抛 QuotaExceededError。
    const real = window.localStorage
    const store = new Map<string, string>()
    Object.defineProperty(window, 'localStorage', {
      value: {
        getItem: (key: string) => store.get(key) ?? null,
        setItem: () => {
          throw new DOMException('QuotaExceededError')
        },
        removeItem: (key: string) => store.delete(key),
        clear: () => store.clear(),
        key: () => null,
        length: 0,
      },
      configurable: true,
    })

    try {
      renderAt('/settings')
      expect(await screen.findByText(/浏览器本地存储不可用/)).toBeInTheDocument()

      const user = userEvent.setup()
      await user.type(screen.getByLabelText('base_url'), 'https://x.example')
      await user.type(screen.getByLabelText('API key'), FULL_KEY)
      await user.click(screen.getByRole('button', { name: '保存' }))

      await waitFor(() =>
        expect(screen.getByText(/无法写入浏览器本地存储/)).toBeInTheDocument(),
      )
    } finally {
      Object.defineProperty(window, 'localStorage', { value: real, configurable: true })
    }
  })

  it('localStorage 内容被手改坏时按未配置处理，不崩', async () => {
    window.localStorage.setItem(CREDENTIALS_KEY, '{ 这不是 JSON')
    renderAt('/settings')
    expect(await screen.findByText(/当前状态：未配置/)).toBeInTheDocument()
  })
})

describe('设置页可达性', () => {
  it('分析未就绪时仍可访问（R-03 的例外）', async () => {
    renderAt('/settings')
    expect(await screen.findByRole('heading', { name: 'Settings', level: 1 })).toBeInTheDocument()
  })

  it('切页后焦点落在页标题', async () => {
    renderAt('/settings')
    const heading = await screen.findByRole('heading', { name: 'Settings', level: 1 })
    expect(heading).toHaveFocus()
  })
})
