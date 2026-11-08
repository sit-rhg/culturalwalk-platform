import { describe, expect, it } from 'vitest'

import { MAX_INPUT_LINES, diffLines, snapshotToText } from '../diff'


describe('snapshotToText', () => {
  it('把快照翻成可读文本：标题/摘要/主题/时长/节点', () => {
    const text = snapshotToText({
      title: '山水洲城',
      summary: '从书院到老街',
      theme_tags: ['历史', '自然'],
      duration_minutes: 300,
      nodes: [
        { stay_minutes: 90, transport: null, note: '从后门出来就是爱晚亭' },
        { stay_minutes: 120, transport: 'metro', note: '' },
      ],
    })
    expect(text).toContain('标题：山水洲城')
    expect(text).toContain('主题：历史、自然')
    expect(text).toContain('时长：5 小时')
    expect(text).toContain(
      '1. 停留 90 分钟 · 步行 · 门票待核实 · 无配图｜从后门出来就是爱晚亭',
    )
    expect(text).toContain('2. 停留 120 分钟 · 地铁 · 门票待核实 · 无配图')
  })

  it('节点渲染地点名、价格区间与配图数量，换点/改价/换图才看得见', () => {
    const placeId = '11111111-2222-3333-4444-555555555555'
    const text = snapshotToText(
      {
        title: '山水洲城',
        nodes: [
          {
            place_id: placeId,
            stay_minutes: 90,
            transport: 'metro',
            price_min: 4500,
            price_max: 6000,
            media_asset_ids: ['a', 'b'],
          },
        ],
      },
      { [placeId]: '岳麓书院' },
    )
    expect(text).toContain('1. 地点 岳麓书院 · 停留 90 分钟 · 地铁 · 门票 ¥45-¥60 · 配图 2 张')
  })

  it('地名解析不到时退回短 id，仍能看出换了地点', () => {
    const text = snapshotToText({
      nodes: [{ place_id: 'abcdefgh-0000-0000-0000-000000000000', stay_minutes: 60 }],
    })
    expect(text).toContain('地点 abcdefgh')
  })

  it('只有一端价格时按起价/上限显示，不谎报免费', () => {
    expect(snapshotToText({ nodes: [{ price_min: 3000 }] })).toContain('门票 ¥30 起')
    expect(snapshotToText({ nodes: [{ price_max: 8000 }] })).toContain('门票 ≤¥80')
    expect(snapshotToText({ nodes: [{ price_min: 0, price_max: 0 }] })).toContain(
      '门票 ¥0-¥0',
    )
  })

  it('空快照给空字符串，不抛错', () => {
    expect(snapshotToText(null)).toBe('')
    expect(snapshotToText({})).toBe('')
  })
})

describe('diffLines 截断', () => {
  it('输入超过 MAX_INPUT_LINES 时标记 inputTruncated', () => {
    const before = Array.from({ length: MAX_INPUT_LINES + 10 }, (_, i) => 'old-' + i).join('\n')
    const after = Array.from({ length: MAX_INPUT_LINES + 10 }, (_, i) => 'new-' + i).join('\n')
    const result = diffLines(before, after)
    expect(result.inputTruncated).toBe(true)
  })

  it('短文本不算输入截断', () => {
    expect(diffLines('甲', '乙').inputTruncated).toBe(false)
  })
})
