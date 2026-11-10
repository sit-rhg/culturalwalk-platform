import type { RouteRecordRaw, RouterHistory } from 'vue-router'
import { createRouter, createWebHistory } from 'vue-router'

import type { NavKey } from '@/shared/nav'
import { isLoggedIn } from '@/shared/session'

declare module 'vue-router' {
  interface RouteMeta {
    /** 这条路由点亮底部导航的哪个 tab；不设 = 不点亮任何 tab */
    nav?: NavKey
    /** 需要登录：未登录先去 /login，登录页按 redirect 回到这里 */
    requiresAuth?: boolean
  }
}

// 页面按目录一一对应 docs/00-总体开发文档.md §9 的 pages/ 列表
const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'browse',
    component: () => import('@/pages/browse/index.vue'),
    meta: { nav: 'browse' },
  },
  {
    path: '/routes/:id',
    name: 'route-detail',
    component: () => import('@/pages/route-detail/index.vue'),
    // 从列表点进来的详情页，底部导航仍然停在「发现」
    meta: { nav: 'browse' },
  },
  {
    path: '/planner',
    name: 'planner',
    component: () => import('@/pages/planner/index.vue'),
    meta: { nav: 'planner' },
  },
  {
    path: '/authoring',
    name: 'authoring',
    component: () => import('@/pages/authoring/index.vue'),
    meta: { nav: 'authoring', requiresAuth: true },
  },
  {
    path: '/authoring/:id',
    name: 'authoring-edit',
    component: () => import('@/pages/authoring/index.vue'),
    meta: { nav: 'authoring', requiresAuth: true },
  },
  {
    path: '/submit',
    name: 'submit',
    component: () => import('@/pages/submit/index.vue'),
    meta: { nav: 'submit', requiresAuth: true },
  },
  {
    path: '/me',
    name: 'me',
    component: () => import('@/pages/me/index.vue'),
    meta: { nav: 'me', requiresAuth: true },
  },
  // 登录页与后台不入主导航，所以不设 meta.nav
  { path: '/login', name: 'login', component: () => import('@/pages/login/index.vue') },
  {
    path: '/admin',
    name: 'admin',
    component: () => import('@/pages/admin/index.vue'),
    meta: { requiresAuth: true },
  },
  // 未知地址回首页：H5 上刷新错地址不该白屏
  { path: '/:pathMatch(.*)*', name: 'not-found', redirect: '/' },
]

/**
 * 路由器工厂：运行时用浏览器 history，测试传内存 history。
 * 这里不在模块顶层创建浏览器 history，否则在 Node 里 import 本文件（跑测试）会报 window is not defined。
 *
 * guarded=false 只给「路由表 + 导航高亮」这类不关心登录态的测试：它们要看的是每条地址渲染出哪一页，
 * 被守卫踢到登录页反而测不到目标。守卫本身在 __tests__/guard.spec.ts 里单独测。
 */
export function createAppRouter(
  history: RouterHistory = createWebHistory(),
  { guarded = true }: { guarded?: boolean } = {},
) {
  const router = createRouter({
    history,
    routes,
    scrollBehavior: () => ({ top: 0 }),
  })
  if (!guarded) return router

  router.beforeEach((to) => {
    // 未登录访问受保护页：带上原地址（含 query），登录后由 redirectTarget 校验再回跳
    if (to.meta.requiresAuth && !isLoggedIn.value) {
      return { path: '/login', query: { redirect: to.fullPath } }
    }
    // 已登录还去登录页没意义，直接回首页
    if (to.name === 'login' && isLoggedIn.value) {
      return { path: '/' }
    }
    return true
  })
  return router
}
