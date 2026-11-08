/**
 * 适合人群的取值与中文标签。
 *
 * value 必须与契约 `components.schemas.Audience` 的枚举一致（落库值），
 * label 只用于界面展示；两处页面（浏览筛选、创作页多选）共用这一份，
 * 避免各自翻译后拼写漂移。
 */
export const AUDIENCE_OPTIONS = [
  { value: 'family', label: '亲子' },
  { value: 'student', label: '学生' },
  { value: 'couple', label: '情侣' },
  { value: 'senior', label: '长者' },
  { value: 'solo', label: '独自' },
  { value: 'group', label: '团体' },
] as const

export type AudienceValue = (typeof AUDIENCE_OPTIONS)[number]['value']

export function audienceLabel(value: string): string {
  return AUDIENCE_OPTIONS.find((option) => option.value === value)?.label ?? value
}
