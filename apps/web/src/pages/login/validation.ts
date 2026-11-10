/**
 * 登录页的纯函数部分：校验、错误文案、回跳地址。
 * 抽出来是为了能在 vitest 里直接测，不依赖 DOM 与 localStorage。
 */

export type AuthField = 'username' | 'password' | 'confirm'

export type AuthForm = {
  username: string
  password: string
  confirm?: string
}

export type FieldProblem = {
  field: AuthField
  message: string
}

export type DemoAccount = {
  username: string
  password: string
  /** 演示账号的角色说明，只用于按钮旁边的小字 */
  label: string
}

/** 假数据模式（VITE_USE_MOCK 默认开启）里的演示账号。 */
export const DEMO_ACCOUNTS: DemoAccount[] = [
  { username: '文化漫步者', password: 'cw123456', label: '普通用户' },
  { username: '审核编辑', password: 'cw123456', label: '管理员' },
]

/** 真接口模式下的演示账号：由 `scripts/seed_demo.py` 写进数据库（P-21）。 */
export const SEEDED_ACCOUNTS: DemoAccount[] = [
  { username: 'demo@demo.local', password: 'Demo12345!', label: '普通用户' },
  { username: 'reviewer@demo.local', password: 'Demo12345!', label: '审核员（管理员）' },
  { username: 'editor@demo.local', password: 'Demo12345!', label: '编辑' },
]

/** 当前该显示哪组演示账号：跟着“是否连真后端”走，否则点了必然登录失败。 */
export function demoAccounts(useMock: boolean): DemoAccount[] {
  return useMock ? DEMO_ACCOUNTS : SEEDED_ACCOUNTS
}

/** 登录只做友好提示：后端 LoginIn 不校验长度（历史账号可能只有 2 个字）。 */
export const MIN_USERNAME_LENGTH = 2
export const MIN_PASSWORD_LENGTH = 6
/** 注册必须与契约对齐：identity/schemas.py RegisterIn 是 username≥3、password≥8。 */
export const MIN_REGISTER_USERNAME_LENGTH = 3
export const MIN_REGISTER_PASSWORD_LENGTH = 8

/** 用户名前后空格不算字数，密码按原样算长度。 */
export function validateLogin(form: AuthForm): FieldProblem | null {
  if (form.username.trim().length < MIN_USERNAME_LENGTH) {
    return { field: 'username', message: `用户名至少 ${MIN_USERNAME_LENGTH} 个字` }
  }
  if (form.password.length < MIN_PASSWORD_LENGTH) {
    return { field: 'password', message: `密码至少 ${MIN_PASSWORD_LENGTH} 位` }
  }
  return null
}

export function validateRegister(form: AuthForm): FieldProblem | null {
  // 不能复用 validateLogin：注册的下限比登录严，复用会让 2 字用户名/6 位密码
  // 一路放行到 POST /auth/register，被后端 422 打回来。
  if (form.username.trim().length < MIN_REGISTER_USERNAME_LENGTH) {
    return { field: 'username', message: `用户名至少 ${MIN_REGISTER_USERNAME_LENGTH} 个字` }
  }
  if (form.password.length < MIN_REGISTER_PASSWORD_LENGTH) {
    return { field: 'password', message: `密码至少 ${MIN_REGISTER_PASSWORD_LENGTH} 位` }
  }
  if (form.password !== (form.confirm ?? '')) {
    return { field: 'confirm', message: '两次输入的密码不一致' }
  }
  return null
}

/** 契约里 code 稳定、message 只是给人看的，所以先按 code 映射，兜底才用 message。 */
const CODE_TEXT: Record<string, string> = {
  INVALID_CREDENTIALS: '用户名或密码不对',
  USERNAME_TAKEN: '这个用户名已经被用了',
}

export function authErrorText(code: string | null, message?: string): string {
  const mapped = code ? CODE_TEXT[code] : undefined
  if (mapped) return mapped
  const text = (message ?? '').trim()
  return text === '' ? '请求失败，请稍后重试' : text
}

/** 只认站内路径，避免 ?redirect= 被拿去做开放跳转。 */
export function redirectTarget(raw: unknown): string {
  const value = Array.isArray(raw) ? raw[0] : raw
  if (typeof value !== 'string') return '/'
  const path = value.trim()
  // 浏览器会把 \ 归一成 /，所以 /\evil.com 和 //evil.com 一样是站外地址，不能放行
  if (path === '' || !path.startsWith('/') || path.startsWith('//') || path.startsWith('/\\')) return '/'
  return path
}
