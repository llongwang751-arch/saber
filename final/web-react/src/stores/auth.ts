// stores/auth.ts — 登录/注册/登出 + JWT 持久化（localStorage）。
// Pinia → Zustand 移植：state + actions 一一对应；loggedIn getter 变为选择器。
import { create } from 'zustand'
import { apiURL } from '../api/client'

const TOKEN_KEY = 'agi_auth_token'
const USER_KEY = 'agi_auth_user'

function storageGet(key: string): string {
  try { return localStorage.getItem(key) || '' } catch { return '' }
}

export interface AuthState {
  token: string
  username: string
  mode: 'login' | 'register'
  error: string
  overlay: boolean
  submitting: boolean
  setMode(mode: 'login' | 'register'): void
  showOverlay(msg?: string): void
  hideOverlay(): void
  persist(): void
  submit(username: string, password: string): Promise<boolean>
  logout(): void
}

export const useAuth = create<AuthState>()((set, get) => ({
  token: storageGet(TOKEN_KEY),
  username: storageGet(USER_KEY),
  mode: 'login', // 'login' | 'register'
  error: '',
  overlay: !storageGet(TOKEN_KEY), // 无 token 则一开始就显示登录层
  submitting: false,
  setMode(m) { set({ mode: m, error: '' }) },
  showOverlay(msg) { set(msg ? { overlay: true, error: msg } : { overlay: true }) },
  hideOverlay() { set({ overlay: false, error: '' }) },
  persist() {
    localStorage.setItem(TOKEN_KEY, get().token)
    localStorage.setItem(USER_KEY, get().username)
  },
  async submit(username, password) {
    if (!username || !password) { set({ error: '用户名和密码不能为空' }); return false }
    set({ submitting: true, error: '' })
    const url = get().mode === 'login' ? '/api/auth/login' : '/api/auth/register'
    try {
      const resp = await fetch(apiURL(url), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })
      const data = await resp.json()
      if (!resp.ok) { set({ error: data.error || data.detail || '操作失败' }); return false }
      set({ token: data.token, username: data.username })
      get().persist()
      get().hideOverlay()
      return true
    } catch {
      set({ error: '网络错误，请稍后再试' })
      return false
    } finally {
      set({ submitting: false })
    }
  },
  logout() {
    set({ token: '', username: '' })
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
    get().showOverlay()
  },
}))

export const selectLoggedIn = (s: AuthState) => !!s.token
