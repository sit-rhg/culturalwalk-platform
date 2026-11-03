/**
 * 真接口模式（VITE_USE_MOCK=false）下的页面冒烟。
 *
 * 目的：切真之后页面不能白屏，也不能再挂着「演示数据」提示。
 * 这里只渲染第一帧（SSR 不跑 onMounted），所以断言集中在标题与静态区块上；
 * 数据路径由 `endpoints.spec.ts` 用真实形状的假响应覆盖。
 */
import type { Component } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'

const NOTICE = '演示数据，接口未接入'

// 页面在 import 期读 localStorage（token / 用户），所以先备好后端登录后的样子
const store = new Map<string, string>([
  ['cw_token', 'mock-token:u-900'],
  ['cw_user', JSON.stringify({ id: 'u-900', username: '审核编辑', role: 'admin' })],
])
vi.stubGlobal('localStorage', {
  getItem: (key: string) => store.get(key) ?? null,
  setItem: (key: string, value: string) => void store.set(key, value),
  removeItem: (key: string) => void store.delete(key),
})
vi.stubEnv('VITE_USE_MOCK', 'false')

const Blank: Component = { render: () => null }

async function render(page: Component, path = '/'): Promise<string> {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      '/',
      '/login',
      '/submit',
      '/me',
      '/authoring',
      '/admin',
      '/routes/:id',
      '/submissions/:id',
    ].map((route) => ({ path: route, component: Blank })),
  })
  await router.push(path)
  await router.isReady()
  const app = createSSRApp(page)
  app.use(router)
  return renderToString(app)
}

describe('真接口模式：已接好的页面不再出现演示提示', () => {
  it('发现页：列表与筛选都走真接口', async () => {
    const { default: Browse } = await import('@/pages/browse/index.vue')
    const html = await render(Browse)

    expect(html).toContain('发现')
    expect(html).not.toContain(NOTICE)
    // 首帧在加载中，应该是骨架屏而不是空白或错误
    expect(html).toContain('skeleton')
  })

  it('路线详情页：详情、收藏、评论都走真接口', async () => {
    const { default: RouteDetail } = await import('@/pages/route-detail/index.vue')
    const html = await render(RouteDetail, '/routes/r-001')

    // 真接口模式下首屏是骨架屏，数据到位才渲染节点
    expect(html).toContain('route-detail')
    expect(html).not.toContain(NOTICE)
    expect(html).not.toContain('GET /routes/{id}')
  })

  it('提交页：草稿列表、送审、撤回都走真接口', async () => {
    const { default: Submit } = await import('@/pages/submit/index.vue')
    const html = await render(Submit, '/submit')

    expect(html).toContain('我的路线')
    expect(html).not.toContain(NOTICE)
    expect(html).not.toContain('POST /route-drafts')
  })

  it('审核后台：任务队列、领取、裁决都走真接口', async () => {
    const { default: Admin } = await import('@/pages/admin/index.vue')
    const html = await render(Admin, '/admin')

    // 已登录 admin 不该再看到权限拦截
    expect(html).not.toContain('需要审核权限')
    expect(html).toContain('审核通知')
    expect(html).not.toContain(NOTICE)
    expect(html).not.toContain('GET /review/tasks')
  })

  it('我的主页：通知已读与个人主页聚合都走真接口，不再挂演示提示', async () => {
    const { default: Me } = await import('@/pages/me/index.vue')
    const html = await render(Me, '/me')

    expect(html).toContain('已发布路线')
    expect(html).not.toContain(NOTICE)
    expect(html).not.toContain('GET /users/')
    expect(html).not.toContain('标记已读接口未接入')
  })
})
