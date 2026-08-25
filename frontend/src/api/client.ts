/**
 * REST 客户端。
 *
 * 错误一律抛 ApiRequestError 并带上 reason：调用方按 reason 分类呈现（AE6），而不是
 * 解析 message 文本——文本会随后端文案调整而变。
 */

import type { AnalysisResult, AnalyzeAccepted, Answer, ApiError } from './types'

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

export function submitAnalysis(repoUrl: string): Promise<AnalyzeAccepted> {
  return request<AnalyzeAccepted>('/api/analyses', {
    method: 'POST',
    body: JSON.stringify({ repo_url: repoUrl }),
  })
}

export function fetchResult(taskId: string): Promise<AnalysisResult> {
  return request<AnalysisResult>(`/api/analyses/${encodeURIComponent(taskId)}`)
}

export function askQuestion(taskId: string, question: string): Promise<Answer> {
  return request<Answer>(`/api/analyses/${encodeURIComponent(taskId)}/questions`, {
    method: 'POST',
    body: JSON.stringify({ question }),
  })
}
