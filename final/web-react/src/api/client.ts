// api/client.ts — 统一 fetch 封装：注入 Bearer token、401 回调、baseURL。
// 逐字移植自 Vue 版 src/api/client.js，请求/响应处理语义保持一致。
// 开发期 BASE 为空走 vite proxy；生产可用 VITE_API_BASE 直连后端。

const BASE = (import.meta as unknown as { env?: Record<string, string> }).env?.VITE_API_BASE || ''

export function apiURL(path: string): string { return BASE + path }

let unauthorizedHandler: (() => void) | null = null
export function setUnauthorizedHandler(fn: () => void): void { unauthorizedHandler = fn }

export function getToken(): string {
  return (typeof localStorage !== 'undefined' && localStorage.getItem('agi_auth_token')) || ''
}

export async function apiFetch(path: string, options: RequestInit = {}): Promise<Response> {
  const headers = new Headers(options.headers || {})
  const tok = getToken()
  if (tok) headers.set('Authorization', 'Bearer ' + tok)
  options.headers = headers
  const resp = await fetch(BASE + path, options)
  if (resp.status === 401) {
    if (unauthorizedHandler) unauthorizedHandler()
    throw new Error('unauthorized')
  }
  return resp
}

// fetchJSON — 读文本再解析，兼容空 body / 非 JSON 错误体。
export async function fetchJSON(path: string, options?: RequestInit): Promise<any> {
  const resp = await apiFetch(path, options)
  const raw = await resp.text()
  let data: any = {}
  if (raw) { try { data = JSON.parse(raw) } catch { data = { error: raw } } }
  if (!resp.ok) {
    const detail = data.error || data.detail || raw || `HTTP ${resp.status}`
    const error = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail)) as Error & { status?: number; data?: unknown }
    error.status = resp.status
    error.data = data
    throw error
  }
  return data
}
