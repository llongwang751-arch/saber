import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchJSON } from '../api/client'
import { useSessions } from '../stores/sessions'
import { useAuth } from '../stores/auth'
import { useChat } from '../stores/chat'

// 未完成任务恢复：15s 轮询当前会话的可恢复任务；恢复后轮询运行状态并把结果写入会话。
export default function TaskRecovery() {
  const [items, setItems] = useState<any[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const generation = useRef(0)
  const mounted = useRef(true)
  const busyRef = useRef('')

  const refresh = useCallback(async () => {
    const current = ++generation.current
    const sessionId = useSessions.getState().currentId
    const loggedIn = !!useAuth.getState().token
    if (!loggedIn || !sessionId) { setItems([]); setError(''); return }
    try {
      const data = await fetchJSON('/api/tasks')
      if (current !== generation.current) return
      setItems((data.items || []).filter((item: any) => item.state?.recovery?.schema === 1
        && item.state.recovery.conversation_id === sessionId
        && ['running', 'interrupted'].includes(item.state.status)))
      setError('')
    } catch (e: any) {
      if (current === generation.current) setError('无法加载恢复任务：' + e.message)
    }
  }, [])

  const currentId = useSessions(s => s.currentId)
  const loading = useChat(s => s.loading)
  const loggedIn = useAuth(s => !!s.token)

  useEffect(() => { refresh() }, [refresh, loggedIn, currentId, loading])

  useEffect(() => {
    mounted.current = true
    const timer = setInterval(() => { if (!busyRef.current && !useChat.getState().loading) refresh() }, 15000)
    return () => { mounted.current = false; generation.current++; clearInterval(timer) }
  }, [refresh])

  async function resume(item: any) {
    const sessionId = useSessions.getState().currentId
    setBusy(item.task_id)
    busyRef.current = item.task_id
    try {
      let run = await fetchJSON('/api/agent-runs/recover', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID() },
        body: JSON.stringify({ task_id: item.task_id, conversation_id: sessionId }),
      })
      for (let i = 0; mounted.current && i < 120 && ['pending', 'running', 'cancelling'].includes(run.status); i++) {
        await new Promise(resolve => setTimeout(resolve, 1000))
        run = await fetchJSON(`/api/agent-runs/${encodeURIComponent(run.run_id)}`)
      }
      if (!mounted.current) return
      let outcomeError = ''
      if (run.status === 'completed') {
        useSessions.getState().addMessage(sessionId!, { role: 'ai', steps: [], answer: String(run.result?.response?.answer || '任务恢复结束') })
      } else if (['pending', 'running', 'cancelling'].includes(run.status)) {
        outcomeError = '任务仍在后台执行，可在“后台任务”查看进度。'
      } else {
        outcomeError = `任务恢复状态：${run.status}。${run.result?.reason || '请在“后台任务”查看执行记录与检查点。'}`
      }
      await refresh()
      if (outcomeError) setError(outcomeError)
    } catch (e: any) {
      setError('任务未恢复：' + e.message)
    } finally {
      setBusy('')
      busyRef.current = ''
    }
  }

  if (!items.length && !error) return null

  return (
    <section className="task-recovery" aria-label="任务恢复">
      {error && <p role="alert">{error} <button onClick={() => refresh()}>刷新</button></p>}
      {items.map(item => (
        <article key={item.task_id}>
          <strong>未完成任务</strong>
          <p>{item.state.query}</p>
          <p>已完成的步骤会跳过；结果不确定的操作会暂停核对。</p>
          <button disabled={!!busy || loading} onClick={() => resume(item)}>
            {busy === item.task_id ? '恢复中…' : '恢复任务'}
          </button>
        </article>
      ))}
    </section>
  )
}
