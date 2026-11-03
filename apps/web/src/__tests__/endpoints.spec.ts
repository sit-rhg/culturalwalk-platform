/**
 * 页面数据入口（api/endpoints.ts）的契约测试。
 *
 * 每个切真页面至少走一条真实路径：用后端真形状的响应喂 fetch，
 * 断言 URL / 方法 / 请求体，以及页面拿到的视图模型。
 * 这里是最容易「看起来通了其实字段名不对」的地方。
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

type Endpoints = typeof import('@/api/endpoints')
type Adapters = typeof import('@/api/adapters')

let endpoints: Endpoints
let adapters: Adapters

const fetchMock = vi.fn()

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function emptyResponse(status = 204): Response {
  return new Response(null, { status })
}

beforeAll(async () => {
  vi.stubEnv('VITE_USE_MOCK', 'false')
  vi.resetModules()
  endpoints = await import('@/api/endpoints')
  adapters = await import('@/api/adapters')
})

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  fetchMock.mockReset()
})

/** fetch 被调用时的真实参数在 mock.calls[i][0]（url）与 [1]（init） */
function urlOf(index = 0): string {
  return String(fetchMock.mock.calls[index][0])
}

function initOf(index = 0): RequestInit {
  return fetchMock.mock.calls[index][1] as RequestInit
}

function bodyOf(index = 0): unknown {
  const raw = initOf(index).body
  return typeof raw === 'string' ? JSON.parse(raw) : undefined
}

describe('发现页：GET /routes 的分页与筛选', () => {
  it('列表响应归一成分页模型，next_cursor 转成 nextCursor', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, {
        items: [
          { id: 'r-1', title: '太平街半日', theme_tags: ['历史'], duration_minutes: 180 },
          { id: 'r-2', title: '湘江夜游' },
        ],
        next_cursor: 'cursor-2',
      }),
    )

    const page = await endpoints.fetchRoutePage('q=太平街&limit=6')

    expect(urlOf()).toBe('/api/v1/routes?q=太平街&limit=6')
    expect(page.items.map((item) => item.title)).toEqual(['太平街半日', '湘江夜游'])
    expect(page.nextCursor).toBe('cursor-2')
  })

  it('空列表与缺字段都归一成空页，页面不会拿到 undefined', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))

    await expect(endpoints.fetchRoutePage('')).resolves.toEqual({ items: [], nextCursor: null })
    expect(urlOf()).toBe('/api/v1/routes')
  })
})

describe('路线详情与收藏', () => {
  it('详情按 days[].items[] 原样透出，收藏态读 is_favorited', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, {
        id: 'r-1',
        title: '太平街半日',
        is_favorited: true,
        days: [
          {
            day_index: 0,
            items: [
              {
                position: 1,
                start_time: '09:00',
                stay_minutes: 60,
                transport: 'walk',
                price_min: 0,
                price_max: 2000,
                place: { id: 'p-1', name: '贾谊故居', lat: 28.19, lon: 112.97 },
              },
            ],
          },
        ],
      }),
    )

    const detail = await endpoints.fetchRouteDetail('r-1')

    expect(urlOf()).toBe('/api/v1/routes/r-1')
    expect(detail.is_favorited).toBe(true)
    expect(detail.days?.[0]?.items?.[0]?.place?.name).toBe('贾谊故居')
  })

  it('收藏走 POST、取消走 DELETE，两个方向都是 204 无 body', async () => {
    fetchMock.mockResolvedValue(emptyResponse())

    await expect(endpoints.setRouteFavorite('r-1', true)).resolves.toBe(true)
    await expect(endpoints.setRouteFavorite('r-1', false)).resolves.toBe(false)

    expect(urlOf(0)).toBe('/api/v1/routes/r-1/favorite')
    expect(initOf(0).method).toBe('POST')
    expect(initOf(1).method).toBe('DELETE')
  })

  it('404 抛出的错误里保留后端的 message', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(404, { error: { code: 'ROUTE_NOT_FOUND', message: '路线不存在' } }),
    )

    const error = await endpoints.fetchRouteDetail('nope').catch((e: unknown) => e)
    expect(adapters.errorMessage(error, '兜底')).toBe('路线不存在')
  })
})

describe('评论区：列表 / 发表 / 置顶', () => {
  it('评论列表取 items，发表评论的请求体是 { body }', async () => {
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse(200, {
          items: [
            {
              id: 'c-1',
              route_id: 'r-1',
              body: '讲解很细',
              pinned: true,
              author: { id: 'u-1', username: '小周' },
              created_at: '2026-10-01T02:00:00Z',
            },
          ],
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(201, { id: 'c-2', body: '补充一条', author: { username: '我' } }),
      )

    const comments = await endpoints.fetchRouteComments('r-1')
    expect(comments[0].pinned).toBe(true)

    const created = await endpoints.createRouteComment('r-1', '补充一条')
    expect(created.id).toBe('c-2')
    expect(urlOf(1)).toBe('/api/v1/routes/r-1/comments')
    expect(initOf(1).method).toBe('POST')
    expect(bodyOf(1)).toEqual({ body: '补充一条' })
  })

  it('置顶走 POST /comments/{id}/pin，返回更新后的评论', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { id: 'c-1', pinned: true }))

    const pinned = await endpoints.pinRouteComment('c-1')

    expect(urlOf()).toBe('/api/v1/comments/c-1/pin')
    expect(pinned.pinned).toBe(true)
  })
})

describe('审核后台：队列 / 认领 / 裁决', () => {
  it('任务队列取 items，字段保持后端 snake_case', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, {
        items: [
          {
            id: 't-1',
            submission_id: 's-1',
            state: 'OPEN',
            assignee_id: null,
            lease_expires_at: null,
            completed_action: null,
            created_at: '2026-10-01T02:00:00Z',
          },
        ],
        next_cursor: null,
      }),
    )

    const tasks = await endpoints.fetchReviewTasks()

    expect(urlOf()).toBe('/api/v1/review/tasks')
    expect(tasks[0].state).toBe('OPEN')
    expect(tasks[0].submission_id).toBe('s-1')
  })

  it('认领返回任务本身', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { id: 't-1', state: 'CLAIMED' }))

    const task = await endpoints.claimReviewTask('t-1')

    expect(urlOf()).toBe('/api/v1/review/tasks/t-1/claim')
    expect(initOf().method).toBe('POST')
    expect(task.state).toBe('CLAIMED')
  })

  it('裁决把页面上的 note 翻译成后端的 reason，通过时省略 reason', async () => {
    fetchMock.mockImplementation(() => jsonResponse(200, { id: 't-1', state: 'COMPLETED' }))

    await endpoints.decideReviewTask('t-1', 'request_changes', '文化说明缺来源，补两条官方资料')
    expect(bodyOf()).toEqual({
      action: 'request_changes',
      reason: '文化说明缺来源，补两条官方资料',
    })

    await endpoints.decideReviewTask('t-1', 'approve', '')
    expect(bodyOf(1)).toEqual({ action: 'approve' })
  })

  it('提交详情取 snapshot / previous_snapshot / author', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, {
        id: 's-1',
        state: 'PENDING_REVIEW',
        snapshot: { title: '新版本', content: '新的文化说明' },
        previous_snapshot: { title: '旧版本', content: '旧的文化说明' },
        author: { id: 'u-2', username: '小周' },
      }),
    )

    const detail = await endpoints.fetchSubmission('s-1')

    expect(urlOf()).toBe('/api/v1/submissions/s-1')
    expect(detail.author?.username).toBe('小周')
  })
})

describe('提交页：送审与撤回', () => {
  it('送审 POST 到 /route-drafts/{id}/submit，没备注就不带 note', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(201, { id: 'sub-1', state: 'PENDING_REVIEW', note: null }),
    )

    const submission = await endpoints.submitDraft('d-1')

    expect(urlOf()).toBe('/api/v1/route-drafts/d-1/submit')
    expect(initOf().method).toBe('POST')
    expect(bodyOf()).toEqual({})
    expect(submission.state).toBe('PENDING_REVIEW')
  })

  it('撤回返回回到 EDITING 的草稿', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { id: 'd-1', state: 'EDITING', lock_version: 2 }))

    const draft = await endpoints.withdrawDraft('d-1')

    expect(urlOf()).toBe('/api/v1/route-drafts/d-1/withdraw')
    expect(draft.state).toBe('EDITING')
  })
})

describe('我的主页：通知已读与个人聚合', () => {
  it('已读接口 204 无 body，本地补 read_at 之后未读样式才会消失', async () => {
    fetchMock.mockResolvedValue(emptyResponse())

    const updated = await endpoints.markNotificationRead({ id: 'n-1', read_at: null })

    expect(urlOf()).toBe('/api/v1/notifications/n-1/read')
    expect(initOf().method).toBe('POST')
    expect(updated.read_at).toBeTruthy()
  })

  it('个人主页走真接口 GET /users/{id}/profile，并映射成视图模型', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, {
        user: { id: 'u-1', username: '小周', role: 'user', bio: '爱走老街' },
        published_routes: [
          {
            id: 'r-1',
            title: '老城文脉',
            summary: '',
            state: 'PUBLISHED',
            node_count: 3,
            favorite_count: 0,
          },
        ],
        comment_count: 2,
      }),
    )

    const profile = await endpoints.fetchUserProfile('u-1')

    expect(urlOf()).toBe('/api/v1/users/u-1/profile')
    expect(profile.user.username).toBe('小周')
    expect(profile.published_routes).toHaveLength(1)
  })
})

describe('创作页：草稿节点保存的乐观锁', () => {
  it('PUT /route-drafts/{id}/nodes 透传当前 lock_version 到 If-Match', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, { id: 'd-1', state: 'EDITING', lock_version: 8 }),
    )

    const detail = await endpoints.replaceDraftNodes(
      'd-1',
      [{ place_id: 'p-1', stay_minutes: 60, transport: 'walk', note: null, price_min: null, price_max: null }],
      7,
    )

    expect(urlOf()).toBe('/api/v1/route-drafts/d-1/nodes')
    expect(initOf().method).toBe('PUT')
    const headers = initOf().headers as Headers
    expect(headers.get('If-Match')).toBe('7')
    // 写操作仍要带幂等键：后端重放靠它
    expect(headers.get('Idempotency-Key')).toBeTruthy()
    expect(bodyOf()).toEqual({
      nodes: [
        { place_id: 'p-1', stay_minutes: 60, transport: 'walk', note: null, price_min: null, price_max: null },
      ],
    })
    expect(detail.lock_version).toBe(8)
  })

  it('版本落后时后端回 412 STALE_DRAFT_VERSION，页面拿到的是 ApiError 而不是 undefined', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(412, {
        error: { code: 'STALE_DRAFT_VERSION', message: '草稿已被修改，请刷新后重试' },
      }),
    )

    await expect(endpoints.replaceDraftNodes('d-1', [], 3)).rejects.toMatchObject({
      status: 412,
      code: 'STALE_DRAFT_VERSION',
    })
  })

  it('重新拉草稿详情走 GET /route-drafts/{id}', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, { id: 'd-1', state: 'EDITING', lock_version: 9, nodes: [] }),
    )

    const detail = await endpoints.fetchDraftDetail('d-1')

    expect(urlOf()).toBe('/api/v1/route-drafts/d-1')
    expect(detail.lock_version).toBe(9)
  })
})
