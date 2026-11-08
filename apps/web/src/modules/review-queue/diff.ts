/**
 * 审核 diff：把两段文本按行做 LCS 对齐，产出逐行差异。
 * 纯函数，便于单测；页面和组件只负责渲染。
 */

export type DiffKind = 'same' | 'add' | 'del'

export interface DiffOp {
  /** same 表示两侧都有；add 只有右栏，del 只有左栏 */
  kind: DiffKind
  text: string
  leftNo: number | null
  rightNo: number | null
}

export interface DiffResult {
  /** 已按 limit 截断的差异行 */
  ops: DiffOp[]
  /** 截断前的总行数，用于提示 */
  totalOps: number
  truncated: boolean
  /** 输入超过 MAX_INPUT_LINES 时为 true：只对比了前 400 行，尾部改动可能看不见 */
  inputTruncated: boolean
}

export interface DiffRow {
  key: string
  kind: DiffKind
  left: { no: number; text: string } | null
  right: { no: number; text: string } | null
}

/** 一屏最多画多少行，再多也看不清 */
export const MAX_DIFF_LINES = 200

/** LCS 是 O(n×m)，超长文本先砍掉，别把主线程算死 */
export const MAX_INPUT_LINES = 400

export function splitLines(text: string | null | undefined): string[] {
  if (!text) return []
  const lines = text.replace(/\r\n?/g, '\n').split('\n')
  // 末尾的换行不算一行空内容
  while (lines.length > 0 && lines[lines.length - 1] === '') lines.pop()
  return lines
}

export function diffLines(
  before: string | null | undefined,
  after: string | null | undefined,
  limit: number = MAX_DIFF_LINES,
): DiffResult {
  const leftLines = splitLines(before)
  const rightLines = splitLines(after)
  const inputTruncated =
    leftLines.length > MAX_INPUT_LINES || rightLines.length > MAX_INPUT_LINES
  const left = leftLines.slice(0, MAX_INPUT_LINES)
  const right = rightLines.slice(0, MAX_INPUT_LINES)
  const table = buildLcsTable(left, right)

  const ops: DiffOp[] = []
  let i = 0
  let j = 0
  while (i < left.length && j < right.length) {
    if (left[i] === right[j]) {
      ops.push({ kind: 'same', text: left[i], leftNo: i + 1, rightNo: j + 1 })
      i += 1
      j += 1
    } else if (table[i + 1][j] >= table[i][j + 1]) {
      ops.push({ kind: 'del', text: left[i], leftNo: i + 1, rightNo: null })
      i += 1
    } else {
      ops.push({ kind: 'add', text: right[j], leftNo: null, rightNo: j + 1 })
      j += 1
    }
  }
  while (i < left.length) {
    ops.push({ kind: 'del', text: left[i], leftNo: i + 1, rightNo: null })
    i += 1
  }
  while (j < right.length) {
    ops.push({ kind: 'add', text: right[j], leftNo: null, rightNo: j + 1 })
    j += 1
  }

  // 「差异共 N 行」只该数增删，把未改的 same 也算进去会吓人一跳。
  const changed = ops.filter((op) => op.kind !== 'same').length
  return {
    ops: ops.slice(0, limit),
    totalOps: changed,
    truncated: ops.length > limit,
    inputTruncated,
  }
}

function buildLcsTable(left: string[], right: string[]): number[][] {
  const table: number[][] = Array.from({ length: left.length + 1 }, () =>
    new Array<number>(right.length + 1).fill(0),
  )
  for (let i = left.length - 1; i >= 0; i -= 1) {
    for (let j = right.length - 1; j >= 0; j -= 1) {
      table[i][j] =
        left[i] === right[j]
          ? table[i + 1][j + 1] + 1
          : Math.max(table[i + 1][j], table[i][j + 1])
    }
  }
  return table
}

/** 转成两栏对照的行：删除只落左栏，新增只落右栏 */
export function toDiffRows(ops: DiffOp[]): DiffRow[] {
  return ops.map((op, index) => ({
    key: index + '-' + op.kind,
    kind: op.kind,
    left: op.leftNo == null ? null : { no: op.leftNo, text: op.text },
    right: op.rightNo == null ? null : { no: op.rightNo, text: op.text },
  }))
}

/** 快照是 { [key: string]: unknown }，取值前先确认是字符串 */
/** 快照里的交通是英文枚举，审核员要看到中文 */
export function transportLabel(value: unknown): string {
  if (typeof value !== 'string' || !value) return '步行'
  const labels: Record<string, string> = {
    walk: '步行',
    metro: '地铁',
    bus: '公交',
    taxi: '打车',
    bike: '骑行',
    car: '自驾',
  }
  return labels[value] ?? value
}

/** 价格在快照里是分，审核员要看到元；缺一端时不能把 0 当成"免费" */
function priceText(min: unknown, max: unknown): string {
  const low = typeof min === 'number' ? min : null
  const high = typeof max === 'number' ? max : null
  const yuan = (cents: number): string => {
    const value = cents / 100
    return Number.isInteger(value) ? String(value) : value.toFixed(2)
  }
  if (low !== null && high !== null) return `门票 ¥${yuan(low)}-¥${yuan(high)}`
  if (low !== null) return `门票 ¥${yuan(low)} 起`
  if (high !== null) return `门票 ≤¥${yuan(high)}`
  return '门票待核实'
}

function mediaText(value: unknown): string {
  const items = Array.isArray(value) ? value : []
  return items.length > 0 ? `配图 ${items.length} 张` : '无配图'
}

/**
 * 把投稿快照翻成可读文本，给「版本对照」做逐行 diff 用。
 * 快照里没有 content 字段（只有 title/summary/theme_tags/nodes），
 * 直接读 content 会让对照区永远空白 —— 这是 P-20 记的那个坑。
 * 节点必须把 place_id / 价格 / 配图也渲染出来，否则换点、改价、换图
 * 三种改动在文本上完全一样，审核员会看到"没有差异"。
 */
export function snapshotToText(
  snapshot: Record<string, unknown> | null | undefined,
  placeNames: Record<string, string> | null | undefined = null,
): string {
  if (!snapshot) return ''
  const lines: string[] = []
  const title = snapshotText(snapshot, 'title')
  if (title) lines.push(`标题：${title}`)
  const summary = snapshotText(snapshot, 'summary')
  if (summary) lines.push(`摘要：${summary}`)

  const tags = Array.isArray(snapshot.theme_tags) ? snapshot.theme_tags : []
  if (tags.length > 0) lines.push(`主题：${tags.join('、')}`)

  const minutes = typeof snapshot.duration_minutes === 'number' ? snapshot.duration_minutes : null
  if (minutes !== null) lines.push(`时长：${Math.round((minutes / 60) * 10) / 10} 小时`)

  const nodes = Array.isArray(snapshot.nodes) ? snapshot.nodes : []
  if (nodes.length > 0) {
    lines.push(`节点（${nodes.length} 个）：`)
    nodes.forEach((raw, index) => {
      const node = (raw ?? {}) as Record<string, unknown>
      const parts: string[] = []
      const placeId = typeof node.place_id === 'string' ? node.place_id : ''
      if (placeId) {
        const name = placeNames?.[placeId]
        // 地名解析不到时退回短 id，至少让"换了个点"可见
        parts.push(`地点 ${name ?? placeId.slice(0, 8)}`)
      }
      if (typeof node.stay_minutes === 'number') parts.push(`停留 ${node.stay_minutes} 分钟`)
      parts.push(transportLabel(node.transport))
      parts.push(priceText(node.price_min, node.price_max))
      parts.push(mediaText(node.media_asset_ids))
      const note = typeof node.note === 'string' ? node.note : ''
      const head = `${index + 1}. ${parts.join(' · ')}`
      lines.push(note ? `${head}｜${note}` : head)
    })
  }
  return lines.join('\n')
}

export function snapshotText(
  snapshot: Record<string, unknown> | null | undefined,
  key: string,
): string {
  const value = snapshot?.[key]
  return typeof value === 'string' ? value : ''
}
