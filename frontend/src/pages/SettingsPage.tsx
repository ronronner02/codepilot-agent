/**
 * 设置页（U17，R-47~R-52、R-67）。
 *
 * 四个输入项：base_url、API key、flash 档模型名、pro 档模型名。凭证只写 localStorage，
 * 只在提交分析与提问的请求体里发出去——服务端结构上没有存它的地方（BR-004）。
 *
 * **key 不回显完整值**（R-51）。存的值能读出来做请求，但界面上只显示掩码形态（尾四位）。
 * 实现方式是「已保存的 key」与「输入框里的新 key」分成两个状态：输入框初始为空，占位符
 * 显示掩码。这让「不回显」成为结构性的——没有任何代码路径把完整 key 写进 DOM。
 *
 * **不出现 embedding 配置项**（R-67）。向量索引由服务端统一构建：索引缓存键含 provider
 * 身份，让访客各带一份会让键分裂、缓存全部失效，每次检索都要重新向量化。
 */

import { useState } from 'react'
import {
  EMPTY_CREDENTIALS,
  clearCredentials,
  isConfigured,
  loadCredentials,
  maskKey,
  missingFields,
  saveCredentials,
  storageAvailable,
} from '../settings/credentials'
import { usePageHeading } from './usePageHeading'

export function SettingsPage() {
  const headingRef = usePageHeading<HTMLHeadingElement>()
  const [stored, setStored] = useState(() => loadCredentials())
  // 输入框里的 key 与已存的 key 分开：前者初始为空，后者只以掩码呈现。
  const [keyInput, setKeyInput] = useState('')
  const [baseUrl, setBaseUrl] = useState(stored.base_url)
  const [modelFlash, setModelFlash] = useState(stored.model_flash)
  const [modelPro, setModelPro] = useState(stored.model_pro)
  const [notice, setNotice] = useState('')
  const available = storageAvailable()

  const handleSave = (event: React.FormEvent) => {
    event.preventDefault()
    const next = {
      base_url: baseUrl.trim(),
      // key 输入框留空表示「不改动已存的值」——否则每次改模型名都得重输一遍 key。
      api_key: keyInput.trim() || stored.api_key,
      model_flash: modelFlash.trim(),
      model_pro: modelPro.trim(),
    }
    if (!saveCredentials(next)) {
      setNotice('无法写入浏览器本地存储。隐私模式或存储被禁用时会这样，凭证未保存。')
      return
    }
    setStored(next)
    setKeyInput('')
    const missing = missingFields(next)
    setNotice(
      missing.length > 0
        ? `已保存，但配置不完整：还缺 ${missing.join('、')}。提交分析会被拒绝。`
        : '已保存。凭证只存在这台浏览器里，不会发送给服务端做持久化。',
    )
  }

  const handleClear = () => {
    clearCredentials()
    setStored({ ...EMPTY_CREDENTIALS })
    setKeyInput('')
    setBaseUrl('')
    setModelFlash('')
    setModelPro('')
    setNotice('已清除。提交分析将按未配置处理。')
  }

  return (
    <section className="page">
      <h1 className="page__title" ref={headingRef} tabIndex={-1}>
        Settings
      </h1>

      {/* 原文在 JSX 里写了 Markdown 的 `**`——那会渲染成字面星号。JSX 不解析 Markdown。 */}
      <p className="page__note">
        凭证只存在这台浏览器里，随请求发出，服务端不保存。
      </p>

      {!available ? (
        <p className="page__note" role="alert">
          浏览器本地存储不可用，关闭标签页后需要重填。
        </p>
      ) : null}

      <form className="settings__form" onSubmit={handleSave} noValidate>
        <label className="settings__label" htmlFor="cred-base-url">
          base_url
        </label>
        <input
          id="cred-base-url"
          className="settings__field"
          type="text"
          value={baseUrl}
          placeholder="https://api.deepseek.com"
          onChange={(event) => setBaseUrl(event.target.value)}
        />

        <label className="settings__label" htmlFor="cred-api-key">
          API key
        </label>
        <input
          id="cred-api-key"
          className="settings__field"
          // type=password：默认掩码显示（R-51）。
          type="password"
          value={keyInput}
          placeholder={stored.api_key ? maskKey(stored.api_key) : '未配置'}
          autoComplete="off"
          onChange={(event) => setKeyInput(event.target.value)}
        />
        <p className="settings__hint">
          {stored.api_key
            ? `已保存一个 key（${maskKey(stored.api_key)}）。留空即保持不变，填入新值即覆盖。`
            : '尚未配置。未配置时提交分析会被拒绝。'}
        </p>

        <label className="settings__label" htmlFor="cred-model-flash">
          flash 档模型名
        </label>
        <input
          id="cred-model-flash"
          className="settings__field"
          type="text"
          value={modelFlash}
          placeholder="留空则用服务端默认"
          onChange={(event) => setModelFlash(event.target.value)}
        />

        <label className="settings__label" htmlFor="cred-model-pro">
          pro 档模型名
        </label>
        <input
          id="cred-model-pro"
          className="settings__field"
          type="text"
          value={modelPro}
          placeholder="留空则用服务端默认"
          onChange={(event) => setModelPro(event.target.value)}
        />

        <div className="settings__actions">
          <button type="submit">保存</button>
          <button type="button" onClick={handleClear}>
            清除设置
          </button>
        </div>
      </form>

      {notice ? (
        <p className="settings__notice" role="status">
          {notice}
        </p>
      ) : null}

      <p className="settings__status">
        当前状态：{isConfigured(stored) ? '已配置，可提交分析' : '未配置'}
      </p>

      <h2 className="page__section-title">关于向量索引</h2>
      <p className="page__note">
        向量索引由服务端统一构建，所以这里没有 embedding 配置项。
      </p>
    </section>
  )
}
