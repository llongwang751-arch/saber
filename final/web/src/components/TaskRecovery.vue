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
    const data = await fetchJSON(`/api/tasks/${encodeURIComponent(item.task_id)}/resume`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation_id: sessionId }),
    })
    sessions.addMessage(sessionId, { role: 'ai', steps: [], answer: String(data.result || '任务恢复结束') })
    await refresh()
  } catch (e) { error.value = '任务未恢复：' + e.message }
  finally { busy.value = '' }
}
watch(() => [auth.loggedIn, sessions.currentId, chat.loading], refresh, { immediate: true })
const timer = setInterval(() => { if (!busy.value && !chat.loading) refresh() }, 15000)
onUnmounted(() => { generation++; clearInterval(timer) })
</script>
<style scoped>
.task-recovery { padding: 8px 20px; max-height: 25vh; overflow: auto; border-top: 1px solid var(--border); }
article { padding: 10px; border: 1px solid var(--border); border-radius: 8px; margin-bottom: 8px; }
p { margin: 6px 0; font-size: 13px; overflow-wrap: anywhere; }
button { min-height: 44px; padding: 8px 14px; cursor: pointer; }
button:disabled { opacity: .6; cursor: wait; }
</style>
