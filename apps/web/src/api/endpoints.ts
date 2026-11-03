/**
 * 页面唯一的请求入口：路径、方法、请求体与响应形状都在这里对齐，
 * 页面只拿视图模型，不再自己拼 URL 或读 snake_case。
 */
import { api } from '@/shared/api'
import type { Schemas } from '@/api/types'

import {
  type Comment,
  type Page,
  type RouteDetail,
  type RouteDraft,
  type RouteSummary,
  type ReviewTask,
  type Submission,
  type SubmissionDetail,
  type UserProfile,
  type DecisionAction,
  applyNotificationRead,
  decisionPayload,
  toComments,
  toDrafts,
  toPage,
  toReviewTasks,
} from './adapters'

type RawPage<T> = { items?: T[]; next_cursor?: string | null }

const seg = (value: string): string => encodeURIComponent(value)

/** 发现页：query 由页面用 buildRouteQuery 生成，这里只负责归一成分页模型。 */
export async function fetchRoutePage(query: string): Promise<Page<RouteSummary>> {
  const suffix = query ? `?${query}` : ''
  return toPage(await api.get<RawPage<RouteSummary>>(`/routes${suffix}`))
}

export async function fetchRouteDetail(routeId: string): Promise<RouteDetail> {
  return api.get<RouteDetail>(`/routes/${seg(routeId)}`)
}

/** 收藏 / 取消收藏：后端两个方向都返回 204，没有 body，状态只能在本地翻转。 */
export async function setRouteFavorite(routeId: string, favorited: boolean): Promise<boolean> {
  if (favorited) await api.post<void>(`/routes/${seg(routeId)}/favorite`)
  else await api.del<void>(`/routes/${seg(routeId)}/favorite`)
  return favorited
}

export async function fetchRouteComments(routeId: string): Promise<Comment[]> {
  return toComments(await api.get<RawPage<Comment>>(`/routes/${seg(routeId)}/comments`))
}

export async function createRouteComment(routeId: string, body: string): Promise<Comment> {
  return api.post<Comment>(`/routes/${seg(routeId)}/comments`, { body })
}

export async function pinRouteComment(commentId: string): Promise<Comment> {
  return api.post<Comment>(`/comments/${seg(commentId)}/pin`)
}

export async function fetchReviewTasks(): Promise<ReviewTask[]> {
  return toReviewTasks(await api.get<RawPage<ReviewTask>>('/review/tasks'))
}

export async function claimReviewTask(taskId: string): Promise<ReviewTask> {
  return api.post<ReviewTask>(`/review/tasks/${seg(taskId)}/claim`)
}

export async function decideReviewTask(
  taskId: string,
  action: DecisionAction,
  note: string,
): Promise<ReviewTask> {
  return api.post<ReviewTask>(`/review/tasks/${seg(taskId)}/decision`, decisionPayload(action, note))
}

export async function fetchSubmission(submissionId: string): Promise<SubmissionDetail> {
  return api.get<SubmissionDetail>(`/submissions/${seg(submissionId)}`)
}

/**
 * 发布：审核通过之后由编辑/管理员把这一版上线。
 * 契约要求同时提交版本号与内容 hash —— 「发哪一版、内容是什么」两件事都要对上。
 */
export async function publishSubmission(
  submissionId: string,
  version: { version_id: string; content_hash: string },
): Promise<Schemas['Publication']> {
  return api.post<Schemas['Publication']>(`/submissions/${seg(submissionId)}/publish`, {
    version_id: version.version_id,
    content_hash: version.content_hash,
  })
}

export async function fetchMyDrafts(): Promise<RouteDraft[]> {
  return toDrafts(await api.get<RawPage<RouteDraft>>('/route-drafts'))
}

export async function submitDraft(draftId: string, note?: string): Promise<Submission> {
  const body = note?.trim() ? { note: note.trim() } : {}
  return api.post<Submission>(`/route-drafts/${seg(draftId)}/submit`, body)
}

export async function withdrawDraft(draftId: string): Promise<RouteDraft> {
  return api.post<RouteDraft>(`/route-drafts/${seg(draftId)}/withdraw`)
}

/** 通知已读：204 无 body，返回值只是把本地那份补上 read_at。 */
export async function markNotificationRead(notification: { id?: string; read_at?: string | null }) {
  if (!notification.id) return notification
  await api.post<void>(`/notifications/${seg(notification.id)}/read`)
  return applyNotificationRead(notification)
}

/** 用户主页：后端 `identity/router.py` 的端点被注释掉了（404），暂时仍由演示数据兜底。 */
export async function fetchUserProfile(userId: string): Promise<UserProfile> {
  return api.get<UserProfile>(`/users/${seg(userId)}/profile`)
}
