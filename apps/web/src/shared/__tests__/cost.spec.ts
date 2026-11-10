import { describe, expect, it } from 'vitest'

import { costText } from '@/shared/format'

describe('costText：费用怎么说', () => {
  it('没核过价：说待核实，不说 ¥0', () => {
    expect(costText({ min_total: 0, max_total: 0, unknown_count: 3 })).toBe('费用待核实')
  })

  it('只有起价：说「起」，不把已知价格藏成待核实', () => {
    expect(costText({ min_total: 4500, unknown_count: 2 })).toBe('¥45 起（2 个节点待核价）')
    expect(costText({ min_total: 4500, unknown_count: 0 })).toBe('¥45 起')
    expect(costText({ max_total: 12000, unknown_count: 0 })).toBe('≤¥120')
  })

  it('全核过价：只给区间', () => {
    expect(costText({ min_total: 4500, max_total: 6000, unknown_count: 0 })).toBe('¥45-¥60')
  })

  it('部分核过价：带上还有几个节点没核', () => {
    expect(costText({ min_total: 200, max_total: 200, unknown_count: 2 })).toBe(
      '¥2（2 个节点待核价）',
    )
  })

  it('完全没有费用对象：说未估', () => {
    expect(costText(undefined)).toBe('费用未估')
    expect(costText(null)).toBe('费用未估')
  })
})
