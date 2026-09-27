// stores/sessions.ts — 本地多会话管理（localStorage 持久化，沿用旧 key 'ai_sessions'）。
// Pinia → Zustand 移植。消息以结构化对象存储：
//   user: { role:'user', text }
//   ai:   { role:'ai', mode, steps:[], memory, toolCall, ragResults, answer, interrupted, sandbox, streaming }
// 与 Vue 版的差异说明：流式期间对消息对象的就地更新保持不变（addMessage 返回同一对象引用），
// 但每次变更后通过 touch() 克隆数组触发 React 订阅更新。
import { create } from 'zustand'
import { apiFetch } from '../api/client'

const KEY = 'ai_sessions'

export interface ChatStep { type: string; content: string; params: any }
export interface ToolCall { tool_name: string; params: any; tool_result?: string }

export interface ChatMessage {
  role: string
  text?: string
  html?: string
  mode?: string
  steps?: ChatStep[]
  memory?: string
  toolCall?: ToolCall | null
  ragResults?: any[] | null
  ragTrace?: any
  answer?: string
  interrupted?: boolean
  sandbox?: string
  streaming?: boolean
  [key: string]: any
}

export interface Session {
  id: string
  title: string
  messages: ChatMessage[]
  ts: number
}

function load(): Session[] {
  try {
    const raw = typeof localStorage !== 'undefined' ? localStorage.getItem(KEY) : null
    return JSON.parse(raw || '[]')
  } catch { return [] }
}

export interface SessionsState {
  sessions: Session[]
  currentId: string | null
  save(): void
  newSession(): Promise<string>
  switchSession(id: string): void
  deleteSession(id: string): void
  addMessage(sessionId: string, msg: ChatMessage): ChatMessage | null
  // 流式期间消息对象被就地修改，touch() 克隆相关数组让订阅者收到新引用。
  touch(sessionId: string): void
  reset(): void
}

export const useSessions = create<SessionsState>()((set, get) => ({
  sessions: load(),
  currentId: null,
  save() { try { localStorage.setItem(KEY, JSON.stringify(get().sessions)) } catch { /* 配额满时静默失败，与 Vue 版行为一致 */ } },
  async newSession() {
    const response = await apiFetch('/api/conversations', { method: 'POST' })
    if (!response.ok) throw new Error('创建会话失败，请重试')
    const { conversation_id: id } = await response.json()
    set(state => ({
      sessions: [{ id, title: '新对话', messages: [], ts: Date.now() }, ...state.sessions].slice(0, 5),
      currentId: id,
    }))
    get().save()
    return id
  },
  switchSession(id) { set({ currentId: id }) },
  deleteSession(id) {
    const sessions = get().sessions.filter(s => s.id !== id)
    set({ sessions, currentId: get().currentId === id ? (sessions[0]?.id || null) : get().currentId })
    get().save()
  },
  // 追加一条消息，返回其在 state 中的对象引用（供流式就地更新）。
  addMessage(sessionId, msg) {
    let added: ChatMessage | null = null
    set(state => ({
      sessions: state.sessions.map(s => {
        if (s.id !== sessionId) return s
        const messages = [...s.messages, msg]
        added = msg
        let title = s.title
        if (messages.filter(m => m.role === 'user').length === 1 && msg.role === 'user') {
          title = (msg.text || '').slice(0, 20) || '新对话'
        }
        return { ...s, messages, title }
      }),
    }))
    get().save()
    return added
  },
  touch(sessionId) {
    set(state => ({
      sessions: state.sessions.map(s => (s.id === sessionId ? { ...s, messages: [...s.messages] } : s)),
    }))
  },
  reset() { set({ sessions: [], currentId: null }); get().save() },
}))

export function selectCurrentSession(state: SessionsState): Session | null {
  return state.sessions.find(x => x.id === state.currentId) || null
}

export const EMPTY_MESSAGES: ChatMessage[] = []
