<template>
  <div class="controls-bar">
    <div class="rag-toggle-wrap" :class="{ on: chat.ragOn }" @click="chat.toggleRag()">
      <div class="toggle-pill"></div>
      <span class="toggle-label">知识库</span>
    </div>

    <div class="tools-btn-wrap">
      <button class="tools-btn" type="button" :class="{ active: skills.enabledCount > 0 }" @click="$emit('open-skills')">
        <svg class="control-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M8.5 4H5v3.5a2.5 2.5 0 1 0 0 5V16h3.5a2.5 2.5 0 1 0 5 0H17v-3.5a2.5 2.5 0 1 0 0-5V4h-3.5a2.5 2.5 0 1 0-5 0Z"/></svg>
        <span>技能广场</span>
        <span class="tool-count" v-if="skills.enabledCount > 0">{{ skills.enabledCount }}</span>
      </button>
    </div>

    <div v-if="features.research" class="tools-btn-wrap">
      <button class="tools-btn native-run-entry" type="button" @click="$emit('open-research')">
        <svg class="control-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="10" cy="10" r="6"/><path d="m15 15 5 5M7 10h6M10 7v6"/></svg>
        <span>研究工作台</span>
      </button>
    </div>

    <div class="tools-btn-wrap">
      <button class="tools-btn eval-entry" type="button" @click="$emit('open-evaluation')">
        <span class="eval-mark">QA</span>
        <span>智能体评测</span>
      </button>
    </div>

    <div class="tools-btn-wrap">
      <button class="tools-btn native-run-entry" type="button" @click="$emit('open-native-runs')">
        <span class="eval-mark">RUN</span><span>运行记录</span>
      </button>
    </div>

    <div class="tools-btn-wrap">
      <button class="tools-btn raglab-entry" type="button" @click="$emit('open-rag-lab')">
        <svg class="control-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h5M15 6h5M9 6a3 3 0 1 0 6 0 3 3 0 0 0-6 0ZM4 18h5M15 18h5M9 18a3 3 0 1 0 6 0 3 3 0 0 0-6 0ZM12 9v6"/></svg>
        <span>知识检索实验台</span>
      </button>
    </div>

    <div class="controls-right" :class="chat.modeHint.cls">{{ chat.modeHint.text }}</div>
  </div>
</template>

<script setup>
import { useChat } from '../stores/chat'
import { useSkills } from '../stores/skills'

defineProps({ features: { type: Object, default: () => ({ research: true }) } })
defineEmits(['open-skills', 'open-evaluation', 'open-rag-lab', 'open-native-runs', 'open-research'])
const chat = useChat()
const skills = useSkills()
</script>

<style scoped>
.controls-right.hint-primary { color: var(--primary-d); }
.controls-right.hint-pink { color: var(--pink-d); }
.controls-right.hint-muted { color: var(--text3); }
.control-icon { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
.eval-entry { color: #b91c4f; border-color: rgba(185,28,79,.25); }
.eval-entry:hover { background: #fff1f5; }
.eval-mark { font: 750 10px/1 ui-monospace, SFMono-Regular, Consolas, monospace; letter-spacing: .04em; }
.raglab-entry { color: #075985; border-color: rgba(3,105,161,.23); }
.raglab-entry:hover { background: #f0f9ff; border-color: #0284c7; }
.native-run-entry { color: #0f766e; border-color: rgba(15,118,110,.25); }
.native-run-entry:hover { background: #f0fdfa; }
</style>
