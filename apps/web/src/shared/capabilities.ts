/**
 * 后端接口落地情况。
 *
 * 页面据此决定数据来源：
 * - 已实现 → api.get/post（真接口）
 * - 未实现 → api.mock.* + 页面顶部 DemoNotice 演示提示（mock 时照常能演示）
 *
 * 后端补上端点后，把对应 PENDING 值改成 null，页面自动切到真接口，其余代码不用动。
 * 这份清单要和 services/api/app/modules 下各模块的 router.py 保持对齐。
 */

import { USE_MOCK } from './api'

export type Feature =
  | 'profile'
  | 'reviewQueue'
  | 'routeList'
  | 'routeDetail'
  | 'favorite'
  | 'comments'
  | 'draftSubmit'
  | 'notificationRead'

/** null = 已实现；字符串 = 还在等这个接口（注释里写清楚等哪个） */
const PENDING: Record<Feature, string | null> = {
  // identity/router.py 里这个端点整段被注释掉了，请求会 404，先留演示数据兜底
  profile: 'GET /users/{user_id}/profile',
  reviewQueue: null,
  routeList: null,
  routeDetail: null,
  favorite: null,
  comments: null,
  draftSubmit: null,
  notificationRead: null,
}

export function pendingEndpoint(feature: Feature): string | null {
  return PENDING[feature]
}

/**
 * 该功能能否走真接口。mock 模式下所有页面都能演示，所以一律 true；
 * 真接口模式下只有后端已实现的才 true。
 */
export function supports(feature: Feature): boolean {
  return USE_MOCK || PENDING[feature] === null
}

/** 页面上是否需要挂「演示数据」提示：只要接口没实现就要挂，和当前模式无关 */
export function isDemo(feature: Feature): boolean {
  return PENDING[feature] !== null
}
