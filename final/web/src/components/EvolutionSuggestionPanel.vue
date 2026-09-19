<template>
  <aside class="evolution-panel" role="dialog" aria-modal="true" aria-labelledby="evolution-title"
         :aria-busy="busy" @keydown.esc="$emit('close')">
    <header class="evolution-header">
      <div>
        <span>证据驱动 · 人工闭环</span>
        <h3 id="evolution-title">受控策略建议</h3>
        <p>从已完成评测的失败指标与失败用例生成受限的 RAG 参数实验假设。</p>
      </div>
      <button ref="closeButton" type="button" aria-label="关闭受控策略建议" @click="$emit('close')">关闭</button>
    </header>

    <div class="evolution-scroll">
      <section class="evolution-truth" role="note" aria-label="受控策略建议真实性说明">
        <strong>它不会“自己学习并上线”，也不代表效果已经提升</strong>
        <p>系统只允许建议单次请求范围内的 <code>RAG top_k</code>（召回数量）和“无答案阈值”。接受建议后，还要人工物化、跑同版本完整基线/候选评测、通过离线门禁，再进入受控线上实验。</p>
        <ol aria-label="受控策略建议流程">
          <li><b>1</b><span>锁定评测证据</span></li>
          <li><b>2</b><span>另一人审批</span></li>
          <li><b>3</b><span>显式物化</span></li>
          <li><b>4</b><span>重新完整评测</span></li>
          <li><b>5</b><span>受控线上实验</span></li>
        </ol>
      </section>

      <div v-if="evaluation.evolutionError" class="evolution-feedback error" role="alert">
        <strong>操作失败</strong><span>{{ friendlyError(evaluation.evolutionError) }}</span>
      </div>
      <div v-if="evaluation.evolutionMessage" class="evolution-feedback success" role="status" aria-live="polite">
        {{ evaluation.evolutionMessage }}
      </div>

      <section class="evolution-create" aria-labelledby="evolution-create-title">
        <div>
          <h4 id="evolution-create-title">从已完成运行生成实验假设</h4>
          <p>只有检索或无答案判断存在可操作信号时才会生成；其他问题不会被强行归因给 RAG 参数。</p>
        </div>
        <form @submit.prevent="createSuggestion">
          <label for="evolution-source-run">证据运行
            <select id="evolution-source-run" v-model="sourceRunId" required :disabled="busy">
              <option value="" disabled>选择一条已完成评测</option>
              <option v-for="run in evaluation.completedRuns" :key="run.id" :value="run.id">
                {{ runLabel(run) }}
              </option>
            </select>
          </label>
          <button type="submit" :disabled="busy || !sourceRunId">
            {{ evaluation.evolutionActionLoading ? '正在锁定证据…' : '生成受控建议' }}
          </button>
        </form>
      </section>

      <div v-if="evaluation.evolutionLoading" class="evolution-loading" role="status" aria-live="polite">
        <span aria-hidden="true"></span>正在核对建议、校验和与审计记录…
      </div>

      <section v-else class="evolution-workspace" aria-labelledby="evolution-record-title">
        <div class="evolution-section-head">
          <div><h4 id="evolution-record-title">建议记录</h4><p>证据与策略清单创建后不可修改，状态变更使用代次校验。</p></div>
          <span>{{ evaluation.evolutionSuggestions.length }} 条</span>
        </div>
        <div v-if="!evaluation.evolutionSuggestions.length" class="evolution-empty">
          <strong>还没有受控建议</strong>
          <p>先选择包含 RAG 失败信号的已完成评测。合成回放可以生成假设，但会明确标记为非真实效果。</p>
        </div>
        <div v-else class="evolution-grid">
          <nav class="evolution-list" aria-label="受控策略建议列表">
            <button v-for="item in evaluation.evolutionSuggestions" :key="item.id" type="button"
                    :class="{ active: selected?.id === item.id }" :aria-pressed="selected?.id === item.id"
                    @click="evaluation.selectEvolutionSuggestion(item.id).catch(() => {})">
              <span>{{ runName(item.source_run_id) }}</span>
              <small>第 {{ item.generation }} 代 · {{ statusText(item.status) }}</small>
              <b :class="`status-${item.status}`">{{ statusText(item.status) }}</b>
            </button>
          </nav>

          <article v-if="selected" class="evolution-detail">
            <div class="detail-title">
              <div><span>实验假设 {{ shortId(selected.id) }}</span><h5>{{ statusText(selected.status) }}</h5></div>
              <b :class="truthTone">{{ truthTitle }}</b>
            </div>

            <section class="claim-boundary" aria-label="结论边界">
              <strong>当前只能说：已生成待验证的参数假设</strong>
              <p>{{ truthDescription }}</p>
            </section>

            <dl class="evolution-facts">
              <div><dt>建议召回数量（top_k）</dt><dd>{{ valueOrDash(ragManifest.top_k) }}</dd></div>
              <div><dt>建议无答案阈值</dt><dd>{{ decimalOrDash(ragManifest.no_answer_threshold) }}</dd></div>
              <div><dt>规则版本</dt><dd>{{ selected.rule_version || '—' }}</dd></div>
              <div><dt>当前代次</dt><dd>{{ valueOrDash(selected.generation) }}</dd></div>
              <div><dt>证据完整性</dt><dd>{{ selected.truth?.evidence_complete === true ? '完整' : '不完整，仅作假设' }}</dd></div>
              <div><dt>自动应用</dt><dd>{{ selected.no_auto_apply === true ? '关闭' : '后端未声明' }}</dd></div>
            </dl>

            <div class="evolution-columns">
              <section>
                <h6>为什么提出这个假设</h6>
                <ul><li v-for="reason in selected.rationale || []" :key="reason">{{ reason }}</li></ul>
              </section>
              <section class="limitations">
                <h6>限制与后续门禁</h6>
                <ul><li v-for="item in selected.limitations || []" :key="item">{{ item }}</li></ul>
              </section>
            </div>

            <details class="evidence-details">
              <summary>查看证据与校验信息</summary>
              <dl>
                <div><dt>来源运行</dt><dd>{{ selected.source_run_id || '—' }}</dd></div>
                <div><dt>数据集版本</dt><dd>{{ selected.dataset_version_id || '—' }}</dd></div>
                <div><dt>证据校验和</dt><dd>{{ selected.evidence_checksum || '—' }}</dd></div>
                <div><dt>建议校验和</dt><dd>{{ selected.manifest_checksum || '—' }}</dd></div>
                <div><dt>失败结果 / 失败用例</dt><dd>{{ evidenceCount('case_results') }} / {{ evidenceCount('badcases') }}</dd></div>
              </dl>
              <pre aria-label="受控建议策略清单">{{ manifestText }}</pre>
            </details>

            <section v-if="selected.status === 'proposed'" class="review-box">
              <div><strong>必须由另一位审批人决定</strong><p>创建者不能审批自己的建议；权限与身份由服务端校验。</p></div>
              <label for="evolution-review-note">审批备注
                <textarea id="evolution-review-note" v-model.trim="reviewNote" rows="3" maxlength="1000"
                          placeholder="记录接受为实验假设或拒绝的理由" :disabled="busy"></textarea>
              </label>
              <div>
                <button class="secondary" type="button" :disabled="busy" @click="decide('reject')">拒绝建议</button>
                <button type="button" :disabled="busy" @click="decide('accept')">接受为实验假设</button>
              </div>
            </section>

            <section v-if="selected.status === 'accepted' && !selected.materialized_strategy_version_id" class="materialize-box">
              <div><strong>接受不等于策略已创建</strong><p>显式物化只创建一个不可变离线策略版本，不会创建晋级候选、激活策略或部署线上流量。</p></div>
              <label for="evolution-strategy-name">离线策略名称
                <input id="evolution-strategy-name" v-model.trim="strategyName" maxlength="200"
                       placeholder="例如：RAG 参数实验候选 v1" :disabled="busy" />
              </label>
              <button type="button" :disabled="busy" @click="materialize">显式物化为离线策略版本</button>
            </section>

            <section v-if="selected.materialized_strategy_version_id" class="materialized-box" role="status">
              <strong>已物化，但尚未证明有效</strong>
              <p>离线策略版本：<code>{{ selected.materialized_strategy_version_id }}</code></p>
              <p>下一步必须建立同数据集完整候选运行，与基线成对比较并通过 P2/P3 门禁。</p>
            </section>

            <section class="audit-box" aria-labelledby="evolution-audit-title">
              <div><h6 id="evolution-audit-title">审计记录</h6><span>{{ evaluation.evolutionAuditEvents.length }} 条</span></div>
              <ol v-if="evaluation.evolutionAuditEvents.length">
                <li v-for="event in evaluation.evolutionAuditEvents" :key="event.id">
                  <b>{{ auditAction(event.action) }}</b><span>{{ event.actor || '未知操作者' }}</span><time>{{ timeText(event.created_at) }}</time>
                </li>
              </ol>
              <p v-else>尚无可显示的审计记录。</p>
            </section>
          </article>
        </div>
      </section>
    </div>
  </aside>
</template>

<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useEvaluation } from '../stores/evaluation'

defineEmits(['close'])
const evaluation = useEvaluation()
const closeButton = ref(null)
const sourceRunId = ref('')
const reviewNote = ref('')
const strategyName = ref('')
const selected = computed(() => evaluation.selectedEvolutionSuggestion)
const busy = computed(() => evaluation.evolutionLoading || evaluation.evolutionActionLoading)
const ragManifest = computed(() => selected.value?.suggestion_manifest?.runtime_overrides?.rag || {})
const manifestText = computed(() => JSON.stringify(selected.value?.suggestion_manifest || {}, null, 2))
const truthTitle = computed(() => {
  if (selected.value?.truth?.synthetic || selected.value?.truth?.replay) return '合成或回放证据'
  if (selected.value?.truth?.evidence_complete !== true) return '证据不完整'
  return '离线证据假设'
})
const truthTone = computed(() => selected.value?.truth?.synthetic || selected.value?.truth?.replay
  ? 'truth-warning'
  : selected.value?.truth?.evidence_complete === true ? 'truth-neutral' : 'truth-danger')
const truthDescription = computed(() => {
  if (selected.value?.truth?.replay) return '来源包含回放预置输出，不调用真实模型、工具或 RAG；不得当作真实智能体效果。'
  if (selected.value?.truth?.synthetic) return '来源包含合成数据，只能用于验证链路与提出待验证假设。'
  if (selected.value?.truth?.evidence_complete !== true) return '来源证据字段不完整，不能据此宣称离线或线上改进。'
  return '来源是离线评测证据；它仍不能证明候选已提升，更不能代替真实线上实验。'
})

watch(() => selected.value?.id, () => {
  reviewNote.value = ''
  strategyName.value = ''
})

onMounted(async () => {
  if (!evaluation.runs.length) await evaluation.refresh().catch(() => {})
  await evaluation.loadEvolutionWorkspace().catch(() => {})
  await nextTick()
  closeButton.value?.focus()
})

async function createSuggestion() {
  if (!sourceRunId.value) return
  await evaluation.createEvolutionSuggestion(sourceRunId.value).catch(() => {})
}

async function decide(decision) {
  if (!selected.value) return
  const label = decision === 'accept' ? '接受为待验证实验假设' : '拒绝该建议'
  if (!window.confirm(`确认${label}？\n\n该操作使用第 ${selected.value.generation} 代状态并写入不可变审计记录。`)) return
  await evaluation.decideEvolutionSuggestion(selected.value.id, decision, reviewNote.value).catch(() => {})
}

async function materialize() {
  if (!selected.value) return
  if (!window.confirm('确认显式物化？\n\n只会创建离线策略版本，不会创建晋级候选、激活策略或部署线上流量。')) return
  await evaluation.materializeEvolutionSuggestion(selected.value.id, strategyName.value).catch(() => {})
}

function shortId(value) { return String(value || '').slice(0, 10) || '未知' }
function runName(id) { return evaluation.runs.find(item => item.id === id)?.name || `运行 ${shortId(id)}` }
function runLabel(run) { return `${run.name || shortId(run.id)} · ${shortId(run.id)}` }
function statusText(value) {
  return ({ proposed: '等待人工审批', accepted: '已接受为假设', rejected: '已拒绝' })[value] || '未知状态'
}
function valueOrDash(value) { return value == null || value === '' ? '—' : String(value) }
function decimalOrDash(value) { const n = Number(value); return value == null || !Number.isFinite(n) ? '—' : n.toFixed(3) }
function evidenceCount(key) { return Array.isArray(selected.value?.evidence?.[key]) ? selected.value.evidence[key].length : 0 }
function auditAction(value) {
  return ({ evolution_create: '生成建议', evolution_review: '人工审批', evolution_materialize: '显式物化' })[value] || value || '未知操作'
}
function timeText(value) {
  if (!value) return '时间未知'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN', { hour12: false })
}
function friendlyError(value) {
  const text = String(value || '')
  if (text === 'unauthorized') return '登录状态已失效，请重新登录。'
  if (text.includes('experiment_admin')) return '当前账号没有策略管理员权限。'
  if (text.includes('experiment_approver')) return '当前账号没有策略审批权限。'
  if (text.includes('actionable v1 RAG signal')) return '这次评测没有可用于 v1 的 RAG 参数信号，因此没有生成建议。'
  if (text.includes('creator cannot review')) return '创建者不能审批自己的建议，请由另一位审批人操作。'
  if (text.includes('generation conflict')) return '记录已被其他操作更新，请刷新后重试。'
  if (text.includes('evidence changed')) return '来源证据已经变化，旧建议已失效，请重新生成。'
  return text || '未知错误'
}
</script>

<style scoped>
.evolution-panel{position:absolute;z-index:6;inset:0 0 0 auto;width:min(1040px,97vw);display:flex;flex-direction:column;background:#f6f8fb;border-left:1px solid #dce2eb;box-shadow:-24px 0 64px rgba(28,39,66,.22);color:#253149}
.evolution-header{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:18px 22px;background:#fff;border-bottom:1px solid #dfe4ec}.evolution-header span{color:#aa2852;font-size:11px;font-weight:800;letter-spacing:.08em}.evolution-header h3{margin:4px 0 0;font-size:19px}.evolution-header p{margin:4px 0 0;color:#657187;font-size:12px;line-height:1.5}.evolution-header button{min-width:64px;min-height:44px;border:1px solid #ccd3df;border-radius:8px;background:#fff;color:#3d4960;font-weight:700;cursor:pointer}
.evolution-scroll{min-height:0;overflow:auto;padding:18px 22px 30px}.evolution-truth{border:1px solid #e9bfd0;border-left:4px solid #c92d5d;border-radius:11px;background:#fff8fa;padding:14px 16px}.evolution-truth>strong{display:block;color:#7a1e3d;font-size:14px}.evolution-truth>p{margin:5px 0 12px;color:#684c57;font-size:12px;line-height:1.6}.evolution-truth code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}.evolution-truth ol{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;list-style:none;margin:0;padding:0}.evolution-truth li{display:flex;align-items:center;gap:7px;min-width:0;color:#604954;font-size:10px}.evolution-truth li b{display:grid;place-items:center;flex:none;width:24px;height:24px;border-radius:50%;background:#f3d4df;color:#8c2146}.evolution-truth li span{line-height:1.35}
.evolution-feedback{display:flex;gap:8px;margin-top:10px;padding:10px 12px;border-radius:9px;font-size:12px;line-height:1.5}.evolution-feedback.error{border:1px solid #f0b7c4;background:#fff1f4;color:#8a1732}.evolution-feedback.success{border:1px solid #b7ddcc;background:#f2faf6;color:#165f43}.evolution-create,.evolution-workspace{margin-top:12px;border:1px solid #dfe4ec;border-radius:11px;background:#fff;padding:15px}.evolution-create{display:flex;align-items:end;justify-content:space-between;gap:20px}.evolution-create h4,.evolution-section-head h4{margin:0;font-size:14px}.evolution-create p,.evolution-section-head p{margin:4px 0 0;color:#69758a;font-size:11px;line-height:1.5}.evolution-create form{display:flex;align-items:end;gap:9px;min-width:min(440px,50%)}.evolution-create label,.review-box label,.materialize-box label{display:grid;flex:1;gap:5px;color:#455168;font-size:11px;font-weight:700}.evolution-create select,.review-box textarea,.materialize-box input{width:100%;min-height:44px;border:1px solid #cbd3df;border-radius:8px;background:#fff;padding:9px 10px;color:#263149;font:inherit;font-size:12px;outline:none}.review-box textarea{resize:vertical;line-height:1.5}.evolution-create select:focus,.review-box textarea:focus,.materialize-box input:focus{border-color:#c92d5d;box-shadow:0 0 0 3px rgba(201,45,93,.13)}
.evolution-create button,.review-box button,.materialize-box button{min-height:44px;border:1px solid #c92d5d;border-radius:8px;background:#c92d5d;padding:9px 13px;color:#fff;font-weight:750;cursor:pointer;touch-action:manipulation}.evolution-create button:disabled,.review-box button:disabled,.materialize-box button:disabled{opacity:.48;cursor:not-allowed}.review-box button.secondary{border-color:#c8d0dc;background:#fff;color:#455168}.evolution-loading{display:flex;align-items:center;justify-content:center;gap:9px;min-height:180px;color:#647087;font-size:12px}.evolution-loading span{width:18px;height:18px;border:2px solid #e7a7bb;border-top-color:#c92d5d;border-radius:50%;animation:evolution-spin .8s linear infinite}.evolution-section-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px}.evolution-section-head>span{border-radius:999px;background:#edf1f5;padding:5px 9px;color:#5d697c;font-size:10px;font-weight:750}.evolution-empty{display:grid;place-items:center;align-content:center;min-height:150px;text-align:center}.evolution-empty strong{font-size:13px}.evolution-empty p{max-width:570px;margin:6px 0 0;color:#6d788c;font-size:11px;line-height:1.55}
.evolution-grid{display:grid;grid-template-columns:230px minmax(0,1fr);gap:15px}.evolution-list{display:flex;flex-direction:column;gap:7px;max-height:640px;overflow:auto}.evolution-list button{position:relative;min-height:66px;border:1px solid #dce2ea;border-radius:9px;background:#fff;padding:10px 76px 10px 11px;text-align:left;color:#354158;cursor:pointer}.evolution-list button.active{border-color:#c92d5d;background:#fff6f9;box-shadow:inset 3px 0 #c92d5d}.evolution-list span,.evolution-list small{display:block}.evolution-list span{font-size:11px;font-weight:750;overflow-wrap:anywhere}.evolution-list small{margin-top:5px;color:#717d91;font-size:9px}.evolution-list b{position:absolute;right:8px;top:9px;border-radius:5px;padding:4px 6px;font-size:9px}.status-proposed{background:#fff0d3;color:#7d5500}.status-accepted{background:#e8f1ff;color:#28528f}.status-rejected{background:#fde9ed;color:#9d213d}.evolution-detail{min-width:0;border-left:1px solid #e5e9ef;padding-left:15px}.detail-title{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.detail-title span{color:#747f92;font:600 10px/1.3 ui-monospace,SFMono-Regular,Consolas,monospace}.detail-title h5{margin:4px 0 0;font-size:16px}.detail-title>b{border-radius:6px;padding:5px 8px;font-size:10px}.truth-warning{background:#fff0d3;color:#7d5500}.truth-neutral{background:#e8eef8;color:#3f506b}.truth-danger{background:#fde9ed;color:#9d213d}.claim-boundary{margin-top:11px;border-left:4px solid #b36a16;border-radius:7px;background:#fff9ef;padding:10px 12px}.claim-boundary strong{color:#70460f;font-size:12px}.claim-boundary p{margin:4px 0 0;color:#6e5a3e;font-size:11px;line-height:1.55}
.evolution-facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));margin:12px 0 0;border:1px solid #e1e6ed;border-radius:9px;overflow:hidden}.evolution-facts>div{min-width:0;padding:9px 10px;border-right:1px solid #e9edf2;border-bottom:1px solid #e9edf2}.evolution-facts>div:nth-child(3n){border-right:0}.evolution-facts>div:nth-last-child(-n+3){border-bottom:0}.evolution-facts dt{color:#707c90;font-size:9px}.evolution-facts dd{margin:4px 0 0;color:#2e3a50;font-size:11px;font-weight:750;overflow-wrap:anywhere}.evolution-columns{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:11px}.evolution-columns section{border:1px solid #dfe5ec;border-radius:9px;background:#fbfcfd;padding:11px}.evolution-columns section.limitations{border-color:#ead8c2;background:#fffcf7}.evolution-columns h6,.audit-box h6{margin:0;font-size:11px}.evolution-columns ul{margin:7px 0 0;padding-left:17px;color:#59667b;font-size:10px;line-height:1.55}.evolution-columns li+li{margin-top:4px}.evidence-details{margin-top:11px;border:1px solid #dfe5ec;border-radius:9px;background:#fbfcfd;padding:10px 11px}.evidence-details summary{min-height:24px;color:#3b485f;font-size:11px;font-weight:750;cursor:pointer}.evidence-details dl{display:grid;gap:6px;margin:8px 0}.evidence-details dl>div{display:grid;grid-template-columns:100px minmax(0,1fr);gap:9px}.evidence-details dt{color:#778296;font-size:9px}.evidence-details dd{margin:0;color:#364259;font:500 9px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere}.evidence-details pre{max-height:180px;overflow:auto;margin:8px 0 0;border-radius:7px;background:#222a38;padding:10px;color:#edf2fa;font:10px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap}
.review-box,.materialize-box,.materialized-box{margin-top:11px;border:1px solid #d8dee8;border-radius:9px;background:#f8fafc;padding:11px}.review-box>div:first-child,.materialize-box>div:first-child{margin-bottom:9px}.review-box strong,.materialize-box strong,.materialized-box strong{font-size:12px}.review-box p,.materialize-box p,.materialized-box p{margin:4px 0 0;color:#617087;font-size:10px;line-height:1.5}.review-box>div:last-child{display:flex;justify-content:flex-end;gap:8px;margin-top:9px}.materialize-box{border-color:#b9dacb;background:#f5fbf8}.materialize-box button{width:100%;margin-top:9px;border-color:#1d7554;background:#1d7554}.materialized-box{border-color:#b9dacb;background:#f3faf6;color:#175d43}.materialized-box code{overflow-wrap:anywhere}.audit-box{margin-top:11px;border-top:1px solid #e2e7ee;padding-top:11px}.audit-box>div{display:flex;align-items:center;justify-content:space-between}.audit-box>div span{color:#717d91;font-size:9px}.audit-box ol{list-style:none;margin:8px 0 0;padding:0}.audit-box li{display:grid;grid-template-columns:100px 1fr auto;gap:8px;padding:7px 0;border-top:1px solid #edf0f4;font-size:9px}.audit-box li b{color:#354158}.audit-box li span{color:#657187}.audit-box time{color:#7a8597}.audit-box>p{color:#778296;font-size:10px}
.evolution-header button:hover,.evolution-list button:hover{background:#f5f7fa}.evolution-header button:focus-visible,.evolution-create select:focus-visible,.evolution-create button:focus-visible,.evolution-list button:focus-visible,.review-box textarea:focus-visible,.review-box button:focus-visible,.materialize-box input:focus-visible,.materialize-box button:focus-visible,.evidence-details summary:focus-visible{outline:3px solid rgba(201,45,93,.32);outline-offset:2px}@keyframes evolution-spin{to{transform:rotate(360deg)}}@media(prefers-reduced-motion:reduce){.evolution-loading span{animation:none}}
@media(max-width:800px){.evolution-panel{position:fixed;width:100vw}.evolution-scroll{padding:13px}.evolution-create{display:grid;align-items:stretch}.evolution-create form{min-width:0}.evolution-grid,.evolution-columns{grid-template-columns:1fr}.evolution-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));max-height:none}.evolution-detail{border-left:0;border-top:1px solid #e5e9ef;padding:14px 0 0}.evolution-facts{grid-template-columns:repeat(2,minmax(0,1fr))}.evolution-facts>div,.evolution-facts>div:nth-child(3n){border-right:1px solid #e9edf2;border-bottom:1px solid #e9edf2}.evolution-facts>div:nth-child(2n){border-right:0}.evolution-facts>div:nth-last-child(-n+2){border-bottom:0}.evolution-create select,.review-box textarea,.materialize-box input{font-size:16px}}
@media(max-width:560px){.evolution-header{align-items:flex-start;padding:14px}.evolution-header p{display:none}.evolution-truth ol{grid-template-columns:1fr 1fr}.evolution-create form{display:grid}.evolution-list{grid-template-columns:1fr}.evolution-facts{grid-template-columns:1fr}.evolution-facts>div,.evolution-facts>div:nth-child(2n),.evolution-facts>div:nth-last-child(-n+2){border-right:0;border-bottom:1px solid #e9edf2}.evolution-facts>div:last-child{border-bottom:0}.audit-box li{grid-template-columns:1fr auto}.audit-box li time{grid-column:1/3}.review-box>div:last-child{display:grid;grid-template-columns:1fr}.evolution-truth li{min-height:36px}}
</style>
