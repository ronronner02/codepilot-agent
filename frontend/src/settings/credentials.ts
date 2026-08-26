/**
 * 访客 LLM 凭证的浏览器本地存储（U17，R-48、R-51、R-52、BR-004）。
 *
 * **凭证只存 localStorage，只在请求体里发出去。** 不发给服务端做持久化——服务端结构上
 * 就没有存它的地方（响应模型不含该字段，落盘的是响应形状）。这让 BR-004 不依赖「记得
 * 不要存」。
 *
 * **不出现 embedding 配置项**（R-67）。向量索引由服务端统一构建：索引缓存键含 provider
 * 身份，让访客各带一份会让键分裂、缓存全部失效。
 *
 * localStorage 在隐私模式下可能抛异常（不只是返回 null——Safari 的隐私窗口里写入直接
 * 抛 QuotaExceededError）。所以每次访问都包 try/catch，并把「存储不可用」作为一种可呈现
 * 的状态返回，而不是静默失败。
 */

const STORAGE_KEY = 'codepilot.credentials.v1'

export interface Credentials {
  base_url: string
  api_key: string
  model_flash: string
  model_pro: string
}

export const EMPTY_CREDENTIALS: Credentials = {
  base_url: '',
  api_key: '',
  model_flash: '',
  model_pro: '',
}

/** 存储可用性。隐私模式下要给说明而非静默失败。 */
export function storageAvailable(): boolean {
  try {
    const probe = '__codepilot_probe__'
    window.localStorage.setItem(probe, '1')
    window.localStorage.removeItem(probe)
    return true
  } catch {
    return false
  }
}

export function loadCredentials(): Credentials {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return { ...EMPTY_CREDENTIALS }
    const parsed = JSON.parse(raw) as Partial<Credentials>
    return {
      base_url: typeof parsed.base_url === 'string' ? parsed.base_url : '',
      api_key: typeof parsed.api_key === 'string' ? parsed.api_key : '',
      model_flash: typeof parsed.model_flash === 'string' ? parsed.model_flash : '',
      model_pro: typeof parsed.model_pro === 'string' ? parsed.model_pro : '',
    }
  } catch {
    // 解析失败按未配置处理：一个被手改坏的 localStorage 条目不该让整个界面崩掉。
    return { ...EMPTY_CREDENTIALS }
  }
}

export function saveCredentials(value: Credentials): boolean {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(value))
    return true
  } catch {
    return false
  }
}

export function clearCredentials(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    // 清不掉也没有补救动作，但界面仍应回到未配置态。
  }
}

/**
 * 是否已完整配置。
 *
 * 判据是 base_url 与 api_key 都非空，与后端的 `_guest_credentials` 一致。只填 key 时用
 * 服务端的 base_url 在技术上可行，但那等于让访客部分借用服务端配置，而 BR-004 要的是
 * 「访客自带」这件事本身可判定。两个模型名可留空，回落到服务端的默认档位名。
 */
export function isConfigured(value: Credentials): boolean {
  return value.base_url.trim().length > 0 && value.api_key.trim().length > 0
}

/** 缺哪一项。设置页据此说明「部分填写」的情形（R-49 的可操作性）。 */
export function missingFields(value: Credentials): string[] {
  const missing: string[] = []
  if (!value.base_url.trim()) missing.push('base_url')
  if (!value.api_key.trim()) missing.push('API key')
  return missing
}

/**
 * 掩码形态（R-51）。
 *
 * 只留尾四位。完整值不出现在任何界面文本里——存的值能读出来做请求，但界面上只显示掩码。
 * 短于 8 位时全部掩掉：留尾四位会让一个 6 位的 key 泄露三分之二。
 */
export function maskKey(apiKey: string): string {
  const trimmed = apiKey.trim()
  if (!trimmed) return ''
  if (trimmed.length < 8) return '•'.repeat(trimmed.length)
  return `${'•'.repeat(trimmed.length - 4)}${trimmed.slice(-4)}`
}
