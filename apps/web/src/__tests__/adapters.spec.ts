/**
 * 数据形状适配层（api/adapters.ts）的单元测试。
 *
 * 真接口是 snake_case + 一堆可选字段，页面只认这里吐出来的模型；
 * 字段名或兜底规则改错了，页面会静默显示空白，所以逐条锁住。
 */
import { describe, expect, it } from 'vitest'

import {
  applyNotificationRead,
  decisionPayload,
  errorMessage,
  favoriteState,
  flattenItems,
  toComments,
  toDrafts,
  toPage,
  toReviewTasks,
  unreadCount,
} from '@/api/adapters'
import { ApiError } from '@/shared/api'

describe('分页归一', () => {
  it('items / next_cursor 都可能是 undefined，统一成空数组与 null', () => {
    expect(toPage(undefined)).toEqual({ items: [], nextCursor: null })
    expect(toPage({})).toEqual({ items: [], nextCursor: null })
    expect(toPage({ items: [{ id: 'a' }], next_cursor: 'c1' })).toEqual({
      items: [{ id: 'a' }],
      nextCursor: 'c1',
    })
  })

  it('评论 / 审核任务 / 草稿列表都复用同一套归一规则', () => {
    expect(toComments(undefined)).toEqual([])
    expect(toReviewTasks({ items: [{ id: 't1' }] })).toHaveLength(1)
    expect(toDrafts({ items: [{ id: 'd1', title: '草稿', state: 'EDITING', lock_version: 1 }] })).toHaveLength(1)
  })
})

describe('路线节点抹平', () => {
  it('按 day_index 与 position 排序后拉平，地图和时间轴共用一份', () => {
    const detail = {
      id: 'r1',
      days: [
        {
          day_index: 1,
          items: [
            { position: 2, place: { id: 'p2', name: '第二站' } },
            { position: 1, place: { id: 'p1', name: '第一站' } },
          ],
        },
        { day_index: 0, items: [{ position: 1, place: { id: 'p0', name: '第零天' } }] },
      ],
    }

    const items = flattenItems(detail as never)

    expect(items.map((item) => item.place?.name)).toEqual(['第零天', '第一站', '第二站'])
  })

  it('没有 days 或 days 为空时返回空数组', () => {
    expect(flattenItems(null)).toEqual([])
    expect(flattenItems({} as never)).toEqual([])
  })
})

describe('裁决请求体', () => {
  it('note 映射成后端的 reason，并去掉首尾空白', () => {
    expect(decisionPayload('request_changes', '  文化说明缺来源  ')).toEqual({
      action: 'request_changes',
      reason: '文化说明缺来源',
    })
  })

  it('通过时即使写了备注也不带 reason（后端 approve 不收）', () => {
    expect(decisionPayload('approve', '内容无误，可以发布')).toEqual({ action: 'approve' })
  })

  it('理由为空时省略 reason', () => {
    expect(decisionPayload('request_changes', '   ')).toEqual({ action: 'request_changes' })
  })
})

describe('无 body 接口的本地状态', () => {
  it('收藏 204 之后本地翻转 is_favorited，不动其他字段', () => {
    const detail = { id: 'r1', title: '路线', is_favorited: false }
    expect(favoriteState(detail as never, true)).toEqual({
      id: 'r1',
      title: '路线',
      is_favorited: true,
    })
  })

  it('通知已读补 read_at，已有时间戳时不覆盖', () => {
    const now = new Date('2026-11-03T00:00:00Z')
    const read = applyNotificationRead({ id: 'n1', read_at: null }, now)
    expect(read.read_at).toBe('2026-11-03T00:00:00.000Z')

    const kept = applyNotificationRead({ id: 'n2', read_at: '2026-10-01T00:00:00Z' }, now)
    expect(kept.read_at).toBe('2026-10-01T00:00:00Z')
  })

  it('未读数只数没有 read_at 的条目', () => {
    expect(unreadCount([{ id: 'a' }, { id: 'b', read_at: null }, { id: 'c', read_at: 'x' }])).toBe(2)
  })
})

describe('失败提示', () => {
  it('ApiError 用后端信封里的 message', () => {
    const error = new ApiError(422, 'REVIEW_REASON_REQUIRED', '退回必须写理由')
    expect(errorMessage(error, '兜底文案')).toBe('退回必须写理由')
  })

  it('普通 Error 用它的 message，未知错误用页面兜底', () => {
    expect(errorMessage(new Error('网络连接失败，请检查网络后重试'), '兜底')).toBe(
      '网络连接失败，请检查网络后重试',
    )
    expect(errorMessage('boom', '加载失败，请重试')).toBe('加载失败，请重试')
  })

  it('缺 message 的 ApiError 退回兜底文案', () => {
    const error = new ApiError(500, 'INTERNAL', '')
    expect(errorMessage(error, '服务暂时不可用')).toBe('服务暂时不可用')
  })
})
