/**
 * 审核任务的状态文案与提交前校验。纯函数，纯展示逻辑不放页面里。
 */

import type { Schemas } from '@/api/types'

type ReviewTask = Schemas['ReviewTask']
export type ReviewDecision = 'approve' | 'request_changes'

const STATE_LABEL: Record<string, string> = {
  OPEN: '待领取',
  CLAIMED: '审核中',
  COMPLETED: '已完成',
  CANCELLED: '已取消',
}

export function reviewTaskStateLabel(state: string | null | undefined): string {
  if (!state) return ''
  return STATE_LABEL[state] ?? state
}

/** 还要人处理的任务：待领取 + 审核中 */
export function isPendingTask(task: ReviewTask): boolean {
  return task.state === 'OPEN' || task.state === 'CLAIMED'
}

/** 后端 DecisionRequest.reason 的 min_length，前端先拦，省掉一次 422。 */
export const MIN_REASON_LENGTH = 10

/**
 * 退回必须写理由，且理由长度 1-9 字后端会 422；通过时理由可以留空。
 * 这里先拦住，省掉一次注定失败的请求。
 */
export function decisionBlockedReason(
  action: ReviewDecision | null,
  note: string,
): string {
  const text = note.trim()
  if (!action) return ''
  if (text.length > 0 && text.length < MIN_REASON_LENGTH) {
    return `理由至少写 ${MIN_REASON_LENGTH} 个字`
  }
  if (action === 'approve') return ''
  return text.length >= MIN_REASON_LENGTH ? '' : '退回必须写理由'
}
