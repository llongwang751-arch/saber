// stores/chat.js — 对话发送 + SSE 流式，事件驱动就地更新消息对象。
import { defineStore } from 'pinia'
import { apiFetch } from '../api/client'
import { readSSE } from '../composables/useSSE'
import { useSessions } from './sessions'
import { useDocs } from './docs'

function requestId() {
  return globalThis.crypto?.randomUUID?.()
    || `chat-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function opaqueExposureId(data) {
  if (!data || typeof data !== 'object') return ''
  if (data.success === false || data.interrupted === true || data.error) return ''
  if (data.experiment?.feedback_eligible !== true) return ''
  return String(data.experiment?.exposure_id || data.experiment?.opaque_exposure_id || '')
}

function rememberExposure(ai, data) {
  const exposureId = opaqueExposureId(data)
  ai.experimentExposureId = exposureId
  ai.feedbackEligible = Boolean(exposureId)
}

export const useChat = defineStore('chat', {
  state: () => ({
    loading: false,
    abort: null,
    activeConversationId: '',
    ragOn: false,
  }),
  getters: {
    modeHint(s) {
      if (s.ragOn) return { text: '📚 知识库增强模式', cls: 'hint-pink' }
      return { text: '💬 直接对话模式', cls: 'hint-muted' }
    },
  },
  actions: {
    toggleRag() { this.ragOn = !this.ragOn },
    abortInflight() {
      if (this.abort) { this.abort.abort(); this.abort = null }
      this.loading = false
    },
    stop() {
      this.abortInflight()
      apiFetch('/api/chat/cancel?conversation_id=' + encodeURIComponent(this.activeConversationId), { method: 'POST' }).catch(() => {})
    },
    handleEvent(ai, evt, data) {
      switch (evt) {
        case 'start': ai.runId = data.run_id || ''; break
        case 'route': ai.mode = data.mode || 'chat'; break
        case 'memory': ai.memory = data.extracted_info || ''; break
        case 'sandbox_ready': ai.sandbox = data.workspace || ''; break
        case 'step': ai.steps.push({ type: data.type, content: data.content || '', params: data.params || null }); break
        case 'tool_call': ai.toolCall = { tool_name: data.tool_name, params: data.params, tool_result: data.tool_result }; break
        case 'rag_trace': ai.ragTrace = data || {}; break
        case 'rag_result': ai.ragResults = data.search_results || []; break
        case 'token': ai.answer += (data.content || ''); break
        case 'done':
          if (!ai.answer && data.answer) ai.answer = data.answer
          if ((!ai.steps || !ai.steps.length) && Array.isArray(data.steps)) {
            ai.steps = data.steps.map(s => ({ type: s.type, content: s.content || '', params: s.params || null }))
          }
          if (data.interrupted) ai.interrupted = true
          rememberExposure(ai, data)
          break
      }
    },
    async send(text) {
      const msg = (text || '').trim()
      if (!msg || this.loading) return
      const sess = useSessions()
      const docs = useDocs()
      if (!sess.currentId) await sess.newSession()
      const sessionId = sess.currentId
      this.activeConversationId = sessionId
      // 一个用户 turn 只生成一个请求标识。请求一旦送达服务端便不做
      // 自动重放；否则断线后的同步回落可能把工具或 Agent 执行两次。
      const turnRequestId = requestId()

      sess.addMessage(sessionId, { role: 'user', text: msg })
      const ai = sess.addMessage(sessionId, {
        role: 'ai', mode: 'chat', steps: [], memory: '', toolCall: null,
        ragResults: null, ragTrace: null, answer: '', interrupted: false, sandbox: '', streaming: true,
        experimentExposureId: '', feedbackEligible: false, feedbackRating: null, feedbackEventId: '', feedbackLoading: false, feedbackError: '',
      })

      this.loading = true
      const controller = new AbortController()
      this.abort = controller
      const body = { message: msg, use_rag: this.ragOn, conversation_id: sessionId }

      try {
        const resp = await apiFetch('/api/chat/stream', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Request-ID': turnRequestId },
          body: JSON.stringify(body),
          signal: controller.signal,
        })
        if (!resp.ok) {
          const raw = await resp.text()
          let data = {}
          if (raw) {
            try { data = JSON.parse(raw) } catch { data = { detail: raw } }
          }
          if (resp.status === 409) {
            throw new Error('该请求已经执行或正在执行。为避免重复调用工具，系统没有自动重试；请查看原会话结果，确认后再发送新请求。')
          }
          throw new Error(data.detail || data.error || `请求失败（HTTP ${resp.status}）`)
        } else {
          await readSSE(resp, (evt, data) => this.handleEvent(ai, evt, data))
        }
        docs.loadLibrary()
      } catch (e) {
        ai.experimentExposureId = ''
        ai.feedbackEligible = false
        if (e.name === 'AbortError') ai.answer = ai.answer || '🛑 已中断'
        else if (e.message !== 'unauthorized') {
          const detail = String(e.message || '').trim()
          const failure = detail
            ? `❌ ${detail}`
            : '❌ 请求失败；服务端可能仍在处理。为避免重复执行，系统没有自动重试。'
          ai.answer = ai.answer ? `${ai.answer}\n\n${failure}` : failure
        }
      } finally {
        ai.streaming = false
        if (this.abort === controller) {
          this.loading = false
          this.abort = null
        }
        sess.save()
      }
    },
    async submitExperimentFeedback(message, rating) {
      if (message?.feedbackEligible !== true || !message.experimentExposureId || message.feedbackLoading || message.feedbackRating != null) return
      if (rating !== 1 && rating !== -1) return
      const sess = useSessions()
      const eventId = message.feedbackEventId || requestId()
      message.feedbackEventId = eventId
      message.feedbackAttemptedRating = message.feedbackAttemptedRating ?? rating
      if (message.feedbackAttemptedRating !== rating) return
      message.feedbackLoading = true
      message.feedbackError = ''
      sess.save()
      try {
        const response = await apiFetch(`/api/online-experiments/exposures/${encodeURIComponent(message.experimentExposureId)}/feedback`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json', 'X-Request-ID': eventId },
          body: JSON.stringify({ rating, event_id: eventId }),
        })
        const raw = await response.text()
        let data = {}
        if (raw) {
          try { data = JSON.parse(raw) } catch { data = { detail: raw } }
        }
        if (!response.ok) throw new Error(data.detail || data.error || `HTTP ${response.status}`)
        message.feedbackRating = rating
        message.feedbackError = ''
      } catch (error) {
        message.feedbackError = error.message === 'unauthorized'
          ? '登录状态已失效，请重新登录后重试。'
          : `反馈未保存：${error.message || '请稍后重试'}`
      } finally {
        message.feedbackLoading = false
        sess.save()
      }
    },
  },
})
