/**
 * 创作页保存流程（pages/authoring/save.ts）的测试。
 *
 * 锁定的是三件事：If-Match 用的是当前 lock_version、412 会自动重拉最新草稿、
 * 其它失败（422 / 网络）原样交给页面按错误信封提示。
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

type Save = typeof import('@/pages/authoring/save')

let saveModule: Save

const fetchMock = vi.fn()

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

beforeAll(async () => {
  vi.stubEnv('VITE_USE_MOCK', 'false')
  vi.resetModules()
  saveModule = await import('@/pages/authoring/save')
})

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  fetchMock.mockReset()
})

function initOf(index = 0): RequestInit {
  return fetchMock.mock.calls[index][1] as RequestInit
}

const nodes = [
  { place_id: 'p-1', stay_minutes: 60, transport: 'walk' as const, note: null, price_min: null, price_max: null },
]

describe('保存草稿节点', () => {
  it('成功时只发一次 PUT，并把新版本交回页面', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { id: 'd-1', state: 'EDITING', lock_version: 5 }))

    const result = await saveModule.saveDraftNodes('d-1', nodes, 4)

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/v1/route-drafts/d-1/nodes')
    expect((initOf().headers as Headers).get('If-Match')).toBe('4')
    expect(result).toEqual({ ok: true, draft: { id: 'd-1', state: 'EDITING', lock_version: 5 } })
  })

  it('412 时自动重拉草稿，并给出「已载入最新版本」的可读提示', async () => {
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse(412, {
          error: { code: 'STALE_DRAFT_VERSION', message: '草稿已被修改，请刷新后重试' },
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, { id: 'd-1', state: 'EDITING', lock_version: 9, nodes: [] }),
      )

    const result = await saveModule.saveDraftNodes('d-1', nodes, 4)

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(String(fetchMock.mock.calls[1][0])).toBe('/api/v1/route-drafts/d-1')
    expect(result.ok).toBe(false)
    if (result.ok || !result.stale) throw new Error('412 必须落到 stale 分支')
    expect(result.draft?.lock_version).toBe(9)
    expect(result.message).toBe(saveModule.STALE_DRAFT_NOTICE)
  })

  it('重拉失败也不能沉默：给出「刷新页面再存」的提示，draft 为空', async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(412, { error: { code: 'STALE_DRAFT_VERSION', message: '' } }))
      .mockResolvedValueOnce(
        jsonResponse(500, { error: { code: 'INTERNAL', message: '服务暂时不可用' } }),
      )

    const result = await saveModule.saveDraftNodes('d-1', nodes, 4)

    expect(result.ok).toBe(false)
    if (result.ok || !result.stale) throw new Error('412 必须落到 stale 分支')
    expect(result.draft).toBeNull()
    expect(result.message).toBe(saveModule.STALE_DRAFT_RELOAD_FAILED)
  })

  it('422 校验失败原样交回页面，不触发重拉（否则用户改的内容会被冲掉）', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(422, {
        error: { code: 'VALIDATION_ERROR', message: '参数校验未通过：stay_minutes（小于最小值 10）' },
      }),
    )

    const result = await saveModule.saveDraftNodes('d-1', nodes, 4)

    expect(fetchMock).toHaveBeenCalledTimes(1)
    if (result.ok || result.stale) throw new Error('422 必须落到普通失败分支')
    expect(result.error).toMatchObject({ status: 422, code: 'VALIDATION_ERROR' })
  })
})
