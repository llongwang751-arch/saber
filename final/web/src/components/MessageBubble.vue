<template>
  <div class="msg-row" :class="msg.role">
    <div class="avatar" :class="msg.role">{{ msg.role === 'user' ? '◇' : '◈' }}</div>
    <div class="msg-body">
      <div class="msg-name">{{ msg.role === 'user' ? '你' : 'AGI-saber' }}</div>

      <!-- 用户消息：纯文本 -->
      <div v-if="msg.role === 'user'" class="bubble user">{{ msg.text }}</div>

      <!-- 兼容旧格式（存的是 html 串） -->
      <div v-else-if="msg.html" class="bubble ai" v-html="msg.html"></div>

      <!-- AI 消息：结构化渲染 -->
      <div v-else class="bubble ai">
        <div v-if="msg.memory" class="memory-note">
          <span class="memory-tag">🧠 记忆</span>
          <span class="memory-content">{{ msg.memory }}</span>
        </div>

        <ThinkPanel :msg="msg" />

        <div v-if="msg.interrupted" class="interrupted-badge">🛑 已中断</div>

        <div v-if="!msg.answer && msg.streaming" class="typing"><span></span><span></span><span></span></div>
        <div v-else class="answer-text" v-html="rendered"></div>
      </div>

      <div v-if="msg.role === 'ai' && msg.feedbackEligible === true && msg.experimentExposureId && !msg.streaming && !msg.interrupted" class="experiment-feedback">
        <span class="feedback-label">这条回答有帮助吗？</span>
        <div class="feedback-actions" role="group" aria-label="评价这条回答">
          <button type="button" :class="{ selected: msg.feedbackRating === 1 }"
                  :aria-pressed="msg.feedbackRating === 1" aria-label="有帮助"
                  :disabled="feedbackDisabled(1)" @click="submitFeedback(1)">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 10v11H3V10h4Zm4 11H9V9l4-6 1.5 1.5V9H19a2 2 0 0 1 2 2l-1.3 7.2A3.5 3.5 0 0 1 16.3 21H11Z" /></svg>
          </button>
          <button type="button" :class="{ selected: msg.feedbackRating === -1 }"
                  :aria-pressed="msg.feedbackRating === -1" aria-label="需要改进"
                  :disabled="feedbackDisabled(-1)" @click="submitFeedback(-1)">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 14V3H3v11h4Zm4-11H9v12l4 6 1.5-1.5V15H19a2 2 0 0 0 2-2l-1.3-7.2A3.5 3.5 0 0 0 16.3 3H11Z" /></svg>
          </button>
        </div>
        <span class="feedback-status" :class="{ error: msg.feedbackError }" aria-live="polite">{{ feedbackStatus }}</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { renderMarkdown } from '../utils/markdown'
import { useChat } from '../stores/chat'
import ThinkPanel from './ThinkPanel.vue'

const props = defineProps({ msg: { type: Object, required: true } })
const chat = useChat()
const rendered = computed(() => renderMarkdown(props.msg.answer || ''))
const feedbackStatus = computed(() => {
  if (props.msg.feedbackLoading) return '正在保存反馈…'
  if (props.msg.feedbackError) return props.msg.feedbackError
  if (props.msg.feedbackRating === 1) return '已记录：有帮助'
  if (props.msg.feedbackRating === -1) return '已记录：需要改进'
  return '反馈将作为匿名 Outcome，不展示实验分组'
})
function feedbackDisabled(rating) {
  return props.msg.feedbackLoading
    || props.msg.feedbackRating != null
    || (props.msg.feedbackAttemptedRating != null && props.msg.feedbackAttemptedRating !== rating)
}
function submitFeedback(rating) { chat.submitExperimentFeedback(props.msg, rating) }
</script>

<style scoped>
.experiment-feedback{display:flex;align-items:center;flex-wrap:wrap;gap:7px;margin-top:4px;padding:2px 4px;color:#64748b;font-size:10px}.feedback-label{font-weight:650;color:#475569}.feedback-actions{display:flex;gap:5px}.feedback-actions button{width:44px;height:44px;display:grid;place-items:center;border:1px solid rgba(37,99,235,.18);border-radius:9px;background:rgba(255,255,255,.78);color:#64748b;cursor:pointer;touch-action:manipulation}.feedback-actions button:hover:not(:disabled){border-color:#2563eb;color:#1e40af;background:#eff6ff}.feedback-actions button.selected{border-color:#1e40af;background:#dbeafe;color:#1e40af}.feedback-actions button:disabled{opacity:.55;cursor:not-allowed}.feedback-actions svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}.feedback-actions button:focus-visible{outline:3px solid rgba(37,99,235,.3);outline-offset:2px}.feedback-status{flex:1;min-width:180px;line-height:1.4}.feedback-status.error{color:#b4233f}
@media(max-width:560px){.experiment-feedback{align-items:flex-start}.feedback-label{width:100%}.feedback-status{min-width:0;width:100%}}
</style>
