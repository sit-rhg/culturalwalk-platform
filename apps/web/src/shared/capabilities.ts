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
  // ID-04：个人主页的已发布路线与统计
  profile: 'GET /users/{user_id}/profile',
  // moderation 模块目前只有空 router，审核列表/认领/裁决都还没实现
  reviewQueue: 'GET /review/tasks（含认领与裁决）',
  // routes 模块只有草稿，浏览所需的路线列表/详情接口还没实现
  routeList: 'GET /routes',
  routeDetail: 'GET /routes/{id}',
  favorite: 'POST /routes/{id}/favorite',
  // 评论区跟随路线详情一起做
  comments: 'GET/POST /routes/{id}/comments',
  // routes 模块的草稿 CRUD 已实现，但送审动作还没有
  draftSubmit: 'POST /route-drafts/{id}/submit',
  // notifications 目前只有列表，没有标记已读
  notificationRead: 'POST /notifications/{id}/read',
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
