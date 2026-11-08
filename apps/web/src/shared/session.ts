/**
 * 登录态。mock 模式下密码是 cw123456（见 shared/mock/workspace.ts），
 * 接口接好后这里一行都不用改：token 与 refresh_token 都由 /auth/login 返回。
 */

import { computed, reactive } from 'vue'

import type { Schemas } from '@/api/types'

import { ApiError, SESSION_EXPIRED_EVENT, api, setToken } from './api'
import { readItem, removeItem, writeItem } from './storage'

type User = Schemas['User']
type TokenPair = Schemas['TokenPair']

const USER_KEY = 'cw_user'
const REFRESH_KEY = 'cw_refresh_token'

function readUser(): User | null {
  try {
    const raw = readItem(USER_KEY)
    return raw ? (JSON.parse(raw) as User) : null
  } catch {
    return null
  }
}

const state = reactive<{ user: User | null }>({ user: readUser() })

function persist(user: User | null): void {
  state.user = user
  if (user) writeItem(USER_KEY, JSON.stringify(user))
  else removeItem(USER_KEY)
}

function adopt(pair: TokenPair): User {
  setToken(pair.access_token)
  writeItem(REFRESH_KEY, pair.refresh_token)
  persist(pair.user)
  return pair.user
}

export async function login(username: string, password: string): Promise<User> {
  return adopt(await api.post<TokenPair>('/auth/login', { username, password }))
}

export async function register(username: string, password: string): Promise<User> {
  return adopt(await api.post<TokenPair>('/auth/register', { username, password }))
}

export async function logout(): Promise<void> {
  try {
    await api.post('/auth/logout')
  } catch {
    /* 后端不可达也要清掉本地登录态 */
  }
  setToken(null)
  removeItem(REFRESH_KEY)
  persist(null)
}

/** 用 token 换最新用户信息；服务端判定 token 失效时才清空登录态。 */
export async function refreshMe(): Promise<User | null> {
  if (!readItem('cw_user')) return null
  try {
    const me = await api.get<User>('/users/me')
    persist(me)
    return me
  } catch (error) {
    // 网络抖动 / 5xx 不代表登录失效：保留 cw_user，下次进页面再试。
    // 只有 401/403 才清，否则用户在弱网下会被无声登出。
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
      setToken(null)
      removeItem(REFRESH_KEY)
      persist(null)
    }
    return null
  }
}

// api 层刷新令牌失败时广播；这里把 cw_user 一起清掉，
// 否则 isLoggedIn/isAdmin 还停在 true，页面继续渲染登录态并反复 401。
if (typeof window !== 'undefined') {
  window.addEventListener(SESSION_EXPIRED_EVENT, () => persist(null))
}

export const currentUser = computed(() => state.user)
export const isLoggedIn = computed(() => state.user !== null)
export const isAdmin = computed(() => state.user?.role === 'admin')
