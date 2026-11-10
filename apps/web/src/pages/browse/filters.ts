/**
 * 浏览页的筛选逻辑。抽成纯函数，方便单测，也让页面只负责状态与渲染。
 */

import { AUDIENCE_OPTIONS as AUDIENCE_CHOICES } from '@/shared/audience'

export type BrowseFilters = {
  q: string
  theme: string
  audience: string
  duration: string
  channel: string
}

export const EMPTY_FILTERS: BrowseFilters = {
  q: '',
  theme: '',
  audience: '',
  duration: '',
  channel: '',
}

/** 时长区间取值与接口契约一致：0-120 / 120-240 / 240- */
export const DURATION_OPTIONS = [
  { value: '', label: '全部时长' },
  { value: '0-120', label: '2 小时以内' },
  { value: '120-240', label: '2-4 小时' },
  { value: '240-', label: '4 小时以上' },
] as const

export const CHANNEL_OPTIONS = [
  { value: '', label: '全部来源' },
  { value: 'official', label: '官方' },
  { value: 'user', label: '用户投稿' },
] as const

/** 与契约 Audience 枚举一一对应；value 是落库值，label 才是给用户看的。 */
export const AUDIENCE_OPTIONS = [
  { value: '', label: '全部人群' },
  ...AUDIENCE_CHOICES.map((option) => ({ value: option.value as string, label: option.label })),
]

export const THEME_LIMIT = 10

export function hasActiveFilter(filters: BrowseFilters): boolean {
  return Boolean(
    filters.q.trim() ||
      filters.theme ||
      filters.audience ||
      filters.duration ||
      filters.channel,
  )
}

/** 空值不下发，避免 ?theme=&duration= 这类噪声；limit 总是带。 */
export function buildRouteQuery(
  filters: BrowseFilters,
  cursor: string | null,
  limit: number,
): string {
  const params = new URLSearchParams()
  const q = filters.q.trim()
  if (q) params.set('q', q)
  if (filters.theme) params.set('theme', filters.theme)
  if (filters.audience) params.set('audience', filters.audience)
  if (filters.duration) params.set('duration', filters.duration)
  if (filters.channel) params.set('channel', filters.channel)
  if (cursor) params.set('cursor', cursor)
  params.set('limit', String(limit))
  return params.toString()
}

/** 深链 ?theme=xxx：重复参数取第一个，空白当没选，非字符串一律忽略。 */
export function themeFromQuery(raw: unknown): string {
  const value = Array.isArray(raw) ? raw[0] : raw
  return typeof value === 'string' ? value.trim() : ''
}

/**
 * 点主题 chip 时写回地址栏的 query：其它参数原样保留，theme 为空就删掉，
 * 这样刷新 / 分享链接都能还原同一个筛选。history 由调用方用 replace 控制。
 */
export function queryWithTheme(
  query: Record<string, string | (string | null)[] | null | undefined>,
  theme: string,
): Record<string, string | (string | null)[]> {
  const next: Record<string, string | (string | null)[]> = {}
  for (const [key, value] of Object.entries(query)) {
    if (key === 'theme' || value == null) continue
    next[key] = value
  }
  if (theme) next.theme = theme
  return next
}

/**
 * 把已加载路线的 theme_tags 汇总成筛选 chip：出现次数多的在前，最多 10 个。
 * 已选中的主题一定保留，否则选完之后 chip 消失，用户无法取消选择。
 */
export function rankThemes(
  pool: readonly string[],
  selected = '',
  limit = THEME_LIMIT,
): string[] {
  const counts = new Map<string, number>()
  const firstSeen: string[] = []
  for (const tag of pool) {
    if (!tag) continue
    if (!counts.has(tag)) firstSeen.push(tag)
    counts.set(tag, (counts.get(tag) ?? 0) + 1)
  }
  const ranked = [...firstSeen].sort((a, b) => (counts.get(b) ?? 0) - (counts.get(a) ?? 0))
  if (!selected) return ranked.slice(0, limit)
  const others = ranked.filter((tag) => tag !== selected)
  return [selected, ...others].slice(0, limit)
}
