// stores/chat.ts — 对话发送 + SSE 流式，事件驱动就地更新消息对象。
// Pinia → Zustand 移植：send/handleEvent 逻辑逐字对齐；流式增量通过 sessions.touch()
// 触发 React 重渲染（消息对象本身仍被就地修改，语义与 Pinia 版一致）。
import { create } from 'zustand'
import { apiFetch } from '../api/client'
import { readSSE } from '../lib/sse'
import { useSessions, type ChatMessage } from './sessions'
import { useDocs } from './docs'

function requestId(): string {
  return globalThis.crypto?.randomUUID?.()
    || `chat-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

export interface ChatState {
  loading: boolean
  abort: AbortController | null
  activeConversationId: string
  ragOn: boolean
  toggleRag(): void
  abortInflight(): void
  stop(): void
  handleEvent(ai: ChatMessage, evt: string, data: any): void
  send(text: string): Promise<void>
}

export const useChat = create<ChatState>()((set, get) => ({
  loading: false,
  abort: null,
  activeConversationId: '',
  ragOn: false,
  toggleRag() { set(state => ({ ragOn: !state.ragOn })) },
  abortInflight() {
    if (get().abort) { get().abort!.abort(); set({ abort: null }) }
    set({ loading: false })
  },
  stop() {
    get().abortInflight()
    apiFetch('/api/chat/cancel?conversation_id=' + encodeURIComponent(get().activeConversationId), { method: 'POST' }).catch(() => {})
  },
  handleEvent(ai, evt, data) {
    switch (evt) {
      case 'start': ai.runId = data.run_id || ''; break
      case 'route': ai.mode = data.mode || 'chat'; break
      case 'memory': ai.memory = data.extracted_info || ''; break
      case 'sandbox_ready': ai.sandbox = data.workspace || ''; break
      case 'step': ai.steps!.push({ type: data.type, content: data.content || '', params: data.params || null }); break
      case 'tool_call': ai.toolCall = { tool_name: data.tool_name, params: data.params, tool_result: data.tool_result }; break
      case 'rag_trace': ai.ragTrace = data || {}; break
      case 'rag_result': ai.ragResults = data.search_results || []; break
      case 'token': ai.answer = (ai.answer || '') + (data.content || ''); break
      case 'done':
        if (!ai.answer && data.answer) ai.answer = data.answer
        if ((!ai.steps || !ai.steps.length) && Array.isArray(data.steps)) {
          ai.steps = data.steps.map((s: any) => ({ type: s.type, content: s.content || '', params: s.params || null }))
        }
        if (data.interrupted) ai.interrupted = true
        break
    }
  },
  async send(text) {
    const msg = (text || '').trim()
    if (!msg || get().loading) return
    const docs = useDocs.getState()
    if (!useSessions.getState().currentId) await useSessions.getState().newSession()
    const sessionId = useSessions.getState().currentId as string
    set({ activeConversationId: sessionId })
    // 一个用户 turn 只生成一个请求标识。请求一旦送达服务端便不做
    // 自动重放；否则断线后的同步回落可能把工具或 Agent 执行两次。
    const turnRequestId = requestId()

    useSessions.getState().addMessage(sessionId, { role: 'user', text: msg })
    const ai = useSessions.getState().addMessage(sessionId, {
      role: 'ai', mode: 'chat', steps: [], memory: '', toolCall: null,
      ragResults: null, ragTrace: null, answer: '', interrupted: false, sandbox: '', streaming: true,
    })!

    set({ loading: true })
    const controller = new AbortController()
    set({ abort: controller })
    const body = { message: msg, use_rag: get().ragOn, conversation_id: sessionId }

    try {
      const resp = await apiFetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Request-ID': turnRequestId },
        body: JSON.stringify(body),
        signal: controller.signal,
      })
      if (!resp.ok) {
        const raw = await resp.text()
        let data: any = {}
        if (raw) {
          try { data = JSON.parse(raw) } catch { data = { detail: raw } }
        }
        if (resp.status === 409) {
          throw new Error('该请求已经执行或正在执行。为避免重复调用工具，系统没有自动重试；请查看原会话结果，确认后再发送新请求。')
        }
        throw new Error(data.detail || data.error || `请求失败（HTTP ${resp.status}）`)
      } else {
        await readSSE(resp, (evt, data) => {
          get().handleEvent(ai, evt, data)
          useSessions.getState().touch(sessionId)
        })
      }
      docs.loadLibrary()
    } catch (e: any) {
      if (e?.name === 'AbortError') ai.answer = ai.answer || '🛑 已中断'
      else if (e?.message !== 'unauthorized') {
        const detail = String(e?.message || '').trim()
        const failure = detail
          ? `❌ ${detail}`
          : '❌ 请求失败；服务端可能仍在处理。为避免重复执行，系统没有自动重试。'
        ai.answer = ai.answer ? `${ai.answer}\n\n${failure}` : failure
      }
    } finally {
      ai.streaming = false
      if (get().abort === controller) {
        set({ loading: false, abort: null })
      }
      useSessions.getState().touch(sessionId)
      useSessions.getState().save()
    }
  },
}))

// modeHint getter（Pinia）→ 纯函数（在组件中以 ragOn 计算，避免不稳定 selector）。
export function modeHint(ragOn: boolean): { text: string; cls: string } {
  if (ragOn) return { text: '📚 知识库增强模式', cls: 'hint-pink' }
  return { text: '💬 直接对话模式', cls: 'hint-muted' }
}
