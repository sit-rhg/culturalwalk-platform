/**
 * 数据形状适配层：真接口返回的是 snake_case + 一堆可选字段，
 * 页面只认这里吐出来的视图模型。所有「后端字段 → 页面字段」的转换只许放在本文件，
 * 页面里不要再出现 `res.items ?? []` 或 `{ action, note }` 这类就地拼接。
 */

import { ApiError } from '@/shared/api'

import type { Schemas } from './types'

export type RouteSummary = Schemas['RouteSummary']
export type RouteDetail = Schemas['RouteDetail']
export type RouteDay = NonNullable<RouteDetail['days']>[number]
export type RouteDayItem = NonNullable<RouteDay['items']>[number]
export type Comment = Schemas['Comment']
export type ReviewTask = Schemas['ReviewTask']
export type Submission = Schemas['Submission']
export type SubmissionDetail = Schemas['SubmissionDetail']
export type RouteDraft = Schemas['RouteDraft']
export type RouteDraftDetail = Schemas['RouteDraftDetail']
export type Notification = Schemas['Notification']
export type UserProfile = Schemas['UserProfile']

/** 分页响应归一：真接口的 items / next_cursor 都是可选的。 */
export interface Page<T> {
  items: T[]
  nextCursor: string | null
}

export function toPage<T>(raw: { items?: T[]; next_cursor?: string | null } | null | undefined): Page<T> {
  return {
    items: raw?.items ?? [],
    nextCursor: raw?.next_cursor ?? null,
  }
}

/** 页面失败提示统一取后端的错误信封 message，兜底用页面自己的中文文案。 */
export function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.message) return error.message
  if (error instanceof Error && error.message && !/^HTTP \d+$/.test(error.message)) return error.message
  return fallback
}

/**
 * 草稿乐观锁失效：后端在 If-Match 落后时回 412 STALE_DRAFT_VERSION。
 * 这是「别处刚存过」而不是「你填错了」，页面要重新拉一次草稿再让用户改。
 */
export function isStaleDraftVersion(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 412 || error.code === 'STALE_DRAFT_VERSION')
}

export function routeTitle(route: RouteSummary): string {
  return route.title?.trim() || '未命名路线'
}

/** 路线详情里的节点通通抹平成一个数组，地图与时间轴都用它。 */
export function flattenItems(detail: RouteDetail | null): RouteDayItem[] {
  if (!detail?.days) return []
  return detail.days
    .slice()
    .sort((a, b) => (a.day_index ?? 0) - (b.day_index ?? 0))
    .flatMap((day) => (day.items ?? []).slice().sort((a, b) => (a.position ?? 0) - (b.position ?? 0)))
}

export function toComments(raw: { items?: Comment[]; next_cursor?: string | null } | null | undefined): Comment[] {
  return toPage(raw).items
}

export function toReviewTasks(
  raw: { items?: ReviewTask[]; next_cursor?: string | null } | null | undefined,
): ReviewTask[] {
  return toPage(raw).items
}

export function toDrafts(raw: { items?: RouteDraft[] } | null | undefined): RouteDraft[] {
  return toPage(raw).items
}

/** 裁决请求体：后端字段叫 reason，且有 10 字下限；approve 不带理由。 */
export type DecisionAction = 'approve' | 'request_changes' | 'reject'

export function decisionPayload(
  action: DecisionAction,
  note: string,
): { action: DecisionAction; reason?: string } {
  const reason = note.trim()
  if (action === 'approve' || !reason) return { action }
  return { action, reason }
}

/** 收藏接口是 204 无 body，状态只能在本地翻转。 */
export function favoriteState(detail: RouteDetail, next: boolean): RouteDetail {
  return { ...detail, is_favorited: next }
}

/** 通知已读接口同样无 body，补一个本地 read_at 让「未读」样式立刻消失。 */
export function applyNotificationRead(
  item: Notification,
  now: Date = new Date(),
): Notification {
  return { ...item, read_at: item.read_at ?? now.toISOString() }
}

export function unreadCount(items: Notification[]): number {
  return items.filter((item) => !item.read_at).length
}
