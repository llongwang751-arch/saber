<template>
  <div class="run-backdrop" @click.self="$emit('close')">
    <section ref="dialog" class="run-shell" role="dialog" aria-modal="true" aria-label="Saber 运行记录" tabindex="-1">
      <header class="run-header">
        <div><span>AGI-SABER / AGENT WORKSPACE</span><h2>{{ mode === 'research' ? '研究工作台' : '运行记录' }}</h2><p>提出目标，审核计划，追踪研究过程与证据。关闭面板后任务继续执行。</p></div>
        <button type="button" class="close" aria-label="关闭运行记录" @click="$emit('close')">×</button>
      </header>
      <div class="run-layout">
        <aside class="run-list" aria-label="最近任务">
          <button type="button" class="new-run" @click="newRun">＋ 新建任务</button>
          <div class="list-label"><span>最近运行 <small v-if="summary">· 活跃 {{ summary.active }}/{{ summary.capacity }}</small></span><button type="button" @click="refresh(true)" aria-label="刷新任务列表">↻</button></div>
          <button v-for="item in runs" :key="item.run_id" type="button" class="run-item" :class="{ active: item.run_id === selected?.run_id }" @click="selectRun(item)">
            <strong>{{ item.message }}</strong><small>{{ kindLabel(item.kind) }} · {{ statusLabel(item.status) }} · {{ dateLabel(item.created_at) }}</small>
          </button>
          <p v-if="!runs.length" class="empty-list">暂无任务记录。</p>
        </aside>
        <main class="run-main">
          <p v-if="error" class="error run-error" role="alert">{{ error }}</p>
          <div class="run-detail">
            <div v-if="selected" class="run-meta">
              <div><span>运行状态</span><strong :class="selected.status">{{ statusLabel(selected.status) }}</strong></div>
              <div><span>会话</span><code>{{ (selected.conversation_id || '').slice(0, 12) }}</code></div>
              <button v-if="isRunObservable(selected.status)" type="button" class="stop" :disabled="cancelling || selected.status === 'cancelling'" @click="cancelRun">{{ cancelling || selected.status === 'cancelling' ? '正在停止…' : '请求停止' }}</button>
              <button v-else-if="selected.status === 'interrupted' && selectedResearch && selected.plan_status === 'approved'" type="button" class="continue" :disabled="resuming" @click="resumeRun">{{ resuming ? '正在恢复…' : '继续研究' }}</button>
              <button v-else type="button" class="continue" @click="continueRun">继续此会话</button>
            </div>
            <div v-if="selected" class="request-card"><span>任务目标</span><p>{{ selected.message }}</p></div>
            <PlanReview v-if="selected?.plan" :key="selected.run_id" :plan="selected.plan" :version="selected.plan_version || 1" :status="selected.status" :review-status="selected.plan_status" :busy="reviewing" @review="reviewPlan" />
            <div v-if="selectedResearch && researchSteps.length" class="research-step-progress" aria-label="研究步骤进度"><div v-for="step in researchSteps" :key="step.id"><strong>{{ step.title || step.id }}</strong><span>{{ stepStatusLabel(step.status) }}<template v-if="step.rounds"> · {{ step.rounds }} 轮</template></span></div></div>
            <details v-if="events.length" class="event-disclosure" :open="!selectedResearch"><summary>执行过程 · {{ events.length }} 条记录</summary><div class="event-list" aria-label="执行事件">
              <div v-for="event in visibleEvents" :key="event.event_id" class="event-row"><span class="event-mark"></span><span>{{ eventLabel(event) }}</span><small>{{ timeLabel(event.created_at) }}</small></div>
            </div></details>
            <div v-if="isRunActive(selected?.status)" class="running" role="status"><span class="pulse"></span><span>{{ progress }}</span></div>
            <p v-if="selected?.status === 'awaiting_plan_review'" class="research-notice" role="status">研究已暂停，等待审核计划。</p>
            <p v-if="selected?.result?.reason" class="result-reason" role="status">{{ selected.result.reason }}</p>
            <ResearchReport v-if="selectedResearch" :result="result" :report="answer" :sources="sources" :artifacts="artifacts" />
            <div v-else-if="answer" class="answer"><strong>AGI-saber 结果</strong><pre>{{ answer }}</pre></div>
            <div v-if="!selected && !error" class="welcome"><span aria-hidden="true">◇</span><h3>{{ mode === 'research' ? '从一个值得研究的问题开始' : '运行一个可追踪的长任务' }}</h3><p>{{ mode === 'research' ? '先生成可修改的计划，再检索资料、补充证据，最后交付带引用的报告。' : '每次运行都有独立状态与事件记录，页面断开后仍可查看结果。' }}</p></div>
          </div>
          <form v-if="!selected || composing" class="run-compose" @submit.prevent="submit">
            <div v-if="researchEnabled" class="run-mode" role="group" aria-label="任务模式"><button type="button" :aria-pressed="mode === 'research'" @click="mode = 'research'">深度研究</button><button type="button" :aria-pressed="mode === 'chat'" @click="mode = 'chat'">普通任务</button><span>{{ mode === 'research' ? '计划 → 审核 → 研究 → 报告' : '直接执行任务' }}</span></div>
            <label for="native-run-message">任务目标</label>
            <textarea id="native-run-message" v-model="message" maxlength="20000" rows="3" placeholder="描述要完成的任务和输出要求" :disabled="submitting" @keydown.ctrl.enter.prevent="submit"></textarea>
            <div class="compose-bottom"><label class="rag-option"><input v-model="useRag" type="checkbox" /> 使用 Saber 知识库</label><span v-if="conversationId">将继续当前会话</span><button type="submit" :disabled="submitting || !message.trim()">{{ submitting ? '提交中…' : mode === 'research' ? '生成研究计划' : '开始运行' }}</button></div>
          </form>
        </main>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { apiFetch, fetchJSON } from '../api/client'
import { readSSE } from '../composables/useSSE'
import { isRunActive, isRunObservable, researchResult, runEventLabel } from '../utils/research'
import PlanReview from './PlanReview.vue'
import ResearchReport from './ResearchReport.vue'

const props = defineProps({ initialMode: { type: String, default: 'research' }, researchEnabled: { type: Boolean, default: true } })
const emit = defineEmits(['close'])
const mode = ref(props.researchEnabled ? props.initialMode : 'chat')
const dialog = ref(null)
const reviewing = ref(false)
const cancelling = ref(false)
const resuming = ref(false)
const composing = ref(false)
const runs = ref([])
const summary = ref(null)
const selected = ref(null)
const events = ref([])
const message = ref('')
const conversationId = ref('')
const useRag = ref(false)
const submitting = ref(false)
const error = ref('')
const progress = ref('等待执行事件…')
const tokenText = ref('')
let streamController = null
let generation = 0
let lastEventId = 0
let lastSubmissionSignature = ''
let lastSubmissionKey = ''
let reviewPoll = null
let previousFocus = null

const selectedResearch = computed(() => selected.value?.kind === 'research' || selected.value?.mode === 'research' || !!selected.value?.plan)
const visibleEvents = computed(() => events.value.filter(event => !['token', 'done'].includes(event.type)).slice(-80))
const result = computed(() => researchResult(selected.value))
const answer = computed(() => result.value.report_markdown || result.value.answer || tokenText.value)
const sources = computed(() => {
  const byId = new Map()
  for (const source of [...events.value.filter(event => event.type === 'source_found').map(event => event.data), ...(selected.value?.sources || []), ...(result.value.sources || [])]) {
    if (source?.source_id) byId.set(source.source_id, source)
  }
  return [...byId.values()]
})
const artifacts = computed(() => result.value.artifacts?.length ? result.value.artifacts : selected.value?.artifacts || [])
const researchSteps = computed(() => {
  const steps = new Map((selected.value?.plan?.steps || []).map(step => [step.id, { ...step, status: 'pending' }]))
  for (const event of events.value) {
    const data = event.data || {}
    const id = data.step_id || data.id
    if (!steps.has(id)) continue
    if (event.type === 'node_start') steps.set(id, { ...steps.get(id), status: 'running' })
    if (event.type === 'node_done') steps.set(id, { ...steps.get(id), status: data.status || 'completed' })
    if (event.type === 'research_round') steps.set(id, { ...steps.get(id), rounds: data.round })
  }
  for (const step of result.value.steps || []) steps.set(step.id, { ...steps.get(step.id), ...step })
  return [...steps.values()]
})
const statusLabel = status => ({ pending: '排队中', running: '执行中', awaiting_plan_review: '待审核计划', cancelling: '停止中', completed: '已完成', partial: '部分完成', cancelled: '已停止', failed: '执行失败', interrupted: '已中断' })[status] || status || '未知'
const stepStatusLabel = status => ({ pending: '等待执行', running: '执行中', completed: '已完成', failed: '失败', partial: '部分完成', code_only: '仅生成代码', skipped: '已跳过', cancelled: '已停止' })[status] || status
const kindLabel = kind => ({ research: '深度研究', chat: '后台任务', chat_sync: '即时聊天', chat_stream: '流式聊天', recovery: '检查点恢复' })[kind] || '任务'
const dateLabel = value => value ? new Date(value * 1000).toLocaleDateString('zh-CN') : ''
const timeLabel = value => value ? new Date(value * 1000).toLocaleTimeString('zh-CN') : ''
const eventLabel = runEventLabel
function applyEvent(event) {
  if (event.event_id <= lastEventId) return
  lastEventId = event.event_id
  if (event.type !== 'token') events.value = [...events.value.slice(-199), event]
  if (event.type === 'token') tokenText.value += String(event.data?.content || '')
  if (event.type !== 'token') progress.value = eventLabel(event)
  if (event.type.startsWith('plan_') && selected.value) {
    const data = event.data || {}
    selected.value = { ...selected.value, ...(data.plan ? { plan: data.plan } : {}), ...(data.version ? { plan_version: data.version } : {}), ...(data.status ? { status: data.status } : {}) }
  }
}
async function refresh(reloadSelected = false) {
  try {
    const [items, state] = await Promise.all([
      fetchJSON('/api/agent-runs?limit=50'), fetchJSON('/api/agent-runs/summary'),
    ])
    runs.value = items
    summary.value = state
    if (reloadSelected && selected.value) await selectRun(selected.value)
  } catch (e) { error.value = e.message }
}
async function selectRun(item) {
  composing.value = false
  generation++
  if (streamController) streamController.abort()
  const current = generation
  selected.value = item
  nextTick(() => { const detail = dialog.value?.querySelector('.run-detail'); if (detail) detail.scrollTop = 0 })
  events.value = []
  lastEventId = 0
  tokenText.value = ''
  error.value = ''
  try {
    while (current === generation) {
      const history = await fetchJSON(`/api/agent-runs/${encodeURIComponent(item.run_id)}/events?after=${lastEventId}&limit=500`)
      if (current !== generation) return
      history.forEach(applyEvent)
      if (history.length < 500 || history.some(event => event.type === 'done')) break
    }
    const detail = await fetchJSON(`/api/agent-runs/${encodeURIComponent(item.run_id)}`)
    if (current !== generation) return
    selected.value = detail
    if (isRunObservable(selected.value.status)) followRun(item.run_id, current)
  } catch (e) { if (current === generation) error.value = e.message }
}
async function followRun(runId, current) {
  let lastError = ''
  for (let attempt = 0; current === generation && attempt < 5; attempt++) {
    streamController = new AbortController()
    const cursor = lastEventId
    try {
      const response = await apiFetch(`/api/agent-runs/${encodeURIComponent(runId)}/stream?after=${cursor}`, { signal: streamController.signal })
      if (!response.ok || !response.body) throw new Error(`事件流 HTTP ${response.status}`)
      await readSSE(response, (type, data, metadata) => {
        if (current !== generation) return false
        const id = Number(metadata.id)
        if (id) applyEvent({ event_id: id, type, data, created_at: Date.now() / 1000 })
      })
    } catch (e) {
      if (e.name === 'AbortError' || current !== generation) return
      lastError = e.message
    }
    if (current !== generation) return
    try {
      const detail = await fetchJSON(`/api/agent-runs/${encodeURIComponent(runId)}`)
      if (current !== generation) return
      selected.value = detail
    }
    catch (e) { lastError = e.message }
    if (!isRunActive(selected.value?.status)) break
    progress.value = '实时连接中断，正在恢复…'
    await new Promise(resolve => setTimeout(resolve, Math.min(1000 * (attempt + 1), 5000)))
  }
  if (current !== generation) return
  try {
    const [detail, items, state] = await Promise.all([
      fetchJSON(`/api/agent-runs/${encodeURIComponent(runId)}`),
      fetchJSON('/api/agent-runs?limit=50'), fetchJSON('/api/agent-runs/summary'),
    ])
    if (current !== generation) return
    selected.value = detail
    runs.value = items
    summary.value = state
  } catch (e) { error.value = e.message }
  if (current !== generation) return
  if (isRunActive(selected.value?.status)) error.value = `实时连接暂时中断：${lastError || '请刷新任务列表以恢复'}`
}
async function submit() {
  if (submitting.value || !message.value.trim()) return
  submitting.value = true
  error.value = ''
  try {
    const body = { message: message.value.trim(), conversation_id: conversationId.value, use_rag: useRag.value, mode: mode.value }
    const signature = JSON.stringify(body)
    if (signature !== lastSubmissionSignature) {
      lastSubmissionSignature = signature
      lastSubmissionKey = crypto.randomUUID()
    }
    const run = await fetchJSON('/api/agent-runs', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': lastSubmissionKey },
      body: signature,
    })
    lastSubmissionSignature = ''
    lastSubmissionKey = ''
    message.value = ''
    conversationId.value = ''
    await refresh()
    await selectRun(run)
  } catch (e) { error.value = e.message }
  finally { submitting.value = false }
}
async function cancelRun() {
  if (!selected.value || cancelling.value) return
  const id = selected.value.run_id
  cancelling.value = true
  try {
    const run = await fetchJSON(`/api/agent-runs/${encodeURIComponent(id)}/cancel`, { method: 'POST' })
    if (selected.value?.run_id === id) await selectRun(run)
  }
  catch (e) { error.value = e.message }
  finally { cancelling.value = false }
}
async function reviewPlan(payload) {
  if (!selected.value || reviewing.value) return
  const id = selected.value.run_id
  const current = generation
  reviewing.value = true
  error.value = ''
  try {
    await fetchJSON(`/api/agent-runs/${encodeURIComponent(id)}/plan/review`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
    if (current === generation) await selectRun(await fetchJSON(`/api/agent-runs/${encodeURIComponent(id)}`))
  } catch (e) {
    if (current !== generation) return
    if (e.status === 409) {
      await selectRun(selected.value)
      error.value = '计划已在其他页面更新。本次操作未生效，请查看最新版本后重新决定。'
    } else error.value = e.message
  } finally { reviewing.value = false }
}
async function resumeRun() {
  if (!selected.value || resuming.value) return
  const id = selected.value.run_id
  const current = generation
  resuming.value = true
  error.value = ''
  try {
    const run = await fetchJSON(`/api/agent-runs/${encodeURIComponent(id)}/resume`, { method: 'POST' })
    if (current === generation) { await refresh(); await selectRun(run) }
  } catch (e) { if (current === generation) error.value = e.message }
  finally { resuming.value = false }
}
async function pollReview() {
  if (selected.value?.status !== 'awaiting_plan_review' || reviewing.value) return
  const current = generation
  try {
    const detail = await fetchJSON(`/api/agent-runs/${encodeURIComponent(selected.value.run_id)}`)
    if (current !== generation) return
    if (detail.status !== 'awaiting_plan_review' || detail.plan_version !== selected.value.plan_version) await selectRun(detail)
  } catch { /* The visible refresh action provides an explicit retry after reconnect. */ }
}
function continueRun() {
  mode.value = selectedResearch.value && props.researchEnabled ? 'research' : 'chat'
  conversationId.value = selected.value?.conversation_id || ''
  message.value = ''
  composing.value = true
  nextTick(() => document.getElementById('native-run-message')?.focus())
}
function newRun() {
  generation++
  if (streamController) streamController.abort()
  selected.value = null
  events.value = []
  lastEventId = 0
  tokenText.value = ''
  message.value = ''
  conversationId.value = ''
  error.value = ''
  nextTick(() => dialog.value?.querySelector('textarea')?.focus())
}
function handleDialogKey(event) {
  if (event.key === 'Escape') { event.preventDefault(); emit('close'); return }
  if (event.key !== 'Tab') return
  const elements = [...dialog.value.querySelectorAll('button:not(:disabled), textarea:not(:disabled), input:not(:disabled), a[href], summary')].filter(element => element.getClientRects().length)
  const first = elements[0], last = elements.at(-1)
  if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.value)) { event.preventDefault(); last?.focus() }
  else if (!dialog.value.contains(document.activeElement) || (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog.value))) { event.preventDefault(); first?.focus() }
}
onMounted(() => { previousFocus = document.activeElement; dialog.value?.focus(); document.addEventListener('keydown', handleDialogKey); refresh(); reviewPoll = setInterval(pollReview, 4000) })
onUnmounted(() => { generation++; if (streamController) streamController.abort(); clearInterval(reviewPoll); document.removeEventListener('keydown', handleDialogKey); previousFocus?.focus() })
</script>

<style scoped>
.run-backdrop{position:fixed;inset:0;z-index:1200;display:grid;place-items:center;padding:20px;background:rgba(12,26,32,.58)}.run-shell{width:min(1100px,100%);height:min(820px,94vh);display:flex;flex-direction:column;overflow:hidden;border-radius:18px;background:var(--bg,#fff);box-shadow:0 28px 90px rgba(0,0,0,.28);color:var(--text,#183039)}.run-header{display:flex;justify-content:space-between;gap:20px;padding:20px 26px;border-bottom:1px solid var(--border,#dce5e8)}.run-header span{font-size:11px;font-weight:800;letter-spacing:.16em;color:#0f766e}.run-header h2{margin:5px 0;font-size:24px}.run-header p{margin:0;color:var(--text3,#667982);font-size:13px}.close{width:44px;height:44px;border:0;background:transparent;color:var(--text3,#667982);font-size:28px;cursor:pointer}.run-layout{display:grid;grid-template-columns:240px 1fr;flex:1;min-height:0}.run-list{padding:16px;border-right:1px solid var(--border,#dce5e8);overflow:auto}.new-run{width:100%;min-height:44px;border:0;border-radius:9px;background:#0f766e;color:#fff;font-weight:700;cursor:pointer}.list-label{display:flex;justify-content:space-between;align-items:center;margin:20px 0 8px;color:var(--text3,#667982);font-size:12px;font-weight:700}.list-label button{width:36px;height:36px;border:0;background:transparent;color:#0f766e;cursor:pointer}.run-item{display:block;width:100%;padding:10px;text-align:left;border:1px solid transparent;border-radius:9px;background:transparent;color:inherit;cursor:pointer}.run-item.active{background:#e7f7f3;border-color:#a7ddd2}.run-item strong,.run-item small{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.run-item strong{font-size:13px}.run-item small{margin-top:5px;color:var(--text3,#667982);font-size:11px}.empty-list{font-size:13px;color:var(--text3,#667982)}.run-main{min-width:0;min-height:0;display:flex;flex-direction:column}.run-detail{flex:1;overflow:auto;padding:22px 26px}.run-meta{display:flex;gap:28px;align-items:center;flex-wrap:wrap;margin-bottom:16px}.run-meta div{display:grid;gap:3px}.run-meta span,.request-card span{color:var(--text3,#667982);font-size:11px}.run-meta strong,.run-meta code{font-size:13px}.run-meta strong.completed{color:#0f766e}.run-meta strong.failed,.run-meta strong.interrupted{color:#b42318}.run-meta button{margin-left:auto;min-height:42px;padding:8px 14px;border-radius:8px;cursor:pointer}.stop{border:1px solid #e4aaa4;background:#fff5f3;color:#a6382a}.continue{border:1px solid #91cfc1;background:#f0fdfa;color:#0f766e}.request-card{padding:14px;border:1px solid var(--border,#dce5e8);border-radius:10px}.request-card p{margin:5px 0 0;white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px}.event-list{margin:18px 0}.event-row{display:flex;align-items:center;gap:10px;min-height:31px;color:var(--text3,#667982);font-size:12px}.event-mark{width:7px;height:7px;border-radius:50%;background:#0f766e;flex:none}.event-row small{margin-left:auto;font-size:11px}.running{display:flex;gap:9px;align-items:center;color:#0f766e;font-size:13px}.pulse{width:9px;height:9px;border-radius:50%;background:#0f766e;animation:pulse 1.2s ease-in-out infinite}@keyframes pulse{50%{opacity:.3}}.answer{margin-top:16px;padding:16px;border-radius:10px;background:#f5faf9}.answer strong{font-size:12px;color:#0f766e}.answer pre{margin:8px 0 0;white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;line-height:1.6;font-size:14px}.welcome{text-align:center;margin:10vh auto;color:var(--text3,#667982)}.welcome span{font-size:48px;color:#0f766e}.welcome h3{color:var(--text,#183039)}.run-compose{padding:15px 20px;border-top:1px solid var(--border,#dce5e8)}.run-compose>label{display:block;margin-bottom:6px;font-size:12px;font-weight:700}.run-compose textarea{box-sizing:border-box;width:100%;padding:10px;border:1px solid var(--border,#dce5e8);border-radius:9px;resize:vertical;font:inherit;font-size:14px;line-height:1.5}.compose-bottom{display:flex;gap:14px;align-items:center;margin-top:8px;color:var(--text3,#667982);font-size:12px}.rag-option{display:flex;align-items:center;gap:5px}.compose-bottom button{margin-left:auto;min-height:44px;padding:9px 18px;border:0;border-radius:8px;background:#0f766e;color:#fff;font-weight:700;cursor:pointer}.compose-bottom button:disabled{opacity:.5;cursor:not-allowed}.error{margin:0 0 8px;color:#b42318;font-size:12px}@media(max-width:720px){.run-backdrop{padding:0}.run-shell{height:100vh;border-radius:0}.run-layout{grid-template-columns:1fr}.run-list{max-height:125px;border-right:0;border-bottom:1px solid var(--border,#dce5e8);white-space:nowrap}.run-item{display:inline-block;width:150px}.run-detail{padding:16px}.run-header{padding:16px}}
.result-reason{padding:12px 14px;border:1px solid #e4aaa4;border-radius:8px;background:#fff5f3;color:#a6382a;font-size:13px;white-space:pre-wrap;overflow-wrap:anywhere}
</style>

<style src="../assets/research.css"></style>
