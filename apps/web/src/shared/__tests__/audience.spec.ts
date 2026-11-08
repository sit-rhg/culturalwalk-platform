import { describe, expect, it } from 'vitest'
import type { components } from '@cw/contracts'
import { AUDIENCE_OPTIONS, audienceLabel } from '../audience'

type Audience = components['schemas']['Audience']

describe('audience 标签', () => {
  it('六个取值与契约枚举一一对应', () => {
    // 类型层面也锁住：任何一边新增/改名都会让这行编译不过。
    const values: Audience[] = AUDIENCE_OPTIONS.map((option) => option.value)
    expect(values).toEqual(['family', 'student', 'couple', 'senior', 'solo', 'group'])
  })

  it('未知取值原样回显，不抛错', () => {
    expect(audienceLabel('family')).toBe('亲子')
    expect(audienceLabel('unknown')).toBe('unknown')
  })
})
