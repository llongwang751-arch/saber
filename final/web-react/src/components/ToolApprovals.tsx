import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchJSON } from '../api/client'
import { useSessions } from '../stores/sessions'
import { useAuth } from '../stores/auth'
import { useChat } from '../stores/chat'

// 工具级 HITL 审批：10s 轮询当前会话的待审批项；批准/拒绝后把结果写入会话消息流。
export default function ToolApprovals() {
  const [items, setItems] = useState<any[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const generation = useRef(0)
  const busyRef = useRef('')

  const refresh = useCallback(async () => {
    const current = ++generation.current
    const sessionId = useSessions.getState().currentId
    const loggedIn = !!useAuth.getState().token
    if (!loggedIn || !sessionId) { setItems([]); setError(''); return }
    try {
      const rows = await fetchJSON('/api/tool-approvals')
      if (current !== generation.current) return
      setItems(rows.filter((row: any) => row.session_id === sessionId))
      setError('')
    } catch (e: any) {
      if (current === generation.current) setError('无法加载审批：' + e.message)
    }
  }, [])

  const currentId = useSessions(s => s.currentId)
  const loading = useChat(s => s.loading)
  const loggedIn = useAuth(s => !!s.token)

  useEffect(() => { refresh() }, [refresh, loggedIn, currentId, loading])

  useEffect(() => {
    const timer = setInterval(() => { if (!busyRef.current) refresh() }, 10000)
    return () => { generation.current++; clearInterval(timer) }
  }, [refresh])

  async function decide(item: any, approved: boolean) {
    setBusy(item.request_id)
    busyRef.current = item.request_id
    try {
      const data = await fetchJSON(`/api/tool-approvals/${item.request_id}/decision`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ approved }),
      })
      const result = data.result
      useSessions.getState().addMessage(item.session_id, {
        role: 'ai', steps: [], answer: approved
          ? (result?.success ? String(result.payload || '操作完成') : '操作未完成：' + (result?.error?.message || data.status))
          : '已拒绝此操作。',
      })
      await refresh()
    } catch (e: any) {
      setError('审批失败：' + e.message)
    } finally {
      setBusy('')
      busyRef.current = ''
    }
  }

  if (!items.length && !error) return null

  return (
    <section className="approvals" aria-label="待审批操作">
      {error && <p role="alert">{error} <button onClick={() => refresh()}>重试</button></p>}
      {items.map(item => (
        <article key={item.request_id}>
          <strong>操作需要你批准：{item.tool_name}</strong>
          <p>批准后将执行以下参数对应的操作。审批十分钟内有效。</p>
          <pre>{JSON.stringify(item.params, null, 2)}</pre>
          <button disabled={Boolean(busy) || loading} onClick={() => decide(item, true)}>{busy === item.request_id ? '处理中…' : '批准并执行'}</button>
          <button disabled={Boolean(busy) || loading} onClick={() => decide(item, false)}>拒绝</button>
        </article>
      ))}
    </section>
  )
}
