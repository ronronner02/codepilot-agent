/**
 * REST 客户端。
 *
 * 错误一律抛 ApiRequestError 并带上 reason：调用方按 reason 分类呈现（AE6），而不是
 * 解析 message 文本——文本会随后端文案调整而变。
 */

import type { Credentials } from '../settings/credentials'
import type {
  AnalysisResult,
  AnalysisSummary,
  AnalyzeAccepted,
  Answer,
  ApiError,
  FileContent,
  FileTree,
  SearchResult,
} from './types'

/** 后端返回的可分类错误。 */
export class ApiRequestError extends Error {
  readonly reason: string
  readonly status: number

  constructor(status: number, reason: string, message: string) {
    super(message)
    this.name = 'ApiRequestError'
    this.status = status
    this.reason = reason
  }
}

/** 网络层失败（连不上、超时）。与后端返回的业务错误区分——前者该重试，后者该改输入。 */
export class NetworkError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'NetworkError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    })
  } catch (cause) {
    throw new NetworkError(
      `无法连接后端（${cause instanceof Error ? cause.message : String(cause)}）`,
    )
  }

  if (!response.ok) {
    // 错误响应体可能不是 JSON（网关返回的 502 页面、代理错误）。解析失败时退回状态码，
    // 不让一个解析异常掩盖真正的 HTTP 错误。
    let payload: Partial<ApiError> = {}
    try {
      payload = (await response.json()) as Partial<ApiError>
    } catch {
      payload = {}
    }
    throw new ApiRequestError(
      response.status,
      payload.reason ?? `http_${response.status}`,
      payload.message ?? `请求失败（HTTP ${response.status}）`,
    )
  }

  return (await response.json()) as T
}

/**
 * 提交分析。凭证随请求体发出（BR-004）——不存服务端，每次请求自带。
 *
 * 凭证为空对象时不带该字段：后端会以 `credentials_required` 拒绝，而那正是未配置时应有的
 * 行为（R-68 要求拦在提交阶段）。前端另有一道拦截并引导到设置页（R-49）。
 */
export function submitAnalysis(
  repoUrl: string,
  credentials?: Credentials,
): Promise<AnalyzeAccepted> {
  return request<AnalyzeAccepted>('/api/analyses', {
    method: 'POST',
    body: JSON.stringify({ repo_url: repoUrl, credentials: credentials ?? null }),
  })
}

export function fetchResult(taskId: string): Promise<AnalysisResult> {
  return request<AnalysisResult>(`/api/analyses/${encodeURIComponent(taskId)}`)
}

export function askQuestion(
  taskId: string,
  question: string,
  credentials?: Credentials,
): Promise<Answer> {
  return request<Answer>(`/api/analyses/${encodeURIComponent(taskId)}/questions`, {
    method: 'POST',
    body: JSON.stringify({ question, credentials: credentials ?? null }),
  })
}

/** 最近分析列表（U9）。 */
export function fetchAnalyses(): Promise<AnalysisSummary[]> {
  return request<AnalysisSummary[]>('/api/analyses')
}

/**
 * 读某文件的一段内容（U12）。
 *
 * 行范围可选：查看器首次打开整个文件，跳转到某条发现时也读整个文件再高亮目标行——只读
 * 目标附近几行会让用户看不到上下文，而那是「点进去核验」的全部意义。
 */
export function fetchFile(
  taskId: string,
  path: string,
  startLine?: number,
): Promise<FileContent> {
  const params = new URLSearchParams({ path })
  if (startLine && startLine > 0) params.set('start_line', String(startLine))
  return request<FileContent>(
    `/api/analyses/${encodeURIComponent(taskId)}/file?${params.toString()}`,
  )
}

/** 列某目录的直接子项（U12）。按需展开单层，不预取全树。 */
export function fetchTree(taskId: string, path = '.'): Promise<FileTree> {
  const params = new URLSearchParams({ path })
  return request<FileTree>(
    `/api/analyses/${encodeURIComponent(taskId)}/tree?${params.toString()}`,
  )
}

/** 语义检索（U16）。embedding 用服务端凭证，所以这里不带访客凭证。 */
export function searchCode(
  taskId: string,
  query: string,
  limit = 8,
): Promise<SearchResult> {
  return request<SearchResult>(`/api/analyses/${encodeURIComponent(taskId)}/search`, {
    method: 'POST',
    body: JSON.stringify({ query, limit }),
  })
}
