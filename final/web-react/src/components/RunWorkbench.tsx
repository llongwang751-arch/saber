import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { apiFetch, fetchJSON } from '../api/client'
import { readSSE } from '../lib/sse'
import { isRunActive, isRunObservable, researchResult, runEventLabel } from '../utils/research'
import PlanReview from './PlanReview'
import ResearchReport from './ResearchReport'

// 研究工作台 / 运行记录：任务列表、事件回放 + SSE 增量跟随、计划审批（CAS 409 冲突处理）、
// 断线自动重连、任务取消与恢复、报告渲染与下载。逻辑逐字移植自 Vue 版 RunWorkbench.vue，
// Vue ref → useState/useRef：跨 await 读取的响应式值一律通过 ref 镜像访问，避免闭包过期。
type Mode = 'research' | 'chat'

const statusLabel = (status?: string) => ({ pending: '排队中', running: '执行中', awaiting_plan_review: '待审核计划', cancelling: '停止中', completed: '已完成', partial: '部分完成', cancelled: '已停止', failed: '执行失败', interrupted: '已中断' } as Record<string, string>)[status || ''] || status || '未知'
const stepStatusLabel = (status?: string) => ({ pending: '等待执行', running: '执行中', completed: '已完成', failed: '失败', partial: '部分完成', code_only: '仅生成代码', skipped: '已跳过', cancelled: '已停止' } as Record<string, string>)[status || ''] || status
const kindLabel = (kind?: string) => ({ research: '深度研究', chat: '后台任务', chat_sync: '即时聊天', chat_stream: '流式聊天', recovery: '检查点恢复' } as Record<string, string>)[kind || ''] || '任务'
const dateLabel = (value?: number) => value ? new Date(value * 1000).toLocaleDateString('zh-CN') : ''
const timeLabel = (value?: number) => value ? new Date(value * 1000).toLocaleTimeString('zh-CN') : ''
const eventLabel = runEventLabel

export default function RunWorkbench(props: { initialMode?: Mode; researchEnabled?: boolean; onClose: () => void }) {
  const [mode, setMode] = useState<Mode>(props.researchEnabled === false ? 'chat' : (props.initialMode || 'research'))
  const dialog = useRef<HTMLElement>(null)
  const [reviewing, setReviewing] = useState(false)
  const reviewingRef = useRef(false)
  function setReviewingBoth(value: boolean) { reviewingRef.current = value; setReviewing(value) }
  const [cancelling, setCancelling] = useState(false)
  const [resuming, setResuming] = useState(false)
  const [composing, setComposing] = useState(false)
  const [runs, setRuns] = useState<any[]>([])
  const [summary, setSummary] = useState<any>(null)
  const [selected, setSelected] = useState<any>(null)
  const [events, setEvents] = useState<any[]>([])
  const [message, setMessage] = useState('')
  const [conversationId, setConversationId] = useState('')
  const [useRag, setUseRag] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [progress, setProgress] = useState('等待执行事件…')
  const [tokenText, setTokenText] = useState('')

  const selectedRef = useRef<any>(null)
  const generation = useRef(0)
  const lastEventId = useRef(0)
  const streamController = useRef<AbortController | null>(null)
  const lastSubmissionSignature = useRef('')
  const lastSubmissionKey = useRef('')
  const previousFocus = useRef<HTMLElement | null>(null)

  function setSelectedBoth(value: any) {
    selectedRef.current = value
    setSelected(value)
  }

  const selectedResearch = !!(selected && (selected.kind === 'research' || selected.mode === 'research' || !!selected.plan))
  const visibleEvents = useMemo(() => events.filter(event => !['token', 'done'].includes(event.type)).slice(-80), [events])
  const result = useMemo(() => researchResult(selected), [selected])
  const answer = result.report_markdown || result.answer || tokenText
  const sources = useMemo(() => {
    const byId = new Map<string, any>()
    for (const source of [
      ...events.filter(event => event.type === 'source_found').map(event => event.data),
      ...(selected?.sources || []),
      ...(result.sources || []),
    ]) {
      if (source?.source_id) byId.set(source.source_id, source)
    }
    return [...byId.values()]
  }, [events, selected, result])
  const artifacts = result.artifacts?.length ? result.artifacts : selected?.artifacts || []
  const researchSteps = useMemo(() => {
    const steps = new Map<string, any>((selected?.plan?.steps || []).map((step: any) => [step.id, { ...step, status: 'pending' }]))
    for (const event of events) {
      const data = event.data || {}
      const id = data.step_id || data.id
      if (!steps.has(id)) continue
      if (event.type === 'node_start') steps.set(id, { ...steps.get(id), status: 'running' })
      if (event.type === 'node_done') steps.set(id, { ...steps.get(id), status: data.status || 'completed' })
      if (event.type === 'research_round') steps.set(id, { ...steps.get(id), rounds: data.round })
    }
    for (const step of result.steps || []) steps.set(step.id, { ...steps.get(step.id), ...step })
    return [...steps.values()]
  }, [selected, events, result])

  function applyEvent(event: any) {
    if (event.event_id <= lastEventId.current) return
    lastEventId.current = event.event_id
    if (event.type !== 'token') setEvents(prev => [...prev.slice(-199), event])
    if (event.type === 'token') setTokenText(prev => prev + String(event.data?.content || ''))
    if (event.type !== 'token') setProgress(eventLabel(event))
    if (event.type.startsWith('plan_') && selectedRef.current) {
      const data = event.data || {}
      setSelectedBoth({
        ...selectedRef.current,
        ...(data.plan ? { plan: data.plan } : {}),
        ...(data.version ? { plan_version: data.version } : {}),
        ...(data.status ? { status: data.status } : {}),
      })
    }
  }

  const refresh = useCallback(async (reloadSelected = false) => {
    try {
      const [items, state] = await Promise.all([
        fetchJSON('/api/agent-runs?limit=50'), fetchJSON('/api/agent-runs/summary'),
      ])
      setRuns(items)
      setSummary(state)
      if (reloadSelected && selectedRef.current) await selectRun(selectedRef.current)
    } catch (e: any) { setError(e.message) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function selectRun(item: any) {
    setComposing(false)
    generation.current++
    if (streamController.current) streamController.current.abort()
    const current = generation.current
    setSelectedBoth(item)
    setTimeout(() => { const detail = dialog.current?.querySelector('.run-detail'); if (detail) detail.scrollTop = 0 }, 0)
    setEvents([])
    lastEventId.current = 0
    setTokenText('')
    setError('')
    try {
      while (current === generation.current) {
        const history = await fetchJSON(`/api/agent-runs/${encodeURIComponent(item.run_id)}/events?after=${lastEventId.current}&limit=500`)
        if (current !== generation.current) return
        history.forEach(applyEvent)
        if (history.length < 500 || history.some((event: any) => event.type === 'done')) break
      }
      const detail = await fetchJSON(`/api/agent-runs/${encodeURIComponent(item.run_id)}`)
      if (current !== generation.current) return
      setSelectedBoth(detail)
      if (isRunObservable(selectedRef.current.status)) followRun(item.run_id, current)
    } catch (e: any) { if (current === generation.current) setError(e.message) }
  }

  async function followRun(runId: string, current: number) {
    let lastError = ''
    for (let attempt = 0; current === generation.current && attempt < 5; attempt++) {
      streamController.current = new AbortController()
      const cursor = lastEventId.current
      try {
        const response = await apiFetch(`/api/agent-runs/${encodeURIComponent(runId)}/stream?after=${cursor}`, { signal: streamController.current.signal })
        if (!response.ok || !response.body) throw new Error(`事件流 HTTP ${response.status}`)
        await readSSE(response, (type, data, metadata) => {
          if (current !== generation.current) return false
          const id = Number(metadata.id)
          if (id) applyEvent({ event_id: id, type, data, created_at: Date.now() / 1000 })
        })
      } catch (e: any) {
        if (e?.name === 'AbortError' || current !== generation.current) return
        lastError = e.message
      }
      if (current !== generation.current) return
      try {
        const detail = await fetchJSON(`/api/agent-runs/${encodeURIComponent(runId)}`)
        if (current !== generation.current) return
        setSelectedBoth(detail)
      } catch (e: any) { lastError = e.message }
      if (!isRunActive(selectedRef.current?.status)) break
      setProgress('实时连接中断，正在恢复…')
      await new Promise(resolve => setTimeout(resolve, Math.min(1000 * (attempt + 1), 5000)))
    }
    if (current !== generation.current) return
    try {
      const [detail, items, state] = await Promise.all([
        fetchJSON(`/api/agent-runs/${encodeURIComponent(runId)}`),
        fetchJSON('/api/agent-runs?limit=50'), fetchJSON('/api/agent-runs/summary'),
      ])
      if (current !== generation.current) return
      setSelectedBoth(detail)
      setRuns(items)
      setSummary(state)
    } catch (e: any) { setError(e.message) }
    if (current !== generation.current) return
    if (isRunActive(selectedRef.current?.status)) setError(`实时连接暂时中断：${lastError || '请刷新任务列表以恢复'}`)
  }

  async function submit() {
    if (submitting || !message.trim()) return
    setSubmitting(true)
    setError('')
    try {
      const body = { message: message.trim(), conversation_id: conversationId, use_rag: useRag, mode }
      const signature = JSON.stringify(body)
      if (signature !== lastSubmissionSignature.current) {
        lastSubmissionSignature.current = signature
        lastSubmissionKey.current = crypto.randomUUID()
      }
      const run = await fetchJSON('/api/agent-runs', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': lastSubmissionKey.current },
        body: signature,
      })
      lastSubmissionSignature.current = ''
      lastSubmissionKey.current = ''
      setMessage('')
      setConversationId('')
      await refresh()
      await selectRun(run)
    } catch (e: any) { setError(e.message) }
    finally { setSubmitting(false) }
  }

  async function cancelRun() {
    if (!selected || cancelling) return
    const id = selected.run_id
    setCancelling(true)
    try {
      const run = await fetchJSON(`/api/agent-runs/${encodeURIComponent(id)}/cancel`, { method: 'POST' })
      if (selectedRef.current?.run_id === id) await selectRun(run)
    } catch (e: any) { setError(e.message) }
    finally { setCancelling(false) }
  }

  async function reviewPlan(payload: any) {
    if (!selectedRef.current || reviewingRef.current) return
    const id = selectedRef.current.run_id
    const current = generation.current
    setReviewingBoth(true)
    setError('')
    try {
      await fetchJSON(`/api/agent-runs/${encodeURIComponent(id)}/plan/review`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
      if (current === generation.current) await selectRun(await fetchJSON(`/api/agent-runs/${encodeURIComponent(id)}`))
    } catch (e: any) {
      if (current !== generation.current) return
      if (e.status === 409) {
        await selectRun(selectedRef.current)
        setError('计划已在其他页面更新。本次操作未生效，请查看最新版本后重新决定。')
      } else setError(e.message)
    } finally { setReviewingBoth(false) }
  }

  async function resumeRun() {
    if (!selectedRef.current || resuming) return
    const id = selectedRef.current.run_id
    const current = generation.current
    setResuming(true)
    setError('')
    try {
      const run = await fetchJSON(`/api/agent-runs/${encodeURIComponent(id)}/resume`, { method: 'POST' })
      if (current === generation.current) { await refresh(); await selectRun(run) }
    } catch (e: any) { if (current === generation.current) setError(e.message) }
    finally { setResuming(false) }
  }

  const pollReview = useCallback(async () => {
    const current = selectedRef.current
    if (current?.status !== 'awaiting_plan_review' || reviewingRef.current) return
    const gen = generation.current
    try {
      const detail = await fetchJSON(`/api/agent-runs/${encodeURIComponent(current.run_id)}`)
      if (gen !== generation.current) return
      if (detail.status !== 'awaiting_plan_review' || detail.plan_version !== selectedRef.current.plan_version) await selectRun(detail)
    } catch { /* The visible refresh action provides an explicit retry after reconnect. */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function continueRun() {
    setMode(selectedResearch && props.researchEnabled !== false ? 'research' : 'chat')
    setConversationId(selectedRef.current?.conversation_id || '')
    setMessage('')
    setComposing(true)
    setTimeout(() => document.getElementById('native-run-message')?.focus(), 0)
  }

  function newRun() {
    generation.current++
    if (streamController.current) streamController.current.abort()
    setSelectedBoth(null)
    setEvents([])
    lastEventId.current = 0
    setTokenText('')
    setMessage('')
    setConversationId('')
    setError('')
    setTimeout(() => dialog.current?.querySelector('textarea')?.focus(), 0)
  }

  const handleDialogKey = useCallback((event: KeyboardEvent) => {
    if (event.key === 'Escape') { event.preventDefault(); props.onClose(); return }
    if (event.key !== 'Tab') return
    const root = dialog.current
    if (!root) return
    const elements = [...root.querySelectorAll('button:not(:disabled), textarea:not(:disabled), input:not(:disabled), a[href], summary')].filter(element => element.getClientRects().length) as HTMLElement[]
    const first = elements[0]
    const last = elements.at(-1)
    if (event.shiftKey && (document.activeElement === first || document.activeElement === root)) { event.preventDefault(); last?.focus() }
    else if (!root.contains(document.activeElement) || (!event.shiftKey && (document.activeElement === last || document.activeElement === root))) { event.preventDefault(); first?.focus() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    previousFocus.current = document.activeElement as HTMLElement | null
    dialog.current?.focus()
    document.addEventListener('keydown', handleDialogKey)
    refresh()
    const reviewPoll = setInterval(() => { pollReview() }, 4000)
    return () => {
      generation.current++
      if (streamController.current) streamController.current.abort()
      clearInterval(reviewPoll)
      document.removeEventListener('keydown', handleDialogKey)
      previousFocus.current?.focus()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="run-backdrop" onClick={e => { if (e.target === e.currentTarget) props.onClose() }}>
      <section ref={dialog as any} className="run-shell" role="dialog" aria-modal="true" aria-label="Saber 运行记录" tabIndex={-1}>
        <header className="run-header">
          <div>
            <span>AGI-SABER / AGENT WORKSPACE</span>
            <h2>{mode === 'research' ? '研究工作台' : '运行记录'}</h2>
            <p>提出目标，审核计划，追踪研究过程与证据。关闭面板后任务继续执行。</p>
          </div>
          <button type="button" className="close" aria-label="关闭运行记录" onClick={props.onClose}>×</button>
        </header>
        <div className="run-layout">
          <aside className="run-list" aria-label="最近任务">
            <button type="button" className="new-run" onClick={newRun}>＋ 新建任务</button>
            <div className="list-label">
              <span>最近运行 {summary && <small>· 活跃 {summary.active}/{summary.capacity}</small>}</span>
              <button type="button" onClick={() => refresh(true)} aria-label="刷新任务列表">↻</button>
            </div>
            {runs.map(item => (
              <button
                key={item.run_id}
                type="button"
                className={`run-item${item.run_id === selected?.run_id ? ' active' : ''}`}
                onClick={() => selectRun(item)}
              >
                <strong>{item.message}</strong>
                <small>{kindLabel(item.kind)} · {statusLabel(item.status)} · {dateLabel(item.created_at)}</small>
              </button>
            ))}
            {!runs.length && <p className="empty-list">暂无任务记录。</p>}
          </aside>
          <main className="run-main">
            {error && <p className="error run-error" role="alert">{error}</p>}
            <div className="run-detail">
              {selected && (
                <div className="run-meta">
                  <div><span>运行状态</span><strong className={selected.status}>{statusLabel(selected.status)}</strong></div>
                  <div><span>会话</span><code>{(selected.conversation_id || '').slice(0, 12)}</code></div>
                  {isRunObservable(selected.status) ? (
                    <button type="button" className="stop" disabled={cancelling || selected.status === 'cancelling'} onClick={cancelRun}>
                      {cancelling || selected.status === 'cancelling' ? '正在停止…' : '请求停止'}
                    </button>
                  ) : selected.status === 'interrupted' && selectedResearch && selected.plan_status === 'approved' ? (
                    <button type="button" className="continue" disabled={resuming} onClick={resumeRun}>{resuming ? '正在恢复…' : '继续研究'}</button>
                  ) : (
                    <button type="button" className="continue" onClick={continueRun}>继续此会话</button>
                  )}
                </div>
              )}
              {selected && (
                <div className="request-card"><span>任务目标</span><p>{selected.message}</p></div>
              )}
              {selected?.plan && (
                <PlanReview
                  key={selected.run_id}
                  plan={selected.plan}
                  version={selected.plan_version || 1}
                  status={selected.status}
                  reviewStatus={selected.plan_status}
                  busy={reviewing}
                  onReview={reviewPlan}
                />
              )}
              {selectedResearch && researchSteps.length > 0 && (
                <div className="research-step-progress" aria-label="研究步骤进度">
                  {researchSteps.map(step => (
                    <div key={step.id}>
                      <strong>{step.title || step.id}</strong>
                      <span>{stepStatusLabel(step.status)}{step.rounds ? ` · ${step.rounds} 轮` : ''}</span>
                    </div>
                  ))}
                </div>
              )}
              {events.length > 0 && (
                <details className="event-disclosure" open={!selectedResearch}>
                  <summary>执行过程 · {events.length} 条记录</summary>
                  <div className="event-list" aria-label="执行事件">
                    {visibleEvents.map(event => (
                      <div key={event.event_id} className="event-row">
                        <span className="event-mark"></span>
                        <span>{eventLabel(event)}</span>
                        <small>{timeLabel(event.created_at)}</small>
                      </div>
                    ))}
                  </div>
                </details>
              )}
              {isRunActive(selected?.status) && (
                <div className="running" role="status"><span className="pulse"></span><span>{progress}</span></div>
              )}
              {selected?.status === 'awaiting_plan_review' && (
                <p className="research-notice" role="status">研究已暂停，等待审核计划。</p>
              )}
              {selected?.result?.reason && <p className="result-reason" role="status">{selected.result.reason}</p>}
              {selectedResearch ? (
                <ResearchReport result={result} report={answer} sources={sources} artifacts={artifacts} />
              ) : answer ? (
                <div className="answer"><strong>AGI-saber 结果</strong><pre>{answer}</pre></div>
              ) : null}
              {!selected && !error && (
                <div className="welcome">
                  <span aria-hidden="true">◇</span>
                  <h3>{mode === 'research' ? '从一个值得研究的问题开始' : '运行一个可追踪的长任务'}</h3>
                  <p>{mode === 'research' ? '先生成可修改的计划，再检索资料、补充证据，最后交付带引用的报告。' : '每次运行都有独立状态与事件记录，页面断开后仍可查看结果。'}</p>
                </div>
              )}
            </div>
            {(!selected || composing) && (
              <form className="run-compose" onSubmit={e => { e.preventDefault(); submit() }}>
                {props.researchEnabled !== false && (
                  <div className="run-mode" role="group" aria-label="任务模式">
                    <button type="button" aria-pressed={mode === 'research'} onClick={() => setMode('research')}>深度研究</button>
                    <button type="button" aria-pressed={mode === 'chat'} onClick={() => setMode('chat')}>普通任务</button>
                    <span>{mode === 'research' ? '计划 → 审核 → 研究 → 报告' : '直接执行任务'}</span>
                  </div>
                )}
                <label htmlFor="native-run-message">任务目标</label>
                <textarea
                  id="native-run-message"
                  value={message}
                  maxLength={20000}
                  rows={3}
                  placeholder="描述要完成的任务和输出要求"
                  disabled={submitting}
                  onChange={e => setMessage(e.target.value)}
                  onKeyDown={e => { if (e.ctrlKey && e.key === 'Enter') { e.preventDefault(); submit() } }}
                />
                <div className="compose-bottom">
                  <label className="rag-option"><input type="checkbox" checked={useRag} onChange={e => setUseRag(e.target.checked)} /> 使用 Saber 知识库</label>
                  {conversationId && <span>将继续当前会话</span>}
                  <button type="submit" disabled={submitting || !message.trim()}>
                    {submitting ? '提交中…' : mode === 'research' ? '生成研究计划' : '开始运行'}
                  </button>
                </div>
              </form>
            )}
          </main>
        </div>
      </section>
    </div>
  )
}
