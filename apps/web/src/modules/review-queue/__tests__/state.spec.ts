/**
 * 审核队列的展示状态与提交前校验（纯函数）。
 *
 * 「退回必须写理由」是产品硬规则：页面先拦一次（省一次注定 422 的请求），
 * 后端再用 REVIEW_REASON_REQUIRED 兜底。这里锁住页面这一层不被顺手改掉。
 */
import { describe, expect, it } from 'vitest'

import {
  canDecide,
  canPublish,
  decisionBlockedReason,
  isPendingTask,
  reviewTaskStateLabel,
} from '../state'

describe('审核任务状态文案', () => {
  it('四个状态都有中文文案', () => {
    expect(reviewTaskStateLabel('OPEN')).toBe('待领取')
    expect(reviewTaskStateLabel('CLAIMED')).toBe('审核中')
    expect(reviewTaskStateLabel('COMPLETED')).toBe('已完成')
    expect(reviewTaskStateLabel('CANCELLED')).toBe('已取消')
  })

  it('空值与未知状态不崩：空值给空串、未知状态原样显示', () => {
    expect(reviewTaskStateLabel(null)).toBe('')
    expect(reviewTaskStateLabel(undefined)).toBe('')
    expect(reviewTaskStateLabel('SOMETHING_NEW')).toBe('SOMETHING_NEW')
  })
})

describe('哪些任务还要人处理', () => {
  it('待领取与审核中算未处理完', () => {
    expect(isPendingTask({ state: 'OPEN' } as never)).toBe(true)
    expect(isPendingTask({ state: 'CLAIMED' } as never)).toBe(true)
  })

  it('已完成与已取消不再出现在待办里', () => {
    expect(isPendingTask({ state: 'COMPLETED' } as never)).toBe(false)
    expect(isPendingTask({ state: 'CANCELLED' } as never)).toBe(false)
  })
})

describe('退回必须写理由', () => {
  it('通过不需要理由', () => {
    expect(decisionBlockedReason('approve', '')).toBe('')
  })

  it('退回时只有空白字符也算没写', () => {
    expect(decisionBlockedReason('request_changes', '   ')).toBe('退回必须写理由')
    expect(decisionBlockedReason('request_changes', '')).toBe('退回必须写理由')
  })

  it('理由长度 1-9 字会被后端 422，页面先拦', () => {
    expect(decisionBlockedReason('request_changes', '文化说明缺来源')).toBe('理由至少写 10 个字')
    expect(decisionBlockedReason('approve', '再改改')).toBe('理由至少写 10 个字')
  })

  it('理由够 10 字就放行', () => {
    expect(decisionBlockedReason('request_changes', '文化说明缺来源，补两条官方资料')).toBe('')
    expect(decisionBlockedReason('approve', '内容无误，可以正式发布啦')).toBe('')
  })

  it('还没选动作时不拦（弹窗刚打开）', () => {
    expect(decisionBlockedReason(null, '')).toBe('')
  })

  it('拒绝也必须写理由，文案说清是拒绝而不是退回', () => {
    expect(decisionBlockedReason('reject', '')).toBe('拒绝必须写理由')
    expect(decisionBlockedReason('reject', '内容与长沙无关，不予收录')).toBe('')
  })
})

describe('哪些任务能点裁决', () => {
  it('只有审核中能裁决：OPEN 会 409 未领取，COMPLETED 会 409 已结束', () => {
    expect(canDecide({ state: 'CLAIMED' } as never)).toBe(true)
    expect(canDecide({ state: 'OPEN' } as never)).toBe(false)
    expect(canDecide({ state: 'COMPLETED' } as never)).toBe(false)
    expect(canDecide(null)).toBe(false)
  })
})

describe('发布按钮什么时候出现', () => {
  const approved = {
    state: 'COMPLETED',
    completed_action: 'approve',
    submission_id: 'sub-1',
  } as never

  it('通过后出现，发布过就消失（再点必 409）', () => {
    expect(canPublish(approved)).toBe(true)
    expect(canPublish(approved, ['sub-1'])).toBe(false)
  })

  it('退回、未完成、没提交号都不出现', () => {
    expect(
      canPublish({ state: 'COMPLETED', completed_action: 'request_changes' } as never),
    ).toBe(false)
    expect(canPublish({ state: 'CLAIMED', completed_action: null } as never)).toBe(false)
  })
})
