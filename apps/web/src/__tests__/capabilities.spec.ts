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
  it('待实现功能 supports 为 false，并给出等待的接口', () => {
    expect(real.supports('profile')).toBe(false)
    expect(real.supports('reviewQueue')).toBe(false)
    expect(real.supports('routeList')).toBe(false)
    expect(real.supports('routeDetail')).toBe(false)
    expect(real.supports('comments')).toBe(false)
    expect(real.supports('draftSubmit')).toBe(false)
    expect(real.supports('notificationRead')).toBe(false)
    expect(real.supports('favorite')).toBe(false)
  })

  it('提示里带具体接口和路径参数，方便对照后端进度', () => {
    expect(real.pendingEndpoint('profile')).toContain('GET /users/')
    expect(real.pendingEndpoint('reviewQueue')).toContain('GET /review/tasks')
    expect(real.pendingEndpoint('draftSubmit')).toBe('POST /route-drafts/{id}/submit')
    expect(real.isDemo('routeDetail')).toBe(true)
  })
})

describe('mock 模式：所有页面都能演示', () => {
  it('未实现功能在 mock 下 supports 为 true，页面照常拿演示数据', () => {
    expect(mocked.supports('profile')).toBe(true)
    expect(mocked.supports('reviewQueue')).toBe(true)
    // 演示提示仍然要在：它描述的是「接口没接」，和当前跑在哪种模式无关
    expect(mocked.isDemo('profile')).toBe(true)
  })
})
