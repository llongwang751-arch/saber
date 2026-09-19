<template>
  <section v-if="items.length || error" class="approvals" aria-label="待审批操作">
    <p v-if="error" role="alert">{{ error }} <button @click="refresh">重试</button></p>
    <article v-for="item in items" :key="item.request_id">
      <strong>操作需要你批准：{{ item.tool_name }}</strong>
      <p>批准后将执行以下参数对应的操作。审批十分钟内有效。</p>
      <pre>{{ JSON.stringify(item.params, null, 2) }}</pre>
      <button :disabled="Boolean(busy) || chat.loading" @click="decide(item, true)">{{ busy === item.request_id ? '处理中…' : '批准并执行' }}</button>
      <button :disabled="Boolean(busy) || chat.loading" @click="decide(item, false)">拒绝</button>
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
  const current = ++generation, id = sessions.currentId
  if (!auth.loggedIn || !id) { items.value = []; error.value = ''; return }
  try {
    const rows = await fetchJSON('/api/tool-approvals')
    if (current !== generation) return
    items.value = rows.filter(row => row.session_id === id)
    error.value = ''
  } catch (e) { if (current === generation) error.value = '无法加载审批：' + e.message }
}
async function decide(item, approved) {
  busy.value = item.request_id
  try {
    const data = await fetchJSON(`/api/tool-approvals/${item.request_id}/decision`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ approved }),
    })
    const result = data.result
    sessions.addMessage(item.session_id, { role: 'ai', steps: [], answer: approved
      ? (result?.success ? String(result.payload || '操作完成') : '操作未完成：' + (result?.error?.message || data.status))
      : '已拒绝此操作。' })
    await refresh()
  } catch (e) { error.value = '审批失败：' + e.message }
  finally { busy.value = '' }
}
watch(() => [auth.loggedIn, sessions.currentId, chat.loading], refresh, { immediate: true })
const timer = setInterval(() => { if (!busy.value) refresh() }, 10000)
onUnmounted(() => { generation++; clearInterval(timer) })
</script>
<style scoped>
.approvals { padding: 12px 20px; max-height: 35vh; overflow: auto; border-top: 1px solid var(--border); }
article { padding: 12px; border: 1px solid var(--border); border-radius: 8px; margin-bottom: 8px; }
p { font-size: 13px; margin: 8px 0; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 150px; overflow: auto; }
button { min-height: 44px; padding: 8px 14px; margin-right: 8px; cursor: pointer; }
button:disabled { opacity: .6; cursor: wait; }
button:focus-visible { outline: 2px solid currentColor; outline-offset: 3px; }
</style>
