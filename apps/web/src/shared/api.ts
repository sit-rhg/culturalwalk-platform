/**
 * 统一请求封装。页面里不要直接 fetch。
 *
 * 约定（见 docs/03-接口契约规范.md）：
 * - 前缀 /api/v1，由 vite 代理到后端
 * - 写操作自动带 Idempotency-Key
 * - 非 2xx 抛 ApiError，页面按 code 分支而不是 message 文案
 * - 访问令牌过期（401）时用 refresh_token 换一次并重试，换不到才清登录态
 */

import { MockError, handleMock } from './mock/handlers'
import { readItem, removeItem, writeItem } from './storage'

export interface ApiErrorBody {
  error: { code: string; message: string; request_id?: string; details?: unknown[] }
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId?: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

const BASE = '/api/v1'
const TOKEN_KEY = 'cw_token'
/** 与 shared/session.ts 里同一把钥匙：token 存 cw_token，刷新令牌存 cw_refresh_token */
const REFRESH_KEY = 'cw_refresh_token'

/**
 * 假数据开关。开发环境默认开，接口就绪后 `pnpm dev:real`（即 VITE_USE_MOCK=false）走真接口。
 * 页面永远只调 api.get/post/patch/del，不要自己判断 USE_MOCK。
 */
const mockFlag = import.meta.env.VITE_USE_MOCK
export const USE_MOCK =
  mockFlag === 'true' ? true : mockFlag === 'false' ? false : import.meta.env.DEV

function uuid(): string {
  return crypto.randomUUID()
}

let accessToken: string | null = readItem(TOKEN_KEY)
export function setToken(token: string | null): void {
  accessToken = token
  if (token) writeItem(TOKEN_KEY, token)
  else removeItem(TOKEN_KEY)
}

/** 刷新令牌也失效时调用，把本地登录态清干净，避免页面反复 401 */
export function clearSessionTokens(): void {
  setToken(null)
  removeItem(REFRESH_KEY)
}

/**
 * 登录态被服务端判定失效时广播：session.ts 收到后把 cw_user 一起清掉。
 * 这里不直接 import session，避免 api ↔ session 循环依赖；页面想跳登录页也监听这个事件。
 */
export const SESSION_EXPIRED_EVENT = 'cw:session-expired'

function expireSession(): void {
  clearSessionTokens()
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT))
  }
}

interface RequestOptions extends RequestInit {
  /** 写操作默认带 Idempotency-Key；只读或后端没要求的请求可以关掉 */
  idempotent?: boolean
  /** 强制走 shared/mock（接口未接入的页面用），即使已切到真接口 */
  forceMock?: boolean
}

function buildHeaders(init: RequestOptions, token: string | null): Headers {
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (init.body) headers.set('Content-Type', 'application/json')
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const isWrite = (init.method ?? 'GET').toUpperCase() !== 'GET'
  if (init.idempotent ?? isWrite) {
    headers.set('Idempotency-Key', uuid())
  }
  return headers
}

async function parseBody<T>(res: Response): Promise<T> {
  if (res.status === 204) return undefined as T

  const text = await res.text()
  let data: unknown = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      // 网关/nginx 出错时返回的是 HTML，不能让页面崩在 JSON.parse 上
      throw new ApiError(
        res.status,
        'BAD_RESPONSE',
        res.ok ? '响应格式不是 JSON' : `请求失败（${res.status}）`,
      )
    }
  }

  if (!res.ok) {
    // FastAPI 自己的错误体是 {"detail": "Not Found"}，没有 error 信封；
    // 直接取 err.error.code 会抛 TypeError，把真正的状态码埋掉。
    const envelope = (data as ApiErrorBody | null)?.error
    throw new ApiError(
      res.status,
      envelope?.code ?? 'UNKNOWN',
      envelope?.message ?? `请求失败（${res.status}）`,
      envelope?.request_id,
    )
  }
  return data as T
}

async function send<T>(path: string, init: RequestOptions, token: string | null): Promise<T> {
  const res = await fetch(BASE + path, {
    method: init.method,
    body: init.body,
    headers: buildHeaders(init, token),
    signal: init.signal,
  })
  return parseBody<T>(res)
}

interface RefreshResponse {
  access_token?: string
  refresh_token?: string
  error?: { code?: string; message?: string }
}

/** 多个请求同时撞上 401 时只发一次刷新，其余等同一个 promise */
let refreshing: Promise<string> | null = null

async function refreshAccessToken(): Promise<string> {
  if (refreshing) return refreshing
  const refreshToken = readItem(REFRESH_KEY)
  if (!refreshToken) {
    expireSession()
    throw new ApiError(401, 'UNAUTHORIZED', '登录已失效，请重新登录')
  }

  const pending = (async () => {
    let res: Response
    try {
      res = await fetch(BASE + '/auth/refresh', {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refreshToken }),
      })
    } catch {
      throw new ApiError(0, 'NETWORK_ERROR', '网络连接失败，请检查网络后重试')
    }

    let data: RefreshResponse | null = null
    try {
      data = (await res.json()) as RefreshResponse
    } catch {
      data = null
    }

    if (!res.ok || !data?.access_token) {
      // 只有服务端明确拒绝（401/403）才清登录态：5xx 或网关错误时保留凭证，
      // 否则一次网络抖动就把用户踢下线。
      if (res.status === 401 || res.status === 403) expireSession()
      throw new ApiError(
        res.status,
        data?.error?.code ?? 'UNAUTHORIZED',
        data?.error?.message ?? '登录已失效，请重新登录',
      )
    }
    setToken(data.access_token)
    if (data.refresh_token) writeItem(REFRESH_KEY, data.refresh_token)
    return data.access_token
  })()

  refreshing = pending
  try {
    return await pending
  } finally {
    refreshing = null
  }
}

export async function request<T>(path: string, init: RequestOptions = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase()

  if (USE_MOCK || init.forceMock) {
    const [pathname, search] = path.split('?')
    const query = new URLSearchParams(search ?? '')
    const body = init.body ? JSON.parse(String(init.body)) : undefined
    try {
      return await handleMock<T>(method, pathname, query, body, accessToken, {
        demo: Boolean(init.forceMock),
      })
    } catch (error) {
      if (error instanceof MockError) {
        throw new ApiError(error.status, error.code, error.message)
      }
      throw error
    }
  }

  try {
    return await send<T>(path, init, accessToken)
  } catch (error) {
    // 登录/注册/刷新自己返回的 401 是业务结果（密码错、刷新令牌过期），
    // 不能再拿去换令牌：否则 INVALID_CREDENTIALS 会被换成「登录已失效」。
    const isAuthEntry = path.startsWith('/auth/')
    const expired = error instanceof ApiError && error.status === 401
    if (!expired || isAuthEntry) throw error
    const fresh = await refreshAccessToken()
    return send<T>(path, init, fresh)
  }
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined }),
  patch: <T>(path: string, body: unknown, lockVersion: number) =>
    request<T>(path, {
      method: 'PATCH',
      body: JSON.stringify(body),
      headers: { 'If-Match': String(lockVersion) },
    }),
  /** PUT 是整表替换（如草稿节点），同样靠 If-Match 做乐观锁 */
  put: <T>(path: string, body: unknown, lockVersion: number) =>
    request<T>(path, {
      method: 'PUT',
      body: JSON.stringify(body),
      headers: { 'If-Match': String(lockVersion) },
    }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),

  /**
   * 接口尚未实现的页面专用：不管 USE_MOCK 与否，都从 shared/mock 取演示数据，
   * 这样切到真接口后这些页面不会 404。后端补上端点后把 api.mock 改回 api 即可。
   */
  mock: {
    get: <T>(path: string) => request<T>(path, { forceMock: true }),
    post: <T>(path: string, body?: unknown) =>
      request<T>(path, {
        method: 'POST',
        body: body ? JSON.stringify(body) : undefined,
        forceMock: true,
      }),
    patch: <T>(path: string, body: unknown, lockVersion: number) =>
      request<T>(path, {
        method: 'PATCH',
        body: JSON.stringify(body),
        headers: { 'If-Match': String(lockVersion) },
        forceMock: true,
      }),
    del: <T>(path: string) => request<T>(path, { method: 'DELETE', forceMock: true }),
  },
}
