/**
 * 真接口模式（VITE_USE_MOCK=false）下的页面冒烟。
 *
 * 目的：接口没实现的页面切到真接口后不能白屏，也要明确告诉用户这是演示数据。
 * 这里只渲染第一帧（SSR 不跑 onMounted），所以断言集中在标题、演示提示和静态区块上。
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

describe('真接口模式：未实现接口的页面保留演示数据并给出提示', () => {
  it('发现页：路线列表未接，顶部有灰字提示', async () => {
    const { default: Browse } = await import('@/pages/browse/index.vue')
    const html = await render(Browse)

    expect(html).toContain(NOTICE)
    expect(html).toContain('GET /routes')
  })

  it('路线详情页：详情与收藏都未接，提示里写清等待的接口', async () => {
    const { default: RouteDetail } = await import('@/pages/route-detail/index.vue')
    const html = await render(RouteDetail, '/routes/r-001')

    expect(html).toContain(NOTICE)
    expect(html).toContain('GET /routes/{id}')
  })

  it('提交页：草稿列表走真接口，送审动作还是演示', async () => {
    const { default: Submit } = await import('@/pages/submit/index.vue')
    const html = await render(Submit, '/submit')

    expect(html).toContain('我的路线')
    expect(html).toContain(NOTICE)
    expect(html).toContain('POST /route-drafts/{id}/submit')
  })

  it('审核后台：审核通知走真接口，任务队列仍是演示', async () => {
    const { default: Admin } = await import('@/pages/admin/index.vue')
    const html = await render(Admin, '/admin')

    // 已登录 admin 不该再看到权限拦截
    expect(html).not.toContain('需要审核权限')
    expect(html).toContain('审核通知')
    expect(html).toContain(NOTICE)
    expect(html).toContain('GET /review/tasks')
  })

  it('我的主页：账号与通知走真接口，主页聚合还是演示', async () => {
    const { default: Me } = await import('@/pages/me/index.vue')
    const html = await render(Me, '/me')

    expect(html).toContain('已发布路线')
    expect(html).toContain(NOTICE)
    expect(html).toContain('GET /users/')
  })
})
