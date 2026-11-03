import { describe, expect, it } from 'vitest'

import { snapshotToText } from '../diff'


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
    expect(text).toContain('1. 停留 90 分钟 · 步行｜从后门出来就是爱晚亭')
    expect(text).toContain('2. 停留 120 分钟 · 地铁')
  })

  it('空快照给空字符串，不抛错', () => {
    expect(snapshotToText(null)).toBe('')
    expect(snapshotToText({})).toBe('')
  })
})
