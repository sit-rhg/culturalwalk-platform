/**
 * A3 路由守卫：未登录访问受保护页要带上原地址去登录，已登录不该再看到登录页，
 * 而且 redirect 只可能是站内路径（开放重定向必须被丢弃）。
 *
 * localStorage 会在 shared/session.ts 模块加载时被读成登录态，所以每个用例
 * 先铺好 localStorage 再 resetModules + 动态 import router，才能拿到对应的守卫行为。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory } from 'vue-router'

import { redirectTarget } from '@/pages/login/validation'

const USER_KEY = 'cw_user'
const LOGGED_IN_USER = JSON.stringify({ id: 'u-guard', username: 'guard', role: 'user' })

function stubStorage(seed: Record<string, string> = {}): void {
  const store = new Map(Object.entries(seed))
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
  })
}

async function freshRouter(seed: Record<string, string> = {}) {
  stubStorage(seed)
  vi.resetModules()
  const { createAppRouter } = await import('@/router')
  return createAppRouter(createMemoryHistory())
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.resetModules()
})

describe('路由守卫（A3）', () => {
  it('未登录访问受保护页 → /login?redirect=原地址', async () => {
    const router = await freshRouter()

    for (const path of ['/authoring', '/authoring/draft-1', '/submit', '/admin', '/me']) {
      await router.push(path)
      expect(router.currentRoute.value.path, path).toBe('/login')
      expect(router.currentRoute.value.query.redirect, path).toBe(path)
      // 登录页拿到 redirect 就该跳回原页，redirectTarget 不能是死代码
      expect(redirectTarget(router.currentRoute.value.query.redirect), path).toBe(path)
    }
  })

  it('已登录时访问 /login 直接回首页', async () => {
    const router = await freshRouter({ [USER_KEY]: LOGGED_IN_USER })

    await router.push('/login')
    expect(router.currentRoute.value.name).toBe('browse')
    expect(router.currentRoute.value.path).toBe('/')

    // 受保护页对已登录用户照常放行
    await router.push('/authoring')
    expect(router.currentRoute.value.path).toBe('/authoring')
  })

  it('恶意 redirect 被丢弃：外层地址 / 伪协议一律回首页', async () => {
    const router = await freshRouter()
    await router.push('/me?tab=drafts')

    // 守卫带出去的是站内地址（含 query），不是站外跳板
    const redirect = router.currentRoute.value.query.redirect
    expect(redirect).toBe('/me?tab=drafts')
    expect(redirectTarget(redirect)).toBe('/me?tab=drafts')

    for (const evil of ['//evil.example.com', 'https://evil.example.com', 'javascript:alert(1)', '/\\evil.example.com']) {
      expect(redirectTarget(evil), evil).toBe('/')
    }
  })
})
