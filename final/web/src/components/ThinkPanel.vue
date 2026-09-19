<template>
  <div v-if="hasThink" class="think-wrap">
    <div class="think-header" :class="{ open }" @click="open = !open">
      <span class="think-icon">▶</span>
      <span class="think-label">思考过程</span>
      <span class="think-badge" :class="badgeClass">{{ badgeText }}</span>
    </div>
    <div class="think-body" :class="{ open }">
      <div v-if="msg.sandbox" class="step-row">
        <span class="step-type step-action">📦 沙箱</span>
        <div class="step-content">工作目录已就绪：{{ msg.sandbox }}（产物将生成于此，宿主机桌面可见）</div>
      </div>

      <div v-for="(s, i) in (msg.steps || [])" :key="i" class="step-row">
        <span class="step-type" :class="stepCls(s.type)">{{ stepLabel(s.type) }}</span>
        <div class="step-content">
          {{ s.content }}
          <div v-if="s.params && Object.keys(s.params).length" class="step-params">{{ JSON.stringify(s.params) }}</div>
        </div>
      </div>

      <div v-if="msg.toolCall" class="tool-call-block">
        <div class="tool-call-row"><span class="tool-call-label">工具</span><span class="tool-call-val">{{ msg.toolCall.tool_name }}</span></div>
        <div class="tool-call-row"><span class="tool-call-label">参数</span><span class="tool-call-val" style="font-family:monospace">{{ JSON.stringify(msg.toolCall.params || {}) }}</span></div>
        <div class="tool-call-row"><span class="tool-call-label">结果</span><span class="tool-call-val tool-call-result">{{ msg.toolCall.tool_result || '' }}</span></div>
      </div>

      <div v-if="msg.ragResults && msg.ragResults.length" class="rag-sources">
        <div v-for="(r, i) in msg.ragResults.slice(0, 3)" :key="i" class="rag-chunk">
          <span class="rag-chunk-score">相关度 {{ similarity(r) }}</span>{{ preview(r) }}
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'

const props = defineProps({ msg: { type: Object, required: true } })
const open = ref(true)

const hasThink = computed(() => {
  const m = props.msg
  return (m.steps && m.steps.length) || m.toolCall || (m.ragResults && m.ragResults.length) || m.sandbox
})

const LABELS = { react: '⚔ Saber · 拔剑中', tool: '工具调用', rag: '知识检索', chat: '对话' }
const CLASSES = { react: 'badge-react', tool: 'badge-tool', rag: 'badge-rag', chat: 'badge-react' }
const badgeText = computed(() => (props.msg.streaming && !props.msg.mode) ? '⚔ 出鞘…' : (LABELS[props.msg.mode] || props.msg.mode || '⚔ Saber'))
const badgeClass = computed(() => CLASSES[props.msg.mode] || 'badge-react')

const STEP = {
  Thought: ['step-thought', '💭 思考'],
  Action: ['step-action', '⚡ 动作'],
  Observation: ['step-observation', '🔍 观察'],
  'Final Answer': ['step-final', '✓ 汇总'],
}
function stepCls(t) { return (STEP[t] || ['step-thought'])[0] }
function stepLabel(t) { return (STEP[t] || [null, t])[1] }
function preview(r) {
  const raw = (r.chunk && r.chunk.content) || r.content || r.parent_content || ''
  const c = String(raw).slice(0, 120)
  return c + (c.length >= 120 ? '…' : '')
}
function similarity(r) {
  const value = Number(r?.similarity ?? r?.score ?? 0)
  return Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : '暂无'
}
</script>
