<template>
  <section v-if="items.length || error" class="task-recovery" aria-label="任务恢复">
    <p v-if="error" role="alert">{{ error }} <button @click="refresh">刷新</button></p>
    <article v-for="item in items" :key="item.task_id">
      <strong>未完成任务</strong>
      <p>{{ item.state.query }}</p>
      <p>已完成的步骤会跳过；结果不确定的操作会暂停核对。</p>
      <button :disabled="!!busy || chat.loading" @click="resume(item)">
        {{ busy === item.task_id ? '恢复中…' : '恢复任务' }}
      </button>
    </article>
  </section>
</template>
<script setup>
import { ref, watch, onUnmounted } from 'vue'
import { fetchJSON } from '../api/client'
import { useSessions } from '../stores/sessions'
import { useAuth } from '../stores/auth'
import { useChat } from '../stores/chat'
const sessions = useSessions(), auth = useAuth(), chat = useChat()
const items = ref([]), error = ref(''), busy = ref('')
let generation = 0
let mounted = true
async function refresh() {
  const current = ++generation, sessionId = sessions.currentId
  if (!auth.loggedIn || !sessionId) { items.value = []; error.value = ''; return }
  try {
    const data = await fetchJSON('/api/tasks')
    if (current !== generation) return
    items.value = (data.items || []).filter(item => item.state?.recovery?.schema === 1
      && item.state.recovery.conversation_id === sessionId
      && ['running', 'interrupted'].includes(item.state.status))
    error.value = ''
  } catch (e) { if (current === generation) error.value = '无法加载恢复任务：' + e.message }
}
async function resume(item) {
  const sessionId = sessions.currentId
  busy.value = item.task_id
  try {
    let run = await fetchJSON('/api/agent-runs/recover', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID() },
      body: JSON.stringify({ task_id: item.task_id, conversation_id: sessionId }),
    })
    for (let i = 0; mounted && i < 120 && ['pending', 'running', 'cancelling'].includes(run.status); i++) {
      await new Promise(resolve => setTimeout(resolve, 1000))
      run = await fetchJSON(`/api/agent-runs/${encodeURIComponent(run.run_id)}`)
    }
    if (!mounted) return
    let outcomeError = ''
    if (run.status === 'completed') {
      sessions.addMessage(sessionId, { role: 'ai', steps: [], answer: String(run.result?.response?.answer || '任务恢复结束') })
    } else if (['pending', 'running', 'cancelling'].includes(run.status)) {
      outcomeError = '任务仍在后台执行，可在“后台任务”查看进度。'
    } else {
      outcomeError = `任务恢复状态：${run.status}。${run.result?.reason || '请在“后台任务”查看执行记录与检查点。'}`
    }
    await refresh()
    if (outcomeError) error.value = outcomeError
  } catch (e) { error.value = '任务未恢复：' + e.message }
  finally { busy.value = '' }
}
watch(() => [auth.loggedIn, sessions.currentId, chat.loading], refresh, { immediate: true })
const timer = setInterval(() => { if (!busy.value && !chat.loading) refresh() }, 15000)
onUnmounted(() => { mounted = false; generation++; clearInterval(timer) })
</script>
<style scoped>
.task-recovery { padding: 8px 20px; max-height: 25vh; overflow: auto; border-top: 1px solid var(--border); }
article { padding: 10px; border: 1px solid var(--border); border-radius: 8px; margin-bottom: 8px; }
p { margin: 6px 0; font-size: 13px; overflow-wrap: anywhere; }
button { min-height: 44px; padding: 8px 14px; cursor: pointer; }
button:disabled { opacity: .6; cursor: wait; }
</style>
