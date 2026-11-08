/**
 * 审核任务的状态文案与提交前校验。纯函数，纯展示逻辑不放页面里。
 */

import type { Schemas } from '@/api/types'

type ReviewTask = Schemas['ReviewTask']
/**
 * 裁决动作：通过 / 退回修改 / 直接拒绝。
 * 后端三种都支持，拒绝是终态（草稿回到编辑状态，作者会收到通知）。
 */
export type ReviewDecision = 'approve' | 'request_changes' | 'reject'

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

/**
 * 只有「审核中」的任务能裁决：OPEN 没领取会 409 TASK_NOT_CLAIMED，
 * COMPLETED 已处理会 409 TASK_ALREADY_FINISHED。按钮不要在这种任务上出现。
 */
export function canDecide(task: ReviewTask | null | undefined): boolean {
  return task?.state === 'CLAIMED'
}

/**
 * 审核通过之后还要「发布」才会出现在发现页，这里给出入口。
 * publishedSubmissionIds 由页面在发布成功后追加：已经发过的再点必 409
 * ROUTE_ALREADY_PUBLISHED，按钮必须消失。
 */
export function canPublish(
  task: ReviewTask,
  publishedSubmissionIds: readonly string[] = [],
): boolean {
  if (task.state !== 'COMPLETED' || task.completed_action !== 'approve') return false
  const submissionId = task.submission_id
  return !submissionId || !publishedSubmissionIds.includes(submissionId)
}

/** 后端 DecisionRequest.reason 的 min_length，前端先拦，省掉一次 422。 */
export const MIN_REASON_LENGTH = 10

/**
 * 退回/拒绝必须写理由，且理由长度 1-9 字后端会 422；通过时理由可以留空。
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
  const label = action === 'reject' ? '拒绝' : '退回'
  return text.length >= MIN_REASON_LENGTH ? '' : `${label}必须写理由`
}
