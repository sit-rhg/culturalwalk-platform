import { describe, expect, it } from 'vitest'

import { moneyRange } from '@/shared/format'

describe('moneyRange 缺一侧的边界', () => {
  it('只有下限说「起」，不是一口价', () => {
    expect(moneyRange(4500, null)).toBe('¥45 起')
  })

  it('只有上限说「≤」，不是一口价', () => {
    expect(moneyRange(null, 12000)).toBe('≤¥120')
  })

  it('两端都有就显示区间', () => {
    expect(moneyRange(4500, 12000)).toBe('¥45-¥120')
    expect(moneyRange(4500, 4500)).toBe('¥45')
  })

  it('两端都没有才是价格待核实', () => {
    expect(moneyRange(null, null)).toBe('价格待核实')
    expect(moneyRange(undefined, undefined)).toBe('价格待核实')
  })
})
