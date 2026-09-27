<template>
  <section class="plan-review" aria-labelledby="plan-heading" :aria-busy="busy">
    <header class="section-heading">
      <div><span class="section-kicker">PLAN / V{{ version }}</span><h3 id="plan-heading">{{ awaiting ? '审核研究计划' : '研究计划' }}</h3></div>
      <span class="plan-state">{{ awaiting ? '等待你的决定' : reviewLabel }}</span>
    </header>
    <p v-if="awaiting" class="section-note">确认范围与步骤后开始研究。修改会保存为新版本，需再次批准。</p>
    <fieldset :disabled="busy" class="plan-fields">
      <template v-if="editing">
        <label>研究目标<textarea v-model="draft.objective" rows="2" maxlength="20000"></textarea></label>
        <label>约束条件（每行一项）<textarea v-model="constraints" rows="2" maxlength="10000"></textarea></label>
      </template>
      <p v-else class="plan-objective">{{ plan.objective }}</p>
      <ul v-if="!editing && plan.constraints?.length" class="plan-constraints"><li v-for="(constraint, index) in plan.constraints" :key="index">{{ constraint }}</li></ul>
      <ol class="plan-steps">
        <li v-for="(step, index) in editing ? draft.steps : plan.steps" :key="step.id" class="plan-step">
          <div class="step-heading"><span class="step-number">{{ index + 1 }}</span><strong>{{ kindLabel(step.kind) }}</strong><code>{{ step.id }}</code><button v-if="editing" type="button" class="text-button" :disabled="draft.steps.length < 2" :aria-label="`删除步骤 ${index + 1}`" @click="removeStep(index)">删除</button></div>
          <template v-if="editing">
            <label>步骤标题<input v-model="step.title" maxlength="500" /></label>
            <label>执行要求<textarea v-model="step.guidance" rows="2" maxlength="10000"></textarea></label>
            <label>验收条件<input v-model="step.acceptance" maxlength="2000" /></label>
          </template>
          <template v-else><h4>{{ step.title }}</h4><p v-if="step.guidance">{{ step.guidance }}</p><p v-if="step.acceptance" class="acceptance">验收：{{ step.acceptance }}</p></template>
          <small v-if="step.depends_on?.length">依赖步骤：{{ step.depends_on.join('、') }}</small>
          <small v-if="step.tool_policy?.length">可用工具：{{ step.tool_policy.join('、') }}</small>
        </li>
      </ol>
      <button v-if="editing" type="button" class="research-button secondary" @click="addStep">添加研究步骤</button>
      <p v-if="validationError" class="research-error" role="alert">{{ validationError }}</p>
      <div v-if="awaiting" class="plan-actions">
        <template v-if="editing"><button type="button" class="research-button secondary" @click="resetDraft">放弃修改</button><button type="button" class="research-button primary" @click="review('edit')">保存新版本</button></template>
        <template v-else><button type="button" class="research-button danger" @click="review('reject')">拒绝计划</button><button type="button" class="research-button secondary" @click="editing = true">修改计划</button><button type="button" class="research-button primary" @click="review('approve')">{{ busy ? '提交中…' : '批准并开始研究' }}</button></template>
      </div>
    </fieldset>
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { planReviewPayload } from '../utils/research'

const props = defineProps({ plan: { type: Object, required: true }, version: { type: Number, required: true }, status: String, reviewStatus: String, busy: Boolean })
const emit = defineEmits(['review'])
const editing = ref(false)
const draft = ref({ steps: [] })
const constraints = ref('')
const validationError = ref('')
const awaiting = computed(() => props.status === 'awaiting_plan_review')
const reviewLabel = computed(() => ({ approved: '已批准', rejected: '已拒绝', pending: '待审核' })[props.reviewStatus] || '已保存')
const kindLabel = kind => ({ research: '资料研究', code: '代码分析', write: '报告撰写' })[kind] || kind
function resetDraft() {
  draft.value = JSON.parse(JSON.stringify(props.plan))
  constraints.value = (props.plan.constraints || []).join('\n')
  editing.value = false
  validationError.value = ''
}
function addStep() {
  const used = new Set(draft.value.steps.map(step => step.id))
  let number = draft.value.steps.length + 1
  while (used.has(`step-${number}`)) number++
  draft.value.steps.push({ id: `step-${number}`, title: '', kind: 'research', guidance: '', tool_policy: [], depends_on: [], acceptance: '' })
}
function removeStep(index) {
  const [removed] = draft.value.steps.splice(index, 1)
  draft.value.steps.forEach(step => { step.depends_on = (step.depends_on || []).filter(id => id !== removed.id) })
}
function review(action) {
  if (props.busy) return
  validationError.value = ''
  try {
    draft.value.constraints = constraints.value.split('\n').map(item => item.trim()).filter(Boolean)
    emit('review', planReviewPayload(action, props.version, draft.value))
  } catch (error) { validationError.value = error.message }
}
// Polling the same version must never discard a user's unfinished edits.
watch(() => props.version, resetDraft, { immediate: true })
watch(awaiting, value => { if (!value) editing.value = false })
</script>
