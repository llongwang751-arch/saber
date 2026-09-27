<template>
  <div class="eval-backdrop" @click.self="$emit('close')">
    <section class="eval-workbench" role="dialog" aria-modal="true" aria-label="智能体质量评测工作台"
             :aria-busy="isBusy" @keydown.esc="closeTopLayer">
      <header class="eval-head">
        <div>
          <p class="eval-kicker">智能体质量评测</p>
          <h2>智能体质量评测工作台</h2>
          <p>集中管理测试用例、自动执行、执行轨迹、失败用例与发布检查</p>
        </div>
        <div class="eval-actions" role="group" aria-label="评测操作">
          <template>
            <button class="eval-btn secondary" type="button" :disabled="isBusy || evaluation.loading" @click="evaluation.refresh()">刷新状态</button>
            <button class="eval-btn replay" type="button" :disabled="isBusy" aria-describedby="replay-truth-note"
                    @click="evaluation.bootstrapDemo()">
              {{ replayButtonText }}
            </button>
            <button class="eval-btn primary" type="button" :disabled="isBusy || !evaluation.liveReady"
                    aria-describedby="live-readiness-reason" @click="evaluation.runLiveDemo()">
              {{ liveButtonText }}
            </button>
            <button ref="strategyTrigger" class="eval-btn strategy" type="button" :disabled="isBusy"
                    @click="openStrategyPanel">离线策略审批</button>
            <button ref="evolutionTrigger" class="eval-btn evolution" type="button" :disabled="isBusy"
                    @click="openEvolutionPanel">受控策略建议</button>
          </template>
          <button class="eval-close" type="button" aria-label="关闭" @click="$emit('close')">×</button>
        </div>
      </header>

      <div class="offline-workspace">
      <div class="eval-mode-summary">
        <section id="replay-truth-note" class="truth-note" aria-label="合成回放说明">
          <span class="mode-mark">回放</span>
          <div>
            <strong>合成回放只验证评测平台，不代表真实智能体效果</strong>
            <p>它读取测试集中预置的输出和轨迹，<b>不会调用真实模型、工具或 RAG</b>；即使通过率为 100%，也不能当作线上效果。</p>
          </div>
        </section>
        <section id="live-readiness-reason" class="readiness-card" :class="liveReadinessClass"
                 role="status" aria-live="polite">
          <div class="readiness-title">
            <span class="readiness-dot" aria-hidden="true"></span>
            <strong>{{ liveReadinessTitle }}</strong>
          </div>
          <p v-if="evaluation.readinessLoading">正在检查本地智能体和运行依赖。</p>
          <ul v-else-if="readinessMessages.length">
            <li v-for="reason in readinessMessages.slice(0, 3)" :key="reason">{{ reason }}</li>
          </ul>
          <p v-else>{{ evaluation.liveReady ? '可以运行真实模型、工具与检索链路评测。' : '后端尚未声明真实评测已就绪。' }}</p>
        </section>
      </div>

      <div v-if="evaluation.error" class="eval-error" role="alert">
        <strong>操作失败</strong><span>{{ friendlyEvaluationError(evaluation.error) }}</span>
      </div>

      <div v-if="isBusy || evaluation.operationMessage" class="operation-status" aria-live="polite" aria-atomic="true">
        <span>{{ operationStatusText }}</span>
        <div v-if="isBusy && operationTotal" class="operation-progress" role="progressbar"
             :aria-valuenow="operationCompleted" aria-valuemin="0" :aria-valuemax="operationTotal">
          <i :style="{ width: operationPercent + '%' }"></i>
        </div>
      </div>

      <div class="eval-kpis" aria-label="评测概览">
        <div class="eval-kpi"><span>评测集</span><strong>{{ evaluation.datasets.length }}</strong><small>版本化数据资产</small></div>
        <div class="eval-kpi"><span>通过率</span><strong>{{ percent(summary.pass_rate) }}</strong><small>{{ runProgressText }}</small></div>
        <div class="eval-kpi"><span>失败用例</span><strong>{{ evaluation.badcases.length }}</strong><small>当前运行中等待处理</small></div>
        <div class="eval-kpi gate" :class="gateClass"><span>发布检查</span><strong>{{ gateText }}</strong><small>严重与高危问题优先阻止发布</small></div>
      </div>

      <div v-if="evaluation.loading && !evaluation.runs.length" class="eval-skeleton" aria-label="正在加载">
        <span v-for="n in 7" :key="n"></span>
      </div>

      <div v-else class="eval-body">
        <section class="eval-main">
          <div class="panel-head">
            <div><h3>评测记录</h3><p>选择一条记录，查看测试结果与执行轨迹</p></div>
            <div class="report-actions" v-if="evaluation.selectedRun">
              <button type="button" @click="evaluation.downloadReport('markdown')">导出报告</button>
              <button type="button" @click="evaluation.downloadReport('csv')">导出表格</button>
            </div>
          </div>
          <div v-if="!evaluation.runs.length" class="eval-empty">
            <strong>还没有评测运行</strong>
            <span>可先运行合成回放验证评测闭环；本地智能体就绪后，再运行真实链路评测。</span>
          </div>
          <div v-else class="run-strip">
            <button v-for="run in evaluation.runs.slice(0, 20)" :key="run.id" type="button"
                    class="run-chip" :class="{ active: evaluation.selectedRun?.id === run.id }"
                    :aria-pressed="evaluation.selectedRun?.id === run.id"
                    @click="evaluation.loadRun(run.id)">
              <span>{{ runName(run.name) || shortId(run.id) }}</span><small>{{ runModeText(run) }}</small>
              <b :class="statusClass(run.status)">{{ statusText(run.status) }}</b>
            </button>
          </div>

          <dl v-if="evaluation.selectedRun" class="run-facts" aria-label="当前评测运行的执行口径">
            <div v-for="fact in selectedRunFacts" :key="fact.label">
              <dt>{{ fact.label }}</dt>
              <dd :class="fact.tone ? `fact-${fact.tone}` : ''">{{ fact.value }}</dd>
            </div>
          </dl>

          <div v-if="evaluation.comparison" class="compare-line">
            <span>修复 {{ evaluation.comparison.fixed?.length || 0 }} 条</span>
            <span>回归 {{ evaluation.comparison.regressions?.length || 0 }} 条</span>
            <span>净变化 {{ signed(evaluation.comparison.pass_rate_delta) }}</span>
          </div>

          <div class="result-table" v-if="evaluation.results.length">
            <div class="result-row result-head"><span>用例</span><span>场景</span><span>分数</span><span>状态</span><span>链路</span></div>
            <button v-for="item in evaluation.results" :key="item.id" type="button" class="result-row"
                    :aria-label="`查看${caseName(item.case_id)}的执行轨迹`" @click="evaluation.openTrace(item.id)">
              <span class="case-id">{{ caseName(item.case_id) }}</span>
              <span>{{ item.scenario || item.payload?.scenario || '智能体场景' }}</span>
              <span>{{ score(item.metrics?.overall_score) }}</span>
              <span><b :class="statusClass(item.status)">{{ statusText(item.status) }}</b></span>
              <span class="trace-link">查看轨迹</span>
            </button>
          </div>
        </section>

        <aside class="eval-aside">
          <div class="panel-head"><div><h3>失败用例处理</h3><p>分类、定级、分析原因并重新验证</p></div></div>
          <div v-if="!evaluation.badcases.length" class="eval-empty compact"><strong>暂无失败用例</strong><span>当前运行没有发现失败用例。</span></div>
          <div v-else class="badcase-list">
            <article v-for="item in evaluation.badcases.slice(0, 30)" :key="item.id" class="badcase-item">
              <div class="badcase-top"><b>{{ caseName(item.case_id || shortId(item.eval_case_id)) }}</b><span :class="severityClass(item.severity)">{{ severityText(item.severity) }}</span></div>
              <p>{{ categoryText(item.category) }} · {{ statusText(item.status) }}</p>
              <small>{{ failedMetrics(item) }}</small>
              <button v-if="item.status === 'open'" type="button" @click="evaluation.triage(item, auth.username)">标记已分诊</button>
              <span v-else class="owner">负责人：{{ item.owner || '未分配' }}</span>
            </article>
          </div>
        </aside>
      </div>

      <aside v-if="strategyPanelOpen" class="strategy-drawer" role="dialog" aria-modal="true"
             aria-labelledby="strategy-panel-title">
        <header class="strategy-head">
          <div><span>离线策略审查</span><h3 id="strategy-panel-title">离线策略候选审批</h3><p>用成对离线运行和安全门禁约束策略变更。</p></div>
          <button ref="strategyClose" type="button" aria-label="关闭离线策略审批" @click="closeStrategyPanel">×</button>
        </header>

        <div class="strategy-scroll">
          <section class="strategy-truth" role="note">
            <strong>仅离线评测，不代表线上 A/B；不会自动上线</strong>
            <p>策略版本来源固定为 <code>offline_eval</code>。审批只记录离线证据，批准后仍需人工显式激活；系统不会分配线上流量。</p>
          </section>

          <div v-if="evaluation.strategyError" class="strategy-feedback error" role="alert">
            <strong>操作失败</strong><span>{{ friendlyEvaluationError(evaluation.strategyError) }}</span>
          </div>
          <div v-if="evaluation.strategyMessage" class="strategy-feedback success" role="status" aria-live="polite">
            {{ evaluation.strategyMessage }}
          </div>

          <div v-if="evaluation.strategyLoading" class="strategy-loading" aria-live="polite">
            <span aria-hidden="true"></span>正在加载离线策略与审批记录…
          </div>

          <template v-else>
            <section class="strategy-section" aria-labelledby="strategy-pointer-title">
              <div class="strategy-section-head">
                <div><h4 id="strategy-pointer-title">当前策略指针</h4><p>仅影响离线评测默认策略，不代表生产发布。</p></div>
                <span>第 {{ evaluation.strategyPointer?.generation || 0 }} 代</span>
              </div>
              <div class="strategy-pointer-grid">
                <article class="strategy-version-card current">
                  <span>当前策略</span>
                  <strong>{{ strategyName(evaluation.currentStrategy, '尚未激活') }}</strong>
                  <small v-if="evaluation.currentStrategy" :title="evaluation.currentStrategy.manifest_checksum">校验和 {{ shortChecksum(evaluation.currentStrategy.manifest_checksum) }}</small>
                  <small v-else>审批和显式激活后才会建立指针</small>
                </article>
                <article class="strategy-version-card previous">
                  <span>上一策略</span>
                  <strong>{{ strategyName(evaluation.previousStrategy, '暂无可回滚版本') }}</strong>
                  <small v-if="evaluation.previousStrategy" :title="evaluation.previousStrategy.manifest_checksum">校验和 {{ shortChecksum(evaluation.previousStrategy.manifest_checksum) }}</small>
                  <small v-else>首次激活前没有上一版本</small>
                </article>
              </div>
              <div class="rollback-box">
                <div><strong>人工回滚</strong><p>危险操作：会把当前离线策略指针切回上一版本，请先填写原因并再次确认。</p></div>
                <label for="strategy-rollback-reason">回滚原因
                  <input id="strategy-rollback-reason" v-model.trim="rollbackReason" type="text" maxlength="500"
                         placeholder="例如：候选策略出现回归" :disabled="evaluation.strategyActionLoading" />
                </label>
                <button class="strategy-danger" type="button"
                        :disabled="evaluation.strategyActionLoading || !evaluation.previousStrategy || !rollbackReason"
                        @click="rollbackStrategy">确认人工回滚</button>
              </div>
            </section>

            <div class="strategy-form-grid">
              <section class="strategy-section" aria-labelledby="strategy-create-title">
                <div class="strategy-section-head"><div><h4 id="strategy-create-title">创建不可变策略版本</h4><p>填写名称和最小策略清单（JSON）；内容相同会复用已有版本。</p></div></div>
                <form class="strategy-form" @submit.prevent="createStrategy">
                  <label for="strategy-name">策略名称
                    <input id="strategy-name" v-model.trim="strategyForm.name" maxlength="200" required
                           placeholder="例如：RAG 召回候选 v2" :disabled="evaluation.strategyActionLoading" />
                  </label>
                  <label for="strategy-manifest">策略清单（JSON）
                    <textarea id="strategy-manifest" v-model="strategyForm.manifest" rows="5" required
                              spellcheck="false" aria-describedby="strategy-manifest-help"
                              :disabled="evaluation.strategyActionLoading"></textarea>
                  </label>
                  <small id="strategy-manifest-help">必须是非空 JSON 对象；创建后内容和校验和不可修改。</small>
                  <p v-if="strategyFormError" class="strategy-inline-error" role="alert">{{ strategyFormError }}</p>
                  <button class="strategy-primary" type="submit"
                          :disabled="evaluation.strategyActionLoading || !strategyForm.name || !strategyForm.manifest.trim()">
                    {{ evaluation.strategyActionLoading ? '正在处理…' : '创建离线策略' }}
                  </button>
                </form>
              </section>

              <section class="strategy-section" aria-labelledby="proposal-create-title">
                <div class="strategy-section-head"><div><h4 id="proposal-create-title">生成晋级候选</h4><p>只能比较两个已完成运行，候选运行必须绑定策略版本。</p></div></div>
                <form class="strategy-form" @submit.prevent="createProposal">
                  <label for="baseline-run">基线运行
                    <select id="baseline-run" v-model="proposalForm.baselineRunId" required :disabled="evaluation.strategyActionLoading">
                      <option value="" disabled>选择已完成的基线</option>
                      <option v-for="run in evaluation.completedRuns" :key="run.id" :value="run.id">{{ runOption(run) }}</option>
                    </select>
                  </label>
                  <label for="candidate-run">候选运行
                    <select id="candidate-run" v-model="proposalForm.candidateRunId" required :disabled="evaluation.strategyActionLoading">
                      <option value="" disabled>选择已完成的候选</option>
                      <option v-for="run in evaluation.completedRuns" :key="run.id" :value="run.id">{{ runOption(run) }}</option>
                    </select>
                  </label>
                  <p v-if="!evaluation.completedRuns.length" class="strategy-empty-inline">暂无已完成运行，请先完成评测。</p>
                  <p v-if="proposalFormError" class="strategy-inline-error" role="alert">{{ proposalFormError }}</p>
                  <button class="strategy-primary" type="submit"
                          :disabled="evaluation.strategyActionLoading || !proposalForm.baselineRunId || !proposalForm.candidateRunId">
                    {{ evaluation.strategyActionLoading ? '正在计算证据…' : '创建离线候选' }}
                  </button>
                </form>
              </section>
            </div>

            <section class="strategy-section proposal-workspace" aria-labelledby="proposal-list-title">
              <div class="strategy-section-head">
                <div><h4 id="proposal-list-title">候选审批记录</h4><p>统计证据不足、发布门禁或安全检查失败时会阻断。</p></div>
                <span>{{ evaluation.promotionProposals.length }} 条</span>
              </div>
              <div v-if="!evaluation.promotionProposals.length" class="strategy-empty">
                <strong>暂无策略候选</strong><span>先创建策略版本，并用绑定该版本的候选运行与基线运行进行比较。</span>
              </div>
              <div v-else class="proposal-grid">
                <nav class="proposal-list" aria-label="策略候选列表">
                  <button v-for="proposal in evaluation.promotionProposals" :key="proposal.id" type="button"
                          :class="{ active: evaluation.selectedProposal?.id === proposal.id }"
                          :aria-pressed="evaluation.selectedProposal?.id === proposal.id"
                          @click="evaluation.loadProposal(proposal.id)">
                    <span>{{ strategyName(strategyById(proposal.candidate_strategy_version_id), '策略候选') }}</span>
                    <small>{{ shortId(proposal.id) }} · {{ proposalStatusText(proposal.status) }}</small>
                  </button>
                </nav>

                <article v-if="selectedProposal" class="proposal-detail">
                  <div class="proposal-title">
                    <div><span>候选 {{ shortId(selectedProposal.id) }}</span><h5>{{ strategyName(selectedProposalStrategy, '未命名策略') }}</h5></div>
                    <b :class="`proposal-status status-${selectedProposal.status}`">{{ proposalStatusText(selectedProposal.status) }}</b>
                  </div>
                  <dl class="proposal-evidence">
                    <div><dt>统计结论</dt><dd>{{ statisticsConclusion(selectedProposal.statistics) }}</dd></div>
                    <div><dt>McNemar 检验 p 值</dt><dd>{{ numberOrDash(selectedProposal.statistics?.mcnemar?.p_value, 4) }}</dd></div>
                    <div><dt>Bootstrap 置信区间</dt><dd>{{ confidenceInterval(selectedProposal.statistics?.paired_bootstrap) }}</dd></div>
                    <div><dt>通过率变化</dt><dd>{{ selectedProposal.comparison?.pass_rate_delta == null ? '—' : signed(selectedProposal.comparison.pass_rate_delta) }}</dd></div>
                    <div><dt>修复 / 回归</dt><dd>{{ selectedProposal.comparison?.fixed?.length ?? '—' }} / {{ selectedProposal.comparison?.regressions?.length ?? '—' }}</dd></div>
                    <div><dt>审阅就绪</dt><dd>{{ selectedProposal.ready_for_review === true ? '是' : '否' }}</dd></div>
                    <div><dt>发布门禁</dt><dd>{{ evidenceConclusion(selectedProposal.release_gate) }}</dd></div>
                    <div><dt>安全检查</dt><dd>{{ evidenceConclusion(selectedProposal.safety) }}</dd></div>
                    <div><dt>自动上线</dt><dd>{{ selectedProposal.auto_activate === false ? '关闭（必须显式激活）' : '后端未声明' }}</dd></div>
                  </dl>
                  <div v-if="proposalBlockers.length" class="proposal-blockers">
                    <strong>阻断原因</strong>
                    <ul><li v-for="reason in proposalBlockers" :key="reason">{{ reason }}</li></ul>
                  </div>
                  <p v-else class="proposal-ready">未发现阻断原因，仍需人工复核统计结果和门禁。</p>

                  <label class="proposal-note" for="proposal-review-note">审批备注
                    <textarea id="proposal-review-note" v-model="reviewNote" rows="3" maxlength="1000"
                              placeholder="记录判断依据或拒绝原因" :disabled="evaluation.strategyActionLoading"></textarea>
                  </label>
                  <div v-if="canDecideProposal" class="proposal-actions">
                    <button class="strategy-secondary" type="button" :disabled="evaluation.strategyActionLoading"
                            @click="decideProposal('reject')">拒绝候选</button>
                    <button class="strategy-primary" type="button"
                            :disabled="evaluation.strategyActionLoading || !canApproveProposal"
                            @click="decideProposal('approve')">批准候选</button>
                  </div>
                  <div v-if="selectedProposal.status === 'approved'" class="activation-box">
                    <strong>批准不等于激活</strong>
                    <p>显式激活只更新离线评测策略指针，不会触发线上发布。</p>
                    <label for="activation-note">激活备注
                      <input id="activation-note" v-model.trim="activationNote" maxlength="1000"
                             placeholder="可选：记录本次激活依据" :disabled="evaluation.strategyActionLoading" />
                    </label>
                    <button class="strategy-activate" type="button" :disabled="evaluation.strategyActionLoading"
                            @click="activateProposal">显式激活离线策略</button>
                  </div>
                  <div v-if="selectedProposal.review_note" class="proposal-audit">
                    <strong>最近审批记录</strong><p>{{ selectedProposal.review_note }}</p>
                  </div>
                </article>
              </div>
            </section>
          </template>
        </div>
      </aside>

      <EvolutionSuggestionPanel v-if="evolutionPanelOpen" @close="closeEvolutionPanel" />

      <aside v-if="evaluation.selectedTrace" class="trace-drawer" role="dialog" aria-modal="true" aria-label="执行轨迹">
        <header><div><span>执行轨迹</span><h3>{{ caseName(evaluation.selectedTrace.case_id) }}</h3></div><button type="button" aria-label="关闭执行轨迹" @click="evaluation.selectedTrace = null">×</button></header>
        <div v-if="!evaluation.selectedTrace.trace?.length" class="eval-empty compact"><span>该用例没有执行轨迹。</span></div>
        <ol v-else aria-label="按执行顺序排列的轨迹事件">
          <li v-for="(event, index) in evaluation.selectedTrace.trace" :key="event.id || `${event.sequence}-${index}`">
            <span class="trace-seq">{{ String(event.sequence ?? index + 1).padStart(2, '0') }}</span>
            <article class="trace-card">
              <div class="trace-card-head">
                <b>{{ eventTypeText(event.event_type || event.type) }}</b>
                <span v-if="event.duration_ms != null">{{ durationText(event.duration_ms) }}</span>
              </div>
              <p v-if="event.name || event.status">{{ eventDetailText(event.name || event.status) }}</p>
              <dl v-if="traceFields(event).length" class="trace-fields">
                <div v-for="field in traceFields(event)" :key="field.key">
                  <dt>{{ field.label }}</dt><dd>{{ field.value }}</dd>
                </div>
              </dl>
              <p v-else class="trace-no-data">此步骤没有附加数据。</p>
            </article>
          </li>
        </ol>
      </aside>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { useAuth } from '../stores/auth'
import { useEvaluation } from '../stores/evaluation'
import EvolutionSuggestionPanel from './EvolutionSuggestionPanel.vue'

const emit = defineEmits(['close'])
const evaluation = useEvaluation()
const auth = useAuth()
const strategyPanelOpen = ref(false)
const evolutionPanelOpen = ref(false)
const strategyTrigger = ref(null)
const evolutionTrigger = ref(null)
const strategyClose = ref(null)
const strategyForm = reactive({
  name: '',
  manifest: '{\n  "prompt_version": "candidate-v1"\n}',
})
const proposalForm = reactive({ baselineRunId: '', candidateRunId: '' })
const strategyFormError = ref('')
const proposalFormError = ref('')
const reviewNote = ref('')
const activationNote = ref('')
const rollbackReason = ref('')
const summary = computed(() => evaluation.summary || {})
const gateText = computed(() => summary.value.release_gate_passed === true ? '通过' : summary.value.release_gate_passed === false ? '阻断' : '待评测')
const gateClass = computed(() => summary.value.release_gate_passed === true ? 'pass' : summary.value.release_gate_passed === false ? 'fail' : '')
const isBusy = computed(() => evaluation.bootstrapping || evaluation.runningLive || evaluation.strategyActionLoading)
const operationCompleted = computed(() => Number(evaluation.activeOperation?.completed || 0))
const operationTotal = computed(() => Number(evaluation.activeOperation?.total || 0))
const operationPercent = computed(() => operationTotal.value > 0
  ? Math.min(100, Math.round(operationCompleted.value / operationTotal.value * 100))
  : 0)
const replayButtonText = computed(() => {
  if (!evaluation.bootstrapping) return '运行合成回放'
  return operationTotal.value
    ? `正在回放 ${operationCompleted.value}/${operationTotal.value} 次执行`
    : '正在准备合成回放'
})
const liveButtonText = computed(() => {
  if (evaluation.runningLive) {
    return operationTotal.value
      ? `本地实测 ${operationCompleted.value}/${operationTotal.value} 条用例`
      : '正在准备本地实测'
  }
  if (evaluation.readinessLoading) return '正在检查本地实测'
  return evaluation.liveReadiness?.llm?.mode === 'mock'
    ? '运行本地智能体实测（模拟模型）'
    : '运行本地智能体实测'
})
const readinessMessages = computed(() => evaluation.liveReady
  ? evaluation.liveWarnings
  : evaluation.liveReasons)
const liveReadinessTitle = computed(() => {
  if (evaluation.readinessLoading) return '正在检查本地实测条件'
  if (!evaluation.liveReady) return '本地智能体实测暂不可用'
  return evaluation.liveReadiness?.llm?.mode === 'mock'
    ? '本地智能体可运行，当前使用模拟模型'
    : '本地智能体实测已就绪'
})
const liveReadinessClass = computed(() => evaluation.readinessLoading
  ? 'checking'
  : evaluation.liveReady ? (evaluation.liveReadiness?.llm?.mode === 'mock' ? 'limited' : 'ready') : 'blocked')
const operationStatusText = computed(() => {
  if (!isBusy.value) return evaluation.operationMessage
  const action = evaluation.activeOperation?.kind === 'live' ? '本地智能体实测' : '合成回放'
  if (!operationTotal.value) return `${action}正在准备测试数据和执行环境。`
  return `${action}正在运行：已完成 ${operationCompleted.value}/${operationTotal.value} 次执行。`
})
const runProgressText = computed(() => {
  const completed = Number(summary.value.completed || 0)
  const total = Number(summary.value.total || 0)
  if (evaluation.selectedRun?.status === 'running') return `${completed} / ${total} 条已完成`
  return `${Number(summary.value.passed || 0)} / ${total} 条通过`
})
const selectedRunFacts = computed(() => {
  const run = evaluation.selectedRun || {}
  const execution = evaluation.selectedExecution || {}
  const adapter = firstMeaningful(
    objectLabel(execution.adapter), execution.adapter_type, execution.adapter_name,
    run.config?.adapter?.type, run.summary?.adapter?.name,
  )
  const executionMode = firstMeaningful(
    execution.execution, execution.execution_mode, execution.mode,
    run.metadata?.execution_mode,
  )
  const dataPolicy = firstMeaningful(
    execution.data_policy, execution.dataPolicy, run.metadata?.data_policy,
    adapter === 'replay' || adapter === 'replay-agent' ? 'synthetic-only' : '',
  )
  const truth = firstMeaningful(
    execution.real_vs_mock, execution.realVsMock, execution.runtime_mode,
    execution.truth_label, run.metadata?.real_vs_mock,
    adapter === 'replay' || adapter === 'replay-agent' ? 'synthetic' : '',
  )
  return [
    { label: '适配器', value: adapterText(adapter) },
    { label: '执行方式', value: executionModeText(executionMode, adapter) },
    { label: '数据口径', value: dataPolicyText(dataPolicy) },
    { label: '真实性', value: truthText(truth), tone: truthTone(truth) },
  ]
})
const selectedProposal = computed(() => evaluation.selectedProposal || null)
const selectedProposalStrategy = computed(() => strategyById(selectedProposal.value?.candidate_strategy_version_id))
const proposalBlockers = computed(() => {
  const proposal = selectedProposal.value || {}
  return uniqueReasons([
    ...(proposal.blocked_reasons || []),
    ...(proposal.statistics?.reasons || []),
    ...(proposal.release_gate?.reasons || []),
    ...(proposal.safety?.reasons || []),
  ])
})
const canDecideProposal = computed(() => selectedProposal.value?.status === 'proposed')
const canApproveProposal = computed(() => selectedProposal.value?.status === 'proposed'
  && selectedProposal.value?.ready_for_review !== false
  && proposalBlockers.value.length === 0)

const caseNames = {
  insurance_missing_city: '缺少参保城市时先澄清',
  insurance_tool_happy_path: '医保政策查询正常流程',
  tool_timeout_fallback: '工具超时后的兜底处理',
  malformed_tool_payload: '工具返回异常数据时的处理',
  service_matching_boundary: '科室服务匹配边界',
  report_explanation_boundary: '报告解读边界',
  privacy_redaction: '隐私信息脱敏',
  rag_prompt_injection: '知识库指令注入防护',
  multiturn_slot_correction: '多轮对话信息更正',
  ambiguous_multi_intent: '多个模糊意图的澄清',
  emergency_escalation: '紧急症状升级处理',
  trace_tool_pair: '工具调用与结果成对记录',
  intent_smalltalk_no_tool: '普通寒暄不调用工具',
  intent_ambiguous_clarification: '互斥意图先澄清',
  slot_nested_extraction: '提取嵌套日期和目的地',
  slot_multiturn_latest_value: '采用多轮更正后的信息',
  tool_search_selection: '时效问题选择搜索工具',
  tool_abstain_missing_required_slot: '缺少必要信息时暂不调用工具',
  tool_document_exact_arguments: '文档工具参数完整准确',
  trace_research_write_dag: '检索后写作的依赖顺序',
  trace_parallel_research_fanout: '并行检索后统一汇总',
  safety_secret_exfiltration_refusal: '拒绝泄露系统密钥',
  safety_rag_prompt_injection: '知识库指令注入防护',
  safety_destructive_action_confirmation: '破坏性操作需要确认',
  fallback_tool_timeout: '工具超时后的降级处理',
  fallback_malformed_tool_result: '工具异常结果防编造',
  fallback_unknown_tool: '未知工具安全回退',
  fallback_partial_failure_local_evidence: '部分失败时仅使用可用证据',
  fallback_rate_limit_retry: '限流后重试恢复',
  trace_replan_after_dependency_failure: '依赖失败后重新规划',
  trace_sequence_integrity: '轨迹顺序及调用结果完整',
  memory_recall_user_city: '读取当前用户的城市记忆',
  memory_write_explicit_preference: '写入用户明确表达的偏好',
  memory_correct_latest_preference: '偏好更正后保留最新值',
  memory_reject_prompt_injection: '拒绝记忆提示注入内容',
  memory_reject_sensitive_secret: '拒绝保存敏感凭证',
  memory_cross_tenant_isolation: '禁止跨账号读取记忆',
}

const statusNames = {
  completed: '已完成', passed: '已通过', failed: '未通过', error: '出错',
  running: '运行中', cancelled: '已取消', pending: '等待中',
  open: '待处理', triaged: '已分诊', resolved: '已解决', verified: '已复验',
  ok: '正常', success: '成功', timeout: '超时', proposed: '待审批', blocked: '已阻断',
  approved: '已批准 / 未激活', rejected: '已拒绝', activated: '已激活', inconclusive: '证据不足',
}

const eventTypeNames = {
  user_message: '用户消息', intent_predicted: '识别用户意图', slot_extracted: '提取关键信息',
  retrieval: '检索知识库', llm_call: '调用语言模型', tool_call: '调用工具',
  tool_result: '工具返回结果', guardrail: '安全规则检查', final_response: '生成最终回复',
  fallback: '执行兜底方案', error: '发生错误', route: '选择处理路径', memory: '读取记忆',
  rag_result: '知识库检索结果', done: '处理完成', event: '处理事件', plan: '生成任务计划',
  planning: '生成任务计划', validation: '验证执行结果', memory_read: '读取记忆',
  memory_write: '写入记忆', retry: '重试任务', clarification: '请求补充信息',
}

const commonNames = {
  'fixed-agent': '修复后版本', 'baseline-agent': '修复前版本', 'local-agent-live': '本地智能体实测',
  policy_search: '医保政策查询工具', web_search: '网页搜索工具', service_router: '服务匹配工具',
  intent: '意图', slots: '提取信息', city: '城市', insurance_type: '医保类型',
  content: '回复内容', evidence_ids: '证据编号', fallback: '兜底处理', trace: '执行轨迹',
  arguments: '参数', tool_calls: '工具调用', name: '名称', query: '查询内容', q: '查询内容',
  error: '错误', status: '状态', message: '消息', mode: '处理模式', tool_name: '工具名称',
  params: '参数', tool_result: '工具结果', success: '是否成功', role: '角色',
  route: '处理路径', path: '处理路径', reason: '原因', duration_ms: '耗时', model: '模型',
  input: '输入', output: '输出', result: '结果', results: '结果', count: '数量', top_k: '召回数量',
  documents: '文档', chunks: '知识片段', sources: '来源', citations: '引用', score: '分数',
  steps: '计划步骤', nodes: '任务节点', dependencies: '依赖关系', attempt: '尝试次数',
  memory_reads: '读取的记忆', memory_writes: '写入的记忆', content_length: '回复长度',
  abstained: '是否拒答', prompt_tokens: '输入词元数', completion_tokens: '输出词元数',
  insurance_policy_query: '医保政策查询', knowledge_query: '知识库查询', appointment: '预约服务',
  ambiguous_multi_intent: '多个模糊意图', user: '用户', assistant: '智能体', tool: '工具', system: '系统',
  intent_accuracy: '意图识别准确率', required_slot_recall: '必要信息召回率',
  tool_selection_f1: '工具选择 F1', tool_argument_accuracy: '工具参数准确率',
  tool_outcome_accuracy: '工具结果状态准确率', required_content_recall: '必要回复内容召回率',
  rag_evidence_f1: 'RAG 证据 F1', rag_recall_at_k: 'RAG 召回率', rag_mrr: 'RAG 首条命中排序',
  rag_ndcg_at_k: 'RAG 分级排序质量', no_answer_decision: '无答案判断准确率',
  memory_behavior: '记忆读写正确性', fallback_recovery: '异常兜底恢复率',
  privacy_non_disclosure: '隐私防泄露', boundary_safety: '安全边界', trace_completeness: '执行轨迹完整性',
  true: '是', false: '否', null: '无',
}

const categoryNames = {
  INFRA: '基础设施', TOOL: '工具调用', SAFETY: '安全问题', RESPONSE: '回复质量',
  RAG: '知识库检索', INTENT: '意图识别', FALLBACK: '兜底处理', TRACE: '执行轨迹',
  SLOT_OR_STATE: '信息与记忆', TOOL_ARGUMENT: '工具参数', GENERATION: '回复生成',
  RETRIEVAL: '知识检索', TOOL_RUNTIME: '工具运行',
  UNCLASSIFIED: '尚未分类',
}

const adapterNames = {
  replay: '合成回放适配器', 'replay-agent': '合成回放适配器',
  local: '本地智能体适配器', 'local-agent': '本地智能体适配器',
  http: 'HTTP 智能体适配器', 'http-agent': 'HTTP 智能体适配器',
}

const executionModeNames = {
  'preauthored-output-replay': '读取预置输出与轨迹', replay: '读取预置输出与轨迹',
  'synthetic-replay': '读取预置输出与轨迹', synthetic_replay: '读取预置输出与轨迹',
  'live-agent': '调用本地智能体主链', live_agent: '调用本地智能体主链',
  live: '调用本地智能体主链', local: '调用本地智能体主链',
}

const dataPolicyNames = {
  'synthetic-only': '仅合成、脱敏数据', synthetic: '仅合成、脱敏数据',
  'synthetic-evaluation-inputs': '合成输入 / 真实执行',
  'versioned-evaluation-dataset': '版本化评测数据', 'versioned-local-dataset': '版本化本地评测数据',
  desensitized: '脱敏业务数据', 'desensitized-real': '脱敏真实业务数据',
}

const truthNames = {
  synthetic: '合成回放（非真实调用）', replay: '合成回放（非真实调用）',
  real: '真实模型链路', live: '真实模型链路',
  fallback: '降级链路', degraded: '降级链路', mock: '模拟模型链路（Mock）', unknown: '后端未声明',
}

function percent(value) { return value == null ? '0%' : `${(Number(value) * 100).toFixed(1)}%` }
function score(value) { return value == null ? '暂无' : Number(value).toFixed(3) }
function shortId(value) { return String(value || '').slice(0, 8) || '未知' }
function signed(value) { const n = Number(value || 0); return `${n > 0 ? '+' : ''}${(n * 100).toFixed(1)}%` }
function statusClass(value) { return `status status-${String(value || '').toLowerCase()}` }
function severityClass(value) { return `severity severity-${String(value || 'medium').toLowerCase()}` }
function caseName(value) { return caseNames[value] || value || '未命名用例' }
function runName(value) { return commonNames[value] || value || '' }
function statusText(value) { return statusNames[String(value || '').toLowerCase()] || value || '未知状态' }
function severityText(value) { return ({ S0: '严重', S1: '高危', S2: '中等', S3: '较低' })[value] || value || '未定级' }
function categoryText(value) { return categoryNames[String(value || 'UNCLASSIFIED').toUpperCase()] || value || '尚未分类' }
function eventTypeText(value) { return eventTypeNames[String(value || '').toLowerCase()] || value || '处理事件' }
function firstMeaningful(...values) { return values.find(value => value !== undefined && value !== null && value !== '') || '' }
function objectLabel(value) {
  if (!value || typeof value !== 'object') return value
  return firstMeaningful(value.type, value.name, value.adapter, value.mode)
}
function adapterText(value) {
  const key = String(value || '').toLowerCase()
  return adapterNames[key] || value || '未声明'
}
function executionModeText(value, adapter) {
  const key = String(value || '').toLowerCase()
  if (executionModeNames[key]) return executionModeNames[key]
  const adapterKey = String(adapter || '').toLowerCase()
  if (adapterKey.includes('replay')) return executionModeNames.replay
  if (adapterKey.includes('local')) return executionModeNames.local
  return value || '未声明'
}
function dataPolicyText(value) {
  const key = String(value || '').toLowerCase()
  return dataPolicyNames[key] || value || '未声明'
}
function truthText(value) {
  const key = String(value || '').toLowerCase()
  return truthNames[key] || value || '后端未声明'
}
function truthTone(value) {
  const key = String(value || '').toLowerCase()
  if (['real', 'live'].includes(key)) return 'real'
  if (['mock', 'fallback', 'degraded'].includes(key)) return 'warning'
  if (['synthetic', 'replay'].includes(key)) return 'synthetic'
  return 'unknown'
}
function runModeText(run) {
  const adapter = String(run?.config?.adapter?.type || run?.summary?.adapter?.name || '').toLowerCase()
  if (adapter.includes('replay')) return '合成回放'
  if (adapter.includes('local')) return '本地实测'
  if (adapter.includes('http')) return '真实 HTTP'
  return '模式待确认'
}
function eventDetailText(value) {
  const key = String(value ?? '')
  return commonNames[key] || statusNames[key.toLowerCase()] || key || '正常'
}
function hasPayload(value) {
  if (value == null) return false
  if (Array.isArray(value)) return value.length > 0
  if (typeof value === 'object') return Object.keys(value).length > 0
  return String(value).length > 0
}
function failedMetrics(item) {
  const value = item.failed_metrics || item.resolution?.failed_metrics || []
  return value.length ? value.map(item => commonNames[item] || categoryNames[String(item).toUpperCase()] || item).join(' / ') : '等待分析原因'
}
function formatFieldValue(value) {
  if (value == null) return '无'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (Array.isArray(value)) {
    if (!value.length) return '无'
    return value.map(item => formatFieldValue(item)).join('；')
  }
  if (typeof value === 'object') {
    const entries = Object.entries(value).filter(([, item]) => hasPayload(item))
    if (!entries.length) return '无'
    return entries.map(([key, item]) => `${commonNames[key] || key}：${formatFieldValue(item)}`).join('；')
  }
  const key = String(value)
  return String(commonNames[key] || statusNames[key.toLowerCase()] || value)
}
function traceFields(event) {
  const payload = event?.payload
  if (!hasPayload(payload) || typeof payload !== 'object') return []
  if (Array.isArray(payload)) return [{ key: 'data', label: '数据', value: formatFieldValue(payload) }]
  return Object.entries(payload)
    .filter(([, value]) => hasPayload(value))
    .map(([key, value]) => ({ key, label: commonNames[key] || key, value: formatFieldValue(value) }))
}
function durationText(value) {
  const duration = Number(value)
  if (!Number.isFinite(duration)) return ''
  return duration >= 1000 ? `${(duration / 1000).toFixed(2)} 秒` : `${Math.round(duration)} 毫秒`
}
function strategyById(id) {
  if (!id) return null
  return evaluation.strategies.find(item => item.id === id)
    || (evaluation.currentStrategy?.id === id ? evaluation.currentStrategy : null)
    || (evaluation.previousStrategy?.id === id ? evaluation.previousStrategy : null)
}
function strategyName(strategy, fallback = '未命名策略') {
  if (!strategy) return fallback
  const version = strategy.version ? `v${strategy.version} · ` : ''
  return `${version}${strategy.name || shortId(strategy.id)}`
}
function shortChecksum(value) {
  const checksum = String(value || '')
  return checksum ? `${checksum.slice(0, 12)}…` : '未提供'
}
function runOption(run) {
  const strategy = strategyById(run.strategy_version_id)
  const strategyLabel = run.strategy_version_id
    ? strategyName(strategy, `策略 ${shortId(run.strategy_version_id)}`)
    : '未绑定策略'
  return `${runName(run.name) || shortId(run.id)} · ${strategyLabel}`
}
function proposalStatusText(value) {
  return statusNames[String(value || '').toLowerCase()] || value || '未知状态'
}
function numberOrDash(value, digits = 3) {
  const number = Number(value)
  return Number.isFinite(number) ? number.toFixed(digits) : '—'
}
function confidenceInterval(value) {
  if (!value || typeof value !== 'object') return '—'
  const low = numberOrDash(value.ci_low, 4)
  const high = numberOrDash(value.ci_high, 4)
  return low === '—' && high === '—' ? '—' : `[${low}, ${high}]`
}
function statisticsConclusion(value) {
  if (!value || typeof value !== 'object') return '尚无统计结论'
  const status = String(value.status || '').toLowerCase()
  if (value.passed === true || status === 'passed') return '候选显著优于基线'
  if (status === 'inconclusive') return '统计证据不足'
  if (status === 'blocked' || value.passed === false) return '未证明候选有稳定增益'
  return statusText(status)
}
function evidenceConclusion(value) {
  if (!value || typeof value !== 'object' || !Object.keys(value).length) return '后端未声明'
  const conclusion = firstMeaningful(
    value.passed,
    value.status,
    value.release_gate_passed,
    value.candidate?.passed,
    value.candidate?.release_gate_passed,
  )
  if (conclusion === true) return '通过'
  if (conclusion === false) return '未通过'
  return statusText(conclusion)
}
function uniqueReasons(values) {
  const normalised = values.flatMap(value => {
    if (!value) return []
    if (typeof value === 'string') return [value]
    if (typeof value === 'object') return [value.message || value.reason || value.detail || value.code || '未说明的阻断项']
    return [String(value)]
  }).map(value => strategyReasonText(String(value).trim())).filter(Boolean)
  return [...new Set(normalised)]
}
function strategyReasonText(value) {
  const exact = {
    statistics_inconclusive: '统计证据不足', statistics_failed: '统计检验未通过',
    release_gate_failed: '发布门禁未通过', s0_s1_safety_failure: '存在严重或高危安全失败',
    protocol_mismatch: '基线与候选的评测协议不一致', case_regressions: '候选版本出现逐用例回归',
  }
  if (exact[value]) return exact[value]
  let match = value.match(/^requires at least (\d+) paired cases$/i)
  if (match) return `至少需要 ${match[1]} 条成对用例`
  match = value.match(/^requires at least (\d+) discordant pair(?:s)?$/i)
  if (match) return `至少需要 ${match[1]} 个结果不同的成对用例`
  match = value.match(/^requires at least (\d+) paired scores$/i)
  if (match) return `至少需要 ${match[1]} 组成对分数`
  return value
}
function friendlyEvaluationError(value) {
  const text = String(value || '')
  if (text === 'unauthorized') return '登录状态已失效，请重新登录。'
  if (text.includes('experiment_admin')) return '当前账号没有策略管理员权限。'
  if (text.includes('experiment_approver')) return '当前账号没有策略审批权限。'
  if (text.includes('generation conflict') || text.includes('generation changed')) return '记录已被其他操作更新，请刷新后重试。'
  if (text.includes('creator cannot')) return '创建者不能审批自己的记录，请由另一位审批人操作。'
  if (text.includes('evidence changed') || text.includes('integrity')) return '证据完整性校验失败，请刷新并重新生成候选。'
  if (/^[a-z0-9_.:-]+$/i.test(text)) return `操作未完成（错误代码：${text}）`
  return text || '评测操作失败'
}
async function openStrategyPanel() {
  evaluation.selectedTrace = null
  strategyPanelOpen.value = true
  evaluation.strategyMessage = ''
  await nextTick()
  strategyClose.value?.focus()
  await evaluation.loadStrategyWorkspace().catch(() => {})
  const completed = evaluation.completedRuns
  const candidate = completed.find(run => run.strategy_version_id)
  if (!proposalForm.candidateRunId && candidate) proposalForm.candidateRunId = candidate.id
  if (!proposalForm.baselineRunId) {
    const baseline = completed.find(run => run.id !== proposalForm.candidateRunId
      && (!candidate || run.dataset_version_id === candidate.dataset_version_id))
      || completed.find(run => run.id !== proposalForm.candidateRunId)
    if (baseline) proposalForm.baselineRunId = baseline.id
  }
}
async function closeStrategyPanel() {
  strategyPanelOpen.value = false
  await nextTick()
  strategyTrigger.value?.focus()
}
async function openEvolutionPanel() {
  evaluation.selectedTrace = null
  strategyPanelOpen.value = false
  evolutionPanelOpen.value = true
}
async function closeEvolutionPanel() {
  evolutionPanelOpen.value = false
  await nextTick()
  evolutionTrigger.value?.focus()
}
async function createStrategy() {
  strategyFormError.value = ''
  let manifest
  try {
    manifest = JSON.parse(strategyForm.manifest)
  } catch {
    strategyFormError.value = '策略清单不是有效 JSON，请检查引号、逗号和括号。'
    return
  }
  if (!manifest || Array.isArray(manifest) || typeof manifest !== 'object' || !Object.keys(manifest).length) {
    strategyFormError.value = '策略清单必须是至少包含一个字段的 JSON 对象。'
    return
  }
  await evaluation.createStrategy(strategyForm.name.trim(), manifest).catch(() => {})
}
async function createProposal() {
  proposalFormError.value = ''
  if (proposalForm.baselineRunId === proposalForm.candidateRunId) {
    proposalFormError.value = '基线运行和候选运行不能相同。'
    return
  }
  const candidate = evaluation.completedRuns.find(run => run.id === proposalForm.candidateRunId)
  if (!candidate?.strategy_version_id) {
    proposalFormError.value = '候选运行未绑定策略版本，不能建立可审计候选。'
    return
  }
  await evaluation.createPromotionProposal(
    proposalForm.baselineRunId,
    proposalForm.candidateRunId,
  ).catch(() => {})
}
async function decideProposal(decision) {
  const proposal = selectedProposal.value
  if (!proposal) return
  const action = decision === 'approve' ? '批准' : '拒绝'
  if (!window.confirm(`确认${action}这个离线策略候选？该决定会写入审计记录，但不会自动上线。`)) return
  await evaluation.decideProposal(proposal.id, decision, reviewNote.value.trim()).catch(() => {})
  reviewNote.value = ''
}
async function activateProposal() {
  const proposal = selectedProposal.value
  if (!proposal) return
  const strategy = selectedProposalStrategy.value
  const confirmed = window.confirm(
    `确认显式激活“${strategyName(strategy)}”？\n\n这只会修改离线评测策略指针，不会发布线上流量。`,
  )
  if (!confirmed) return
  await evaluation.activateProposal(proposal.id, activationNote.value.trim()).catch(() => {})
  activationNote.value = ''
}
async function rollbackStrategy() {
  if (!evaluation.currentStrategy || !evaluation.previousStrategy || !rollbackReason.value) return
  const confirmed = window.confirm(
    `危险操作：确认从“${strategyName(evaluation.currentStrategy)}”回滚到“${strategyName(evaluation.previousStrategy)}”？\n\n回滚原因：${rollbackReason.value}`,
  )
  if (!confirmed) return
  await evaluation.rollbackStrategy(rollbackReason.value).catch(() => {})
  rollbackReason.value = ''
}
function closeTopLayer() {
  if (evaluation.selectedTrace) evaluation.selectedTrace = null
  else if (evolutionPanelOpen.value) closeEvolutionPanel()
  else if (strategyPanelOpen.value) closeStrategyPanel()
  else emit('close')
}

onMounted(() => evaluation.refresh())
</script>

<style scoped>
.eval-backdrop{position:fixed;inset:0;z-index:90;background:rgba(15,23,42,.45);backdrop-filter:blur(8px);display:grid;place-items:center;padding:22px}
.eval-workbench{position:relative;width:min(1480px,100%);height:min(900px,calc(100dvh - 44px));overflow:hidden;background:#f7f8fb;border:1px solid #dfe3ec;border-radius:16px;box-shadow:0 24px 80px rgba(26,38,68,.22);color:#172033;display:flex;flex-direction:column}
.eval-head{display:flex;align-items:center;justify-content:space-between;gap:24px;padding:22px 26px;background:#fff;border-bottom:1px solid #e5e8ef}
.offline-workspace{min-height:0;display:flex;flex:1;flex-direction:column;overflow:hidden}
.eval-kicker{margin:0 0 4px;color:#d92f64;font:700 11px/1.2 ui-monospace,SFMono-Regular,Consolas,monospace;letter-spacing:.11em}.eval-head h2{margin:0;font-size:23px;letter-spacing:-.02em}.eval-head p:not(.eval-kicker){margin:5px 0 0;color:#697386;font-size:13px}.eval-actions{display:flex;align-items:center;gap:8px}.eval-btn,.report-actions button,.badcase-item button{border:1px solid #d7dce6;background:#fff;color:#263149;border-radius:8px;padding:9px 13px;font-weight:650;cursor:pointer;white-space:nowrap}.eval-btn:active,.report-actions button:active,.badcase-item button:active{transform:translateY(1px)}.eval-btn.primary{background:#d92f64;border-color:#d92f64;color:#fff}.eval-btn:disabled{opacity:.6;cursor:wait}.eval-close{border:0;background:transparent;font-size:25px;color:#697386;cursor:pointer;padding:4px 7px}.eval-error{margin:14px 26px 0;padding:11px 13px;border-left:3px solid #c62846;background:#fff0f3;color:#8c1832;display:flex;gap:12px;font-size:13px}.eval-kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));margin:18px 26px 0;background:#fff;border:1px solid #e2e6ee;border-radius:12px}.eval-kpi{padding:15px 18px;border-right:1px solid #e6e9f0;display:grid;grid-template-columns:1fr auto;gap:2px 12px}.eval-kpi:last-child{border:0}.eval-kpi span{color:#697386;font-size:12px}.eval-kpi strong{grid-row:1/3;grid-column:2;font-size:25px;letter-spacing:-.04em}.eval-kpi small{color:#98a1b2}.eval-kpi.pass strong{color:#177a52}.eval-kpi.fail strong{color:#c62846}.eval-body{min-height:0;display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:16px;padding:16px 26px 24px;flex:1}.eval-main,.eval-aside{min-height:0;background:#fff;border:1px solid #e2e6ee;border-radius:12px;overflow:hidden;display:flex;flex-direction:column}.panel-head{display:flex;justify-content:space-between;align-items:center;padding:15px 17px;border-bottom:1px solid #edf0f4}.panel-head h3{margin:0;font-size:15px}.panel-head p{margin:3px 0 0;color:#8b95a7;font-size:11px}.report-actions{display:flex;gap:6px}.report-actions button{font-size:11px;padding:6px 9px}.run-strip{display:flex;gap:7px;overflow:auto;padding:12px 16px;border-bottom:1px solid #edf0f4}.run-chip{flex:0 0 auto;display:flex;align-items:center;gap:8px;border:1px solid #dfe3ea;border-radius:8px;background:#fff;padding:8px 10px;color:#374158;cursor:pointer}.run-chip.active{border-color:#d92f64;background:#fff5f8}.status{font-size:10px;padding:2px 5px;border-radius:4px;background:#edf0f4;color:#657086;text-transform:uppercase}.status-completed,.status-passed{background:#e5f5ed;color:#177a52}.status-failed,.status-error{background:#fde9ed;color:#b72543}.status-running{background:#fff0d5;color:#946200}.compare-line{display:flex;gap:20px;padding:10px 17px;background:#f9fafc;border-bottom:1px solid #edf0f4;color:#536077;font-size:12px}.result-table{overflow:auto}.result-row{width:100%;display:grid;grid-template-columns:1.2fr 1fr 70px 82px 90px;gap:12px;align-items:center;text-align:left;padding:10px 17px;border:0;border-bottom:1px solid #eef0f4;background:#fff;color:#39445b;font-size:12px}.result-row:not(.result-head){cursor:pointer}.result-row:not(.result-head):hover{background:#faf3f6}.result-head{position:sticky;top:0;z-index:1;background:#f5f7fa;color:#778196;font-weight:700}.case-id{font:600 11px ui-monospace,SFMono-Regular,Consolas,monospace;color:#263149}.trace-link{color:#c62659;font-weight:650}.badcase-list{overflow:auto;padding:4px 15px 15px}.badcase-item{padding:13px 2px;border-bottom:1px solid #eceff4}.badcase-top{display:flex;justify-content:space-between;align-items:center}.badcase-top b{font:650 12px ui-monospace,SFMono-Regular,Consolas,monospace}.badcase-item p{margin:6px 0;color:#505c73;font-size:11px}.badcase-item small{display:block;color:#8b95a7;line-height:1.5}.badcase-item button{margin-top:9px;padding:6px 8px;font-size:11px}.owner{display:block;margin-top:8px;color:#657086;font-size:10px}.severity{font-size:10px;font-weight:750;text-transform:uppercase}.severity-critical,.severity-high{color:#b72543}.severity-medium{color:#946200}.severity-low{color:#177a52}.eval-empty{display:grid;place-items:center;align-content:center;gap:7px;min-height:220px;padding:24px;text-align:center;color:#8791a3}.eval-empty strong{color:#364158}.eval-empty.compact{min-height:130px}.eval-skeleton{margin:16px 26px;display:grid;gap:10px}.eval-skeleton span{height:46px;border-radius:8px;background:linear-gradient(90deg,#eef1f5,#fafbfc,#eef1f5);background-size:200% 100%;animation:eval-shimmer 1.2s infinite}.trace-drawer{position:absolute;right:0;top:0;bottom:0;width:min(480px,90vw);z-index:3;background:#fff;border-left:1px solid #dfe3ea;box-shadow:-18px 0 50px rgba(28,39,66,.15);overflow:auto}.trace-drawer header{position:sticky;top:0;display:flex;justify-content:space-between;align-items:center;padding:18px;background:#fff;border-bottom:1px solid #e8ebf0}.trace-drawer header span{font:700 10px ui-monospace,SFMono-Regular,Consolas,monospace;color:#d92f64;letter-spacing:.1em}.trace-drawer h3{margin:3px 0 0;font-size:15px}.trace-drawer header button{border:0;background:transparent;font-size:24px;cursor:pointer}.trace-drawer ol{list-style:none;margin:0;padding:18px}.trace-drawer li{display:grid;grid-template-columns:35px 1fr;gap:10px;padding:0 0 18px;position:relative}.trace-drawer li:not(:last-child):before{content:"";position:absolute;left:14px;top:25px;bottom:1px;width:1px;background:#dfe3ea}.trace-seq{z-index:1;width:29px;height:25px;display:grid;place-items:center;border:1px solid #dfe3ea;border-radius:6px;background:#fff;font:650 10px ui-monospace,SFMono-Regular,Consolas,monospace}.trace-drawer li b{font-size:12px}.trace-drawer li p{margin:3px 0;color:#7d8799;font-size:11px}.trace-drawer pre{max-height:150px;overflow:auto;margin:7px 0 0;padding:9px;background:#f5f7fa;border-radius:7px;font-size:10px;white-space:pre-wrap}.eval-close:focus-visible,.eval-btn:focus-visible,.run-chip:focus-visible,.result-row:focus-visible{outline:2px solid #d92f64;outline-offset:2px}@keyframes eval-shimmer{to{background-position:-200% 0}}@media(prefers-reduced-motion:reduce){.eval-skeleton span{animation:none}}@media(max-width:900px){.eval-backdrop{padding:0}.eval-workbench{height:100dvh;border-radius:0}.eval-head{align-items:flex-start;padding:16px}.eval-head p:not(.eval-kicker){display:none}.eval-actions{flex-wrap:wrap;justify-content:flex-end}.eval-kpis{grid-template-columns:repeat(2,1fr);margin:12px 14px 0}.eval-kpi:nth-child(2){border-right:0}.eval-kpi{border-bottom:1px solid #e6e9f0}.eval-body{grid-template-columns:1fr;padding:12px 14px}.eval-aside{min-height:260px}.result-row{grid-template-columns:1.4fr 62px 74px 74px}.result-row span:nth-child(2){display:none}}@media(max-width:560px){.eval-head{display:block}.eval-actions{margin-top:12px;justify-content:flex-start}.eval-btn{padding:8px}.eval-kpis{grid-template-columns:1fr 1fr}.eval-kpi{padding:11px}.eval-kpi strong{font-size:19px}.eval-body{overflow:auto}.eval-main,.eval-aside{min-height:420px}}
.eval-mode-summary{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(270px,.85fr);gap:12px;margin:16px 26px 0}
.truth-note,.readiness-card{min-height:76px;border:1px solid #e2e6ee;border-radius:12px;background:#fff;padding:13px 15px}
.truth-note{display:grid;grid-template-columns:auto minmax(0,1fr);align-items:start;gap:12px;border-color:#efc8d5;background:#fff8fa}
.truth-note .mode-mark{display:inline-flex;align-items:center;justify-content:center;min-height:26px;padding:0 8px;border-radius:999px;background:#f7dbe4;color:#9f1f4a;font-size:11px;font-weight:800;letter-spacing:.05em}
.truth-note strong{display:block;font-size:13px;line-height:1.45;color:#72203c}.truth-note p{margin:4px 0 0;color:#684b56;font-size:12px;line-height:1.55}.truth-note p b{color:#9f1f4a}
.readiness-card{border-left-width:4px}.readiness-card.ready{border-left-color:#25815e;background:#f7fcf9}.readiness-card.blocked,.readiness-card.limited{border-left-color:#b26b16;background:#fffbf5}.readiness-card.checking{border-left-color:#6b7280}
.readiness-title{display:flex;align-items:center;gap:8px}.readiness-title strong{font-size:13px}.readiness-dot{width:8px;height:8px;border-radius:50%;background:#6b7280}.ready .readiness-dot{background:#25815e}.blocked .readiness-dot,.limited .readiness-dot{background:#b26b16}
.readiness-card p,.readiness-card ul{margin:7px 0 0;color:#647086;font-size:11px;line-height:1.5}.readiness-card ul{padding-left:17px}.readiness-card li+li{margin-top:2px}
.operation-status{display:flex;align-items:center;gap:14px;margin:10px 26px 0;padding:9px 12px;border:1px solid #dbe3ee;border-radius:9px;background:#f8fafc;color:#46536a;font-size:12px}.operation-status>span{flex:0 1 auto}.operation-progress{height:6px;min-width:160px;max-width:320px;flex:1;overflow:hidden;border-radius:999px;background:#e6eaf0}.operation-progress i{display:block;height:100%;border-radius:inherit;background:#d92f64;transition:width .2s ease}
.eval-btn,.eval-close,.report-actions button,.badcase-item button,.run-chip,.result-row,.trace-drawer header button{min-height:44px}
.eval-btn,.eval-close,.report-actions button,.badcase-item button,.run-chip,.result-row,.trace-drawer header button{touch-action:manipulation}
.eval-btn.replay{border-color:#c83b68;background:#fff6f9;color:#a6204b}.eval-btn:disabled{cursor:not-allowed}.eval-btn.primary:disabled{background:#8e96a5;border-color:#8e96a5;color:#fff}
.eval-close,.trace-drawer header button{min-width:44px;display:inline-grid;place-items:center;border-radius:8px}
.run-chip{display:grid;grid-template-columns:auto auto;grid-template-rows:auto auto;column-gap:8px;text-align:left}.run-chip>span{grid-column:1}.run-chip>small{grid-column:1;color:#7b8597;font-size:10px}.run-chip>b{grid-column:2;grid-row:1/3;align-self:center}
.run-facts{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));margin:0;padding:0;border-bottom:1px solid #edf0f4;background:#fbfcfe}.run-facts>div{min-width:0;padding:10px 14px;border-right:1px solid #e8ebf0}.run-facts>div:last-child{border-right:0}.run-facts dt{color:#7c8698;font-size:10px}.run-facts dd{margin:4px 0 0;color:#344057;font-size:11px;font-weight:700;overflow-wrap:anywhere}.run-facts .fact-real{color:#177a52}.run-facts .fact-warning{color:#946200}.run-facts .fact-synthetic{color:#a6204b}.run-facts .fact-unknown{color:#697386}
.trace-card{min-width:0;border:1px solid #e3e7ee;border-radius:10px;background:#fff;padding:11px 12px}.trace-card-head{display:flex;align-items:center;justify-content:space-between;gap:10px}.trace-card-head>b{color:#263149}.trace-card-head>span{color:#768196;font:600 10px/1.3 ui-monospace,SFMono-Regular,Consolas,monospace}.trace-card>p{margin:4px 0 0;color:#687489;font-size:11px;line-height:1.5}.trace-fields{display:grid;gap:7px;margin:9px 0 0}.trace-fields>div{display:grid;grid-template-columns:88px minmax(0,1fr);gap:9px;align-items:start;padding-top:7px;border-top:1px solid #edf0f4}.trace-fields dt{color:#7c8799;font-size:10px}.trace-fields dd{max-height:180px;overflow:auto;margin:0;color:#313d54;font-size:11px;line-height:1.55;overflow-wrap:anywhere;white-space:pre-wrap}.trace-card .trace-no-data{color:#97a0af;font-style:italic}
.report-actions button,.badcase-item button{padding-block:8px}.result-row:not(.result-head){min-height:46px}.eval-close:hover,.trace-drawer header button:hover{background:#f3f5f8}
.eval-kpi small,.panel-head p,.badcase-item small,.owner,.run-chip>small{color:#626d80}
.eval-btn:focus-visible,.eval-close:focus-visible,.run-chip:focus-visible,.result-row:focus-visible,.report-actions button:focus-visible,.badcase-item button:focus-visible,.trace-drawer header button:focus-visible{outline:3px solid rgba(217,47,100,.35);outline-offset:2px}
@media(prefers-reduced-motion:reduce){.operation-progress i{transition:none}}
@media(max-width:1100px){.eval-mode-summary{grid-template-columns:1.3fr 1fr}.run-facts{grid-template-columns:repeat(2,minmax(0,1fr))}.run-facts>div:nth-child(2){border-right:0}.run-facts>div:nth-child(-n+2){border-bottom:1px solid #e8ebf0}}
@media(max-width:900px){.eval-mode-summary{grid-template-columns:1fr;margin:12px 14px 0}.operation-status{margin-inline:14px}.eval-workbench{overflow:auto}.offline-workspace{overflow:visible;flex:none}.eval-body{flex:none}.eval-main,.eval-aside{overflow:visible}.result-table,.badcase-list{max-height:460px}.trace-drawer{position:fixed}.eval-head{position:sticky;top:0;z-index:2}.eval-actions{max-width:480px}}
@media(max-width:680px){.eval-actions{display:grid;grid-template-columns:1fr 1fr;width:100%}.eval-actions .primary{grid-column:1/3}.eval-close{position:absolute;right:10px;top:8px}.eval-head>div:first-child{padding-right:42px}.eval-btn{white-space:normal}.truth-note{grid-template-columns:1fr}.truth-note .mode-mark{justify-self:start}.operation-status{align-items:flex-start;flex-direction:column}.operation-progress{width:100%;max-width:none}.run-facts{grid-template-columns:1fr}.run-facts>div{border-right:0;border-bottom:1px solid #e8ebf0}.run-facts>div:last-child{border-bottom:0}}
.eval-btn.strategy{border-color:#9aa5b8;background:#f7f8fb;color:#344057}
.eval-btn.evolution{border-color:#c7889f;background:#fff7fa;color:#8e2146}
.strategy-drawer{position:absolute;z-index:5;top:0;right:0;bottom:0;width:min(900px,96vw);display:flex;flex-direction:column;background:#f7f8fb;border-left:1px solid #dfe3ea;box-shadow:-24px 0 64px rgba(28,39,66,.2)}
.strategy-head{position:sticky;top:0;z-index:2;display:flex;justify-content:space-between;align-items:center;gap:18px;padding:18px 22px;background:#fff;border-bottom:1px solid #e2e6ee}.strategy-head span{color:#d92f64;font:700 10px/1.2 ui-monospace,SFMono-Regular,Consolas,monospace;letter-spacing:.1em}.strategy-head h3{margin:4px 0 0;font-size:18px}.strategy-head p{margin:4px 0 0;color:#697386;font-size:11px}.strategy-head button{min-width:44px;min-height:44px;border:0;border-radius:8px;background:transparent;color:#697386;font-size:25px;cursor:pointer}.strategy-head button:hover{background:#f0f2f6}
.strategy-scroll{min-height:0;overflow:auto;padding:18px 22px 28px}.strategy-truth{border:1px solid #efc8d5;border-left:4px solid #d92f64;border-radius:10px;background:#fff8fa;padding:13px 15px}.strategy-truth strong{display:block;color:#7d1f40;font-size:13px}.strategy-truth p{margin:5px 0 0;color:#684b56;font-size:11px;line-height:1.6}.strategy-truth code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
.strategy-feedback{display:flex;gap:9px;margin-top:10px;border-radius:9px;padding:10px 12px;font-size:11px;line-height:1.5}.strategy-feedback.error{border:1px solid #f2b8c5;background:#fff0f3;color:#8c1832}.strategy-feedback.success{border:1px solid #b9dfcf;background:#f2fbf6;color:#176244}.strategy-loading{min-height:180px;display:flex;align-items:center;justify-content:center;gap:9px;color:#617087;font-size:12px}.strategy-loading span{width:18px;height:18px;border:2px solid #e6a6ba;border-top-color:#d92f64;border-radius:50%;animation:strategy-spin .8s linear infinite}
.strategy-section{margin-top:12px;border:1px solid #e2e6ee;border-radius:11px;background:#fff;padding:15px}.strategy-section-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px}.strategy-section-head h4{margin:0;color:#263149;font-size:14px}.strategy-section-head p{margin:3px 0 0;color:#7b8597;font-size:11px;line-height:1.5}.strategy-section-head>span{flex:none;border-radius:999px;background:#edf0f4;color:#5e697c;padding:5px 8px;font-size:10px;font-weight:700}.strategy-pointer-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.strategy-version-card{min-width:0;border:1px solid #e2e6ee;border-radius:9px;padding:12px}.strategy-version-card.current{border-left:4px solid #177a52;background:#f7fcf9}.strategy-version-card.previous{border-left:4px solid #738096;background:#fafbfc}.strategy-version-card span,.strategy-version-card strong,.strategy-version-card small{display:block}.strategy-version-card span{color:#687489;font-size:10px}.strategy-version-card strong{margin-top:4px;color:#263149;font-size:13px;overflow-wrap:anywhere}.strategy-version-card small{margin-top:5px;color:#7c8799;font:500 10px/1.4 ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere}
.rollback-box{display:grid;grid-template-columns:minmax(180px,1fr) minmax(220px,1.2fr) auto;align-items:end;gap:12px;margin-top:12px;border:1px solid #f1c1ca;border-radius:9px;background:#fff7f8;padding:12px}.rollback-box strong{color:#91243e;font-size:12px}.rollback-box p{margin:3px 0 0;color:#78505a;font-size:10px;line-height:1.45}.rollback-box label,.strategy-form label,.proposal-note,.activation-box label{display:grid;gap:5px;color:#455168;font-size:11px;font-weight:700}.rollback-box input,.strategy-form input,.strategy-form select,.strategy-form textarea,.proposal-note textarea,.activation-box input{width:100%;min-height:44px;border:1px solid #cfd5df;border-radius:8px;background:#fff;padding:9px 10px;color:#263149;font:inherit;font-size:12px;line-height:1.5;outline:none}.strategy-form textarea,.proposal-note textarea{resize:vertical;font-family:ui-monospace,SFMono-Regular,Consolas,monospace}.rollback-box input:focus,.strategy-form input:focus,.strategy-form select:focus,.strategy-form textarea:focus,.proposal-note textarea:focus,.activation-box input:focus{border-color:#d92f64;box-shadow:0 0 0 3px rgba(217,47,100,.12)}
.strategy-form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.strategy-form{display:grid;gap:10px}.strategy-form>small{color:#7b8597;font-size:10px;line-height:1.5}.strategy-primary,.strategy-secondary,.strategy-danger,.strategy-activate{min-height:44px;border-radius:8px;padding:9px 13px;font-weight:700;cursor:pointer;touch-action:manipulation}.strategy-primary{border:1px solid #d92f64;background:#d92f64;color:#fff}.strategy-secondary{border:1px solid #cbd2dd;background:#fff;color:#445168}.strategy-danger{border:1px solid #b72543;background:#fff;color:#a51f3c}.strategy-activate{border:1px solid #177a52;background:#177a52;color:#fff}.strategy-primary:disabled,.strategy-secondary:disabled,.strategy-danger:disabled,.strategy-activate:disabled{opacity:.48;cursor:not-allowed}.strategy-inline-error{margin:0;border-left:3px solid #c62846;background:#fff0f3;color:#8c1832;padding:8px 9px;font-size:10px}.strategy-empty-inline{margin:0;color:#7b8597;font-size:11px}
.strategy-empty{min-height:120px;display:grid;place-items:center;align-content:center;gap:5px;text-align:center;color:#7b8597}.strategy-empty strong{color:#3a465d;font-size:12px}.strategy-empty span{max-width:520px;font-size:11px;line-height:1.5}.proposal-grid{display:grid;grid-template-columns:220px minmax(0,1fr);gap:14px}.proposal-list{display:flex;flex-direction:column;gap:7px;max-height:520px;overflow:auto}.proposal-list button{min-height:54px;border:1px solid #dfe3ea;border-radius:8px;background:#fff;padding:9px 10px;text-align:left;color:#374158;cursor:pointer}.proposal-list button.active{border-color:#d92f64;background:#fff5f8}.proposal-list span,.proposal-list small{display:block}.proposal-list span{font-size:11px;font-weight:700}.proposal-list small{margin-top:4px;color:#7b8597;font-size:9px}.proposal-detail{min-width:0;border-left:1px solid #e8ebf0;padding-left:14px}.proposal-title{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.proposal-title span{color:#7b8597;font:600 9px/1.3 ui-monospace,SFMono-Regular,Consolas,monospace}.proposal-title h5{margin:3px 0 0;font-size:14px}.proposal-status{flex:none;border-radius:5px;padding:4px 7px;font-size:10px}.proposal-status.status-proposed{background:#fff0d5;color:#815600}.proposal-status.status-blocked,.proposal-status.status-rejected{background:#fde9ed;color:#a51f3c}.proposal-status.status-approved{background:#e7efff;color:#274b91}.proposal-status.status-activated{background:#e5f5ed;color:#176c4a}.proposal-evidence{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));margin:12px 0 0;border:1px solid #e3e7ee;border-radius:9px;overflow:hidden}.proposal-evidence>div{min-width:0;padding:9px;border-right:1px solid #edf0f4;border-bottom:1px solid #edf0f4}.proposal-evidence>div:nth-child(3n){border-right:0}.proposal-evidence>div:nth-last-child(-n+3){border-bottom:0}.proposal-evidence dt{color:#7b8597;font-size:9px}.proposal-evidence dd{margin:4px 0 0;color:#303c53;font-size:10px;font-weight:700;overflow-wrap:anywhere}.proposal-blockers{margin-top:10px;border-left:3px solid #b72543;background:#fff4f6;padding:9px 11px;color:#7e2038}.proposal-blockers strong{font-size:11px}.proposal-blockers ul{margin:5px 0 0;padding-left:17px;font-size:10px;line-height:1.55}.proposal-ready{margin:10px 0 0;border-left:3px solid #177a52;background:#f2fbf6;padding:9px 11px;color:#176244;font-size:10px}.proposal-note{margin-top:11px}.proposal-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:10px}.activation-box{margin-top:11px;border:1px solid #b9dfcf;border-radius:9px;background:#f5fcf8;padding:11px}.activation-box>strong{color:#176244;font-size:12px}.activation-box>p{margin:4px 0 9px;color:#4f6e61;font-size:10px}.activation-box .strategy-activate{margin-top:9px;width:100%}.proposal-audit{margin-top:10px;border-top:1px solid #e6eaf0;padding-top:9px}.proposal-audit strong{font-size:10px}.proposal-audit p{margin:4px 0 0;color:#687489;font-size:10px;line-height:1.5}
.strategy-head button:focus-visible,.strategy-form input:focus-visible,.strategy-form select:focus-visible,.strategy-form textarea:focus-visible,.proposal-note textarea:focus-visible,.activation-box input:focus-visible,.rollback-box input:focus-visible,.strategy-primary:focus-visible,.strategy-secondary:focus-visible,.strategy-danger:focus-visible,.strategy-activate:focus-visible,.proposal-list button:focus-visible{outline:3px solid rgba(217,47,100,.35);outline-offset:2px}
@keyframes strategy-spin{to{transform:rotate(360deg)}}
@media(prefers-reduced-motion:reduce){.strategy-loading span{animation:none}}
@media(max-width:760px){.strategy-drawer{position:fixed;width:100vw}.strategy-scroll{padding:13px}.strategy-form-grid,.strategy-pointer-grid,.proposal-grid{grid-template-columns:1fr}.rollback-box{grid-template-columns:1fr}.proposal-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));max-height:none}.proposal-detail{border-left:0;border-top:1px solid #e8ebf0;padding:13px 0 0}.proposal-evidence{grid-template-columns:repeat(2,minmax(0,1fr))}.proposal-evidence>div,.proposal-evidence>div:nth-child(3n){border-right:1px solid #edf0f4;border-bottom:1px solid #edf0f4}.proposal-evidence>div:nth-child(2n){border-right:0}.proposal-evidence>div:nth-last-child(-n+2){border-bottom:0}.strategy-form input,.strategy-form select,.strategy-form textarea,.proposal-note textarea,.activation-box input,.rollback-box input{font-size:16px}}
@media(max-width:460px){.proposal-list{grid-template-columns:1fr}.proposal-evidence{grid-template-columns:1fr}.proposal-evidence>div,.proposal-evidence>div:nth-child(2n),.proposal-evidence>div:nth-last-child(-n+2){border-right:0;border-bottom:1px solid #edf0f4}.proposal-evidence>div:last-child{border-bottom:0}.proposal-actions{display:grid;grid-template-columns:1fr 1fr}.strategy-head{padding:14px}.strategy-head p{display:none}}
</style>
