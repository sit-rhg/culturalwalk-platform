import { describe, expect, it } from 'vitest'

import { demoAccounts } from '@/pages/login/validation'

describe('演示账号：跟着数据源走', () => {
  it('假数据模式给 mock 账号（密码 cw123456）', () => {
    const list = demoAccounts(true)
    expect(list[0]?.username).toBe('文化漫步者')
    expect(list.every((a) => a.password === 'cw123456')).toBe(true)
  })

  it('真接口模式给种子账号（demo@demo.local / Demo12345!）', () => {
    const list = demoAccounts(false)
    expect(list.map((a) => a.username)).toEqual([
      'demo@demo.local',
      'reviewer@demo.local',
      'editor@demo.local',
    ])
    expect(list.every((a) => a.password === 'Demo12345!')).toBe(true)
  })
})
