/**
 * 接口落地清单（shared/capabilities.ts）的单元测试。
 *
 * 这张表决定页面走真接口还是演示数据，错了会让已接好的页面悄悄退回 mock，
 * 所以把「哪些还没实现」和「两种模式下 supports 的结果」都锁住。
 */
import { beforeAll, describe, expect, it, vi } from 'vitest'

type Caps = typeof import('@/shared/capabilities')

let real: Caps
let mocked: Caps

beforeAll(async () => {
  vi.stubEnv('VITE_USE_MOCK', 'false')
  vi.resetModules()
  real = await import('@/shared/capabilities')

  vi.stubEnv('VITE_USE_MOCK', 'true')
  vi.resetModules()
  mocked = await import('@/shared/capabilities')
})

describe('真接口模式：只有后端已实现的功能才走 api', () => {
  it('已接好的功能全部 supports 为 true', () => {
    expect(real.supports('reviewQueue')).toBe(true)
    expect(real.supports('routeList')).toBe(true)
    expect(real.supports('routeDetail')).toBe(true)
    expect(real.supports('comments')).toBe(true)
    expect(real.supports('draftSubmit')).toBe(true)
    expect(real.supports('notificationRead')).toBe(true)
    expect(real.supports('favorite')).toBe(true)
  })

  it('个人主页聚合已接真接口，清单里不再有等待中的端点', () => {
    expect(real.supports('profile')).toBe(true)
    expect(real.pendingEndpoint('profile')).toBeNull()
    expect(real.isDemo('profile')).toBe(false)
    // 所有功能都不该再挂着「演示数据」提示
    for (const f of ['profile', 'routeDetail', 'reviewQueue', 'notificationRead'] as const) {
      expect(real.isDemo(f)).toBe(false)
    }
  })
})

describe('mock 模式：所有页面都能演示', () => {
  it('mock 模式下全部功能 supports 为 true，页面照常拿演示数据', () => {
    expect(mocked.supports('profile')).toBe(true)
    expect(mocked.supports('reviewQueue')).toBe(true)
    // 演示提示由 isDemo 决定（跟着接口实现状态走），不跟运行模式走
    expect(mocked.isDemo('profile')).toBe(false)
  })
})
