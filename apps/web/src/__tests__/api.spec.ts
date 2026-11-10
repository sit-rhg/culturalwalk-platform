/**
 * 请求层（shared/api.ts）的契约测试。
 *
 * 这里锁的是「所有页面共用的那一条通道」——最容易被改动、又最不容易被发现弄坏的地方：
 *   - token 要不要带、带在哪
 *   - 写操作有没有 Idempotency-Key（后端靠它防重复提交，漏了会造重复数据）
 *   - 改操作有没有 If-Match（漏了会覆盖别人的修改）
 *   - 后端错误信封有没有变成带 code 的 ApiError（页面按 code 分支，不按文案）
 */
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

type ApiModule = typeof import('@/shared/api')
type StorageModule = typeof import('@/shared/storage')

let api: ApiModule['api']
let ApiError: ApiModule['ApiError']
let setToken: ApiModule['setToken']
let clearSessionTokens: ApiModule['clearSessionTokens']
let REQUEST_TIMEOUT_MS: ApiModule['REQUEST_TIMEOUT_MS']
let readItem: StorageModule['readItem']
let writeItem: StorageModule['writeItem']

const fetchMock = vi.fn()

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

beforeAll(async () => {
  // 走真请求分支：假数据开关关掉，再看 fetch 收到什么
  vi.stubEnv('VITE_USE_MOCK', 'false')
  vi.resetModules()
  const mod = await import('@/shared/api')
  api = mod.api
  ApiError = mod.ApiError
  setToken = mod.setToken
  clearSessionTokens = mod.clearSessionTokens
  REQUEST_TIMEOUT_MS = mod.REQUEST_TIMEOUT_MS
  const storage = await import('@/shared/storage')
  readItem = storage.readItem
  writeItem = storage.writeItem
})

afterEach(() => {
  fetchMock.mockReset()
  setToken(null)
  clearSessionTokens()
})

function installFetch(): void {
  vi.stubGlobal('fetch', fetchMock)
}

describe('共享请求层', () => {
  it('GET 不带 Authorization；setToken 之后每个请求都带上', async () => {
    installFetch()
    fetchMock.mockImplementation(async () => jsonResponse(200, { items: [] }))

    await api.get('/routes')
    let headers = fetchMock.mock.calls[0][1].headers as Headers
    expect(headers.get('Authorization')).toBeNull()

    setToken('token-abc')
    await api.get('/routes')
    headers = fetchMock.mock.calls[1][1].headers as Headers
    expect(headers.get('Authorization')).toBe('Bearer token-abc')
  })

  it('POST / DELETE 自动带 Idempotency-Key，GET 不带', async () => {
    installFetch()
    fetchMock.mockImplementation(async () => jsonResponse(201, { id: 'x' }))

    await api.post('/submissions', { title: '测试' })
    await api.get('/routes')
    const postHeaders = fetchMock.mock.calls[0][1].headers as Headers
    const getHeaders = fetchMock.mock.calls[1][1].headers as Headers

    expect(postHeaders.get('Idempotency-Key')).toMatch(/^[0-9a-f-]{36}$/)
    expect(getHeaders.get('Idempotency-Key')).toBeNull()
    // 两次写操作不能复用同一个 key，否则后端会把第二次当成重放吞掉
    await api.post('/submissions', { title: '测试' })
    const again = fetchMock.mock.calls[2][1].headers as Headers
    expect(again.get('Idempotency-Key')).not.toBe(postHeaders.get('Idempotency-Key'))
  })

  it('PATCH 把乐观锁版本放进 If-Match', async () => {
    installFetch()
    fetchMock.mockImplementation(async () => jsonResponse(200, { id: 'r1' }))

    await api.patch('/routes/r1', { title: '新标题' }, 7)
    const headers = fetchMock.mock.calls[0][1].headers as Headers

    expect(headers.get('If-Match')).toBe('7')
    expect(headers.get('Content-Type')).toBe('application/json')
  })

  it('204 返回 undefined，不把空响应当 JSON 解析', async () => {
    installFetch()
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }))

    await expect(api.del('/routes/r1')).resolves.toBeUndefined()
  })

  it('后端错误信封被翻译成带 code 的 ApiError', async () => {
    installFetch()
    fetchMock.mockResolvedValue(
      jsonResponse(422, {
        error: {
          code: 'REVIEW_REASON_REQUIRED',
          message: '退回必须写理由',
          request_id: 'req-9',
        },
      }),
    )

    await expect(api.post('/review/tasks/t1/decide', {})).rejects.toMatchObject({
      name: 'ApiError',
      status: 422,
      code: 'REVIEW_REASON_REQUIRED',
      requestId: 'req-9',
      message: '退回必须写理由',
    })
  })

  it('响应不是合法 JSON 时不崩，抛错里带状态码', async () => {
    installFetch()
    fetchMock.mockResolvedValue(new Response('<html>502</html>', { status: 502 }))

    const error = await api.get('/routes').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as InstanceType<ApiModule['ApiError']>).status).toBe(502)
  })

  it('401 时用 refresh_token 换新令牌，再拿新令牌重试原请求', async () => {
    installFetch()
    writeItem('cw_refresh_token', 'refresh-1')
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, { error: { code: 'UNAUTHORIZED', message: '过期' } }))
      .mockResolvedValueOnce(
        jsonResponse(200, { access_token: 'fresh-token', refresh_token: 'refresh-2' }),
      )
      .mockResolvedValueOnce(jsonResponse(200, { items: [] }))

    await expect(api.get('/users/me')).resolves.toEqual({ items: [] })

    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/auth/refresh')
    const retryHeaders = fetchMock.mock.calls[2][1].headers as Headers
    expect(retryHeaders.get('Authorization')).toBe('Bearer fresh-token')
    expect(readItem('cw_token')).toBe('fresh-token')
    // 轮换后的新 refresh_token 要存下来，否则下次刷新还是旧令牌
    expect(readItem('cw_refresh_token')).toBe('refresh-2')
  })

  it('刷新令牌也失效时清掉本地登录态，向上抛 401', async () => {
    installFetch()
    writeItem('cw_refresh_token', 'stale')
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, { error: { code: 'UNAUTHORIZED', message: '过期' } }))
      .mockResolvedValueOnce(
        jsonResponse(401, { error: { code: 'INVALID_REFRESH_TOKEN', message: '刷新失效' } }),
      )

    await expect(api.get('/users/me')).rejects.toMatchObject({ status: 401 })

    // 不清理的话页面会一直带着废 token 反复 401
    expect(readItem('cw_token')).toBeNull()
    expect(readItem('cw_refresh_token')).toBeNull()
  })

  it('默认 15s 没响应就中止，抛 code=TIMEOUT 的中文错误', async () => {
    vi.useFakeTimers()
    try {
      installFetch()
      // 永不 resolve 的请求，只有 abort 能让它结束——正好模拟后端卡死
      fetchMock.mockImplementation(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            init.signal?.addEventListener('abort', () =>
              reject(new DOMException('Aborted', 'AbortError')),
            )
          }),
      )

      const pending = api.get('/routes/slow').catch((error: unknown) => error)
      await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS + 1)
      const error = await pending

      expect(error).toBeInstanceOf(ApiError)
      expect(error).toMatchObject({
        status: 408,
        code: 'TIMEOUT',
        message: '请求超时（15 秒），请稍后重试',
      })
      // 超时必须真的把底层请求 abort 掉，否则连接一直挂着
      expect((fetchMock.mock.calls[0][1] as RequestInit).signal?.aborted).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('调用方 abort 时抛 ABORTED，不会被误判成超时', async () => {
    installFetch()
    fetchMock.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () =>
            reject(new DOMException('Aborted', 'AbortError')),
          )
        }),
    )
    const controller = new AbortController()

    const pending = api
      .get('/routes/slow', { signal: controller.signal })
      .catch((error: unknown) => error)
    controller.abort()
    const error = await pending

    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 0, code: 'ABORTED', message: '请求已取消' })
  })

  it('api.mock 在真接口模式下也走演示数据，并给出演示身份', async () => {
    // 没有 token：真接口会 401，演示分支要退回审核编辑，页面才画得出来
    const me = await api.mock.get<{ username: string; role: string }>('/users/me')

    expect(me.role).toBe('admin')
    expect(me.username).toBeTruthy()
  })
})
