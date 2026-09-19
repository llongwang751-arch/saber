<template>
  <main class="online-panel" aria-labelledby="online-experiment-title" :aria-busy="busy">
    <section class="truth-banner" :class="`truth-${truthState.tone}`" role="status" aria-live="polite" aria-atomic="true">
      <span class="truth-signal" aria-hidden="true"></span>
      <div>
        <p class="eyebrow">线上实验真实性</p>
        <h3 id="online-experiment-title">{{ truthState.title }}</h3>
        <p>{{ truthState.detail }}</p>
      </div>
      <strong v-if="!experiments.canAnalyze">尚不能宣称效果</strong>
      <strong v-else class="claim-ready">证据门禁通过，可下结论</strong>
    </section>

    <div v-if="experiments.error" class="online-feedback error" role="alert">
      <strong>操作失败</strong>
      <span>{{ friendlyOnlineError(experiments.error) }}。请核对权限、版本代次和后端就绪状态后重试。</span>
    </div>
    <div v-if="experiments.message" class="online-feedback success" role="status" aria-live="polite">
      {{ experiments.message }}
    </div>

    <div v-if="experiments.loading && !experiments.experiments.length" class="online-loading" aria-live="polite">
      <span aria-hidden="true"></span>正在读取真实在线实验控制面…
    </div>

    <template v-else>
      <section class="control-strip" aria-label="在线实验选择与就绪状态">
        <div class="readiness-block">
          <span>生产链路</span>
          <strong>{{ readinessTitle }}</strong>
          <small>{{ readinessDetail }}</small>
          <div class="permission-line" aria-label="当前在线实验权限">
            <b :class="{ allowed: canManage }">{{ canManage ? '可管理' : '无管理权限' }}</b>
            <b :class="{ allowed: canApprove }">{{ canApprove ? '可审批' : '无审批权限' }}</b>
          </div>
        </div>
        <label for="online-experiment-select">当前实验
          <select id="online-experiment-select" :value="experiments.selectedId" :disabled="busy || !experiments.experiments.length"
                  @change="selectExperiment">
            <option v-if="!experiments.experiments.length" value="">暂无实验</option>
            <option v-for="item in experiments.experiments" :key="item.id" :value="item.id">
              {{ item.name || shortId(item.id) }} · {{ statusText(item.status) }}
            </option>
          </select>
        </label>
        <button class="online-button secondary" type="button" :disabled="busy" @click="experiments.refresh()">
          {{ experiments.loading ? '正在刷新…' : '刷新控制面' }}
        </button>
      </section>

      <div class="online-layout">
        <div class="online-main">
          <section v-if="selected" class="online-card evidence-card" aria-labelledby="evidence-title">
            <header class="card-head">
              <div>
                <p class="eyebrow">预注册证据</p>
                <h3 id="evidence-title">{{ selected.name || shortId(selected.id) }}</h3>
                <p>{{ selected.hypothesis || '未填写实验假设。' }}</p>
              </div>
              <span class="state-pill" :class="`state-${selected.status}`">{{ statusText(selected.status) }}</span>
            </header>

            <dl class="facts-grid">
              <div><dt>预注册校验和</dt><dd class="mono" :title="selected.preregistration_checksum">{{ checksumText(selected.preregistration_checksum) }}</dd></div>
              <div><dt>每组所需样本 N</dt><dd>{{ knownNumber(selected.required_sample_per_arm) }}</dd></div>
              <div><dt>真实曝光</dt><dd>{{ knownNumber(analysis?.real_exposure_count) }}</dd></div>
              <div><dt>结果反馈（Outcome）覆盖</dt><dd>{{ outcomeCoverage }}</dd></div>
              <div><dt>流量来源</dt><dd>{{ provenanceText(experiments.provenance) }}</dd></div>
              <div><dt>受众资格</dt><dd>{{ selected.audience_policy_version ? '后台预置业务账号' : '未冻结' }}</dd></div>
              <div><dt>灰度（Canary）流量</dt><dd>{{ basisPoints(selected.enrollment_bps) }}</dd></div>
              <div><dt>候选组占比</dt><dd>{{ basisPoints(selected.candidate_allocation_bps) }}</dd></div>
              <div><dt>配置代次</dt><dd>{{ knownNumber(selected.generation) }}</dd></div>
            </dl>

            <div class="evidence-grid">
              <article>
                <div class="mini-head"><h4>真实曝光与结果反馈（Outcome）</h4><span>{{ sampleStatus }}</span></div>
                <dl class="paired-metrics">
                  <div><dt>对照组唯一用户</dt><dd>{{ knownNumber(analysis?.sample?.control_unique_users) }}</dd></div>
                  <div><dt>候选组唯一用户</dt><dd>{{ knownNumber(analysis?.sample?.candidate_unique_users) }}</dd></div>
                  <div><dt>对照组结果反馈</dt><dd>{{ knownNumber(analysis?.sample?.control_outcomes) }}</dd></div>
                  <div><dt>候选组结果反馈</dt><dd>{{ knownNumber(analysis?.sample?.candidate_outcomes) }}</dd></div>
                </dl>
                <p class="metric-note">只有后台预置、非管理角色且早于实验提交建立的业务账号才计入真实曝光；同时还必须满足样本、时长与数据门禁，才能形成效果结论。</p>
              </article>

              <article>
                <div class="mini-head"><h4>分流比例异常检查（SRM）</h4><span :class="srmTone">{{ srmStatus }}</span></div>
                <dl class="paired-metrics">
                  <div><dt>观测总量</dt><dd>{{ knownNumber(analysis?.srm?.total) }}</dd></div>
                  <div><dt>预期候选占比</dt><dd>{{ ratioPercent(analysis?.srm?.expected_candidate_share) }}</dd></div>
                  <div v-if="canShowSrmP"><dt>分流检查显著性概率（p 值）</dt><dd>{{ decimal(analysis.srm.p_value, 5) }}</dd></div>
                  <div><dt>门禁</dt><dd>{{ analysis?.srm?.mismatch === true ? '阻断' : analysis?.srm?.passes === true ? '通过' : '待检查' }}</dd></div>
                </dl>
                <p class="metric-note">样本不足时不展示显著性概率（p 值），也不会用默认值伪装为已通过。</p>
              </article>

              <article class="guardrail-card">
                <div class="mini-head"><h4>安全与数据门禁</h4><span>{{ guardrailTitle }}</span></div>
                <ul v-if="blockers.length">
                  <li v-for="blocker in blockers" :key="blocker">{{ blockerText(blocker) }}</li>
                </ul>
                <p v-else>{{ experiments.canClaimEffect ? '未发现阻断项，仍需人工复核业务风险。' : '后端尚未提供可核验的门禁结论。' }}</p>
              </article>
            </div>

            <section class="conclusion" :class="{ ready: experiments.canAnalyze }" aria-labelledby="conclusion-title">
              <div>
                <p class="eyebrow">确认性统计结论</p>
                <h4 id="conclusion-title">{{ experiments.canAnalyze ? '可下结论' : '尚不能宣称效果' }}</h4>
              </div>
              <dl v-if="experiments.canAnalyze" class="conclusion-values">
                <div><dt>统计结论</dt><dd>{{ winnerText(analysis.winner) }}</dd></div>
                <div><dt>绝对效果</dt><dd>{{ signedPercent(analysis.absolute_effect) }}</dd></div>
                <div><dt>相对提升</dt><dd>{{ signedPercent(analysis.relative_lift) }}</dd></div>
                <div><dt>95% 置信区间</dt><dd>{{ intervalText(analysis.confidence_interval) }}</dd></div>
                <div><dt>显著性概率（p 值）</dt><dd>{{ decimal(analysis.p_value, 5) }}</dd></div>
              </dl>
              <p v-else>当前证据不满足真实流量、样本量、实验时长、分流比例异常检查（SRM）、安全门禁及完成状态的全部要求，因此不显示相对提升、显著性概率或赢家。</p>
              <p v-if="experiments.canAnalyze && !experiments.canClaimEffect" class="no-lift-claim">未达到统计显著：无赢家，不得宣称线上提升。</p>
            </section>
          </section>

          <section v-else class="online-card online-empty">
            <strong>还没有在线实验</strong>
            <span>先从已批准的离线候选创建不可变部署，再完成预注册与人工审批。没有真实生产曝光时只能验证链路，不能宣称效果。</span>
          </section>

          <section v-if="selected" class="online-card action-card" aria-labelledby="actions-title">
            <header class="card-head compact">
              <div><h3 id="actions-title">受控生命周期</h3><p>每次操作都携带预期代次（expected_generation）与幂等键（idempotency_key），并写入审计。</p></div>
            </header>
            <label for="online-action-reason">操作原因
              <textarea id="online-action-reason" v-model.trim="actionReason" rows="2" maxlength="2000"
                        placeholder="说明业务依据、观察结果或暂停原因" :disabled="busy"></textarea>
            </label>
            <label v-if="canRamp" for="online-ramp">目标总流量
              <select id="online-ramp" v-model.number="rampTarget" :disabled="busy">
                <option v-for="bps in rampOptions" :key="bps" :value="bps">{{ basisPoints(bps) }}</option>
              </select>
            </label>
            <div class="action-buttons" role="group" aria-label="在线实验生命周期操作">
              <button v-if="selected.status === 'draft'" class="online-button primary" type="button" :disabled="busy || !canManage" @click="runAction('submit')">提交审批</button>
              <button v-if="selected.status === 'pending_review'" class="online-button approve" type="button" :disabled="busy || !canApprove" @click="runAction('approve')">人工批准</button>
              <button v-if="selected.status === 'pending_review'" class="online-button danger-outline" type="button" :disabled="busy || !canApprove" @click="runAction('reject')">拒绝实验</button>
              <button v-if="selected.status === 'approved'" class="online-button primary" type="button" :disabled="busy || !canManage" @click="runAction('start')">启动灰度（Canary）</button>
              <button v-if="canRamp" class="online-button primary" type="button" :disabled="busy || !canManage || rampTarget <= Number(selected.enrollment_bps)" @click="runAction('ramp')">显式扩量</button>
              <button v-if="['canary','running'].includes(selected.status)" class="online-button warning" type="button" :disabled="busy || !canManage || !actionReason" @click="runAction('pause')">安全暂停</button>
              <button v-if="['paused','safety_paused'].includes(selected.status)" class="online-button warning" type="button" :disabled="busy || !(selected.status === 'safety_paused' ? canApprove : canManage) || !actionReason" @click="runAction('resume')">人工恢复</button>
              <button v-if="['canary','running','paused'].includes(selected.status)" class="online-button secondary" type="button" :disabled="busy || !canManage || !actionReason" @click="runAction('complete')">停止收集并完成</button>
            </div>
            <p v-if="selected.status === 'pending_review'" class="action-help">创建人不能审批自己的实验；权限不足时后端会阻断并返回原因。</p>

            <div v-if="canRollback" class="rollback-zone">
              <div><strong>回滚候选配置</strong><p>危险操作：立即停止该实验继续分配新流量。请输入实验名称并再次确认。</p></div>
              <label for="online-rollback-confirm">输入“{{ selected.name }}”
                <input id="online-rollback-confirm" v-model="rollbackConfirm" type="text" autocomplete="off" :disabled="busy" />
              </label>
              <button class="online-button danger" type="button"
                      :disabled="busy || !canManage || !actionReason || rollbackConfirm !== selected.name" @click="runAction('rollback')">确认回滚</button>
            </div>
          </section>
        </div>

        <aside class="online-side">
          <details class="online-card create-card">
            <summary>创建候选部署</summary>
            <p>只接受已经人工批准的离线候选（proposal）；编译后仍不会自动接入线上流量。</p>
            <form @submit.prevent="createDeployment">
              <label for="online-proposal-id">离线候选编号
                <input id="online-proposal-id" v-model.trim="proposalId" required maxlength="200" placeholder="输入已批准候选编号" :disabled="busy" />
              </label>
              <button class="online-button secondary" type="submit" :disabled="busy || !canManage || !proposalId">创建不可变部署</button>
            </form>
          </details>

          <details class="online-card create-card">
            <summary>创建预注册实验</summary>
            <p>实验创建后关键统计参数锁定；审批通过也不会自动启动。</p>
            <form @submit.prevent="createExperiment">
              <label for="experiment-name">实验名称
                <input id="experiment-name" v-model.trim="form.name" required maxlength="200" placeholder="例如：知识检索（RAG）召回策略灰度" :disabled="busy" />
              </label>
              <label for="experiment-hypothesis">实验假设
                <textarea id="experiment-hypothesis" v-model.trim="form.hypothesis" rows="2" maxlength="2000" placeholder="候选策略为何可能改善正向反馈" :disabled="busy"></textarea>
              </label>
              <label for="experiment-deployment">候选部署
                <select id="experiment-deployment" v-model="form.candidate_deployment_id" required :disabled="busy || !experiments.deployments.length">
                  <option value="" disabled>{{ experiments.deployments.length ? '请选择部署' : '暂无可用部署' }}</option>
                  <option v-for="deployment in experiments.deployments" :key="deployment.id" :value="deployment.id">
                    {{ shortId(deployment.id) }} · {{ checksumText(deployment.compiled_checksum) }}
                  </option>
                </select>
              </label>
              <div class="form-grid">
                <label for="baseline-rate">基线正反馈率
                  <input id="baseline-rate" v-model.number="form.baseline_rate" type="number" min="0.001" max="0.999" step="0.001" required :disabled="busy" />
                </label>
                <label for="minimum-effect">最小可检测效果
                  <input id="minimum-effect" v-model.number="form.minimum_detectable_effect" type="number" min="0.001" max="0.999" step="0.001" required :disabled="busy" />
                </label>
                <label for="experiment-alpha">显著性水平（Alpha）
                  <input id="experiment-alpha" v-model.number="form.alpha" type="number" min="0.001" max="0.999" step="0.001" required :disabled="busy" />
                </label>
                <label for="experiment-power">统计功效
                  <input id="experiment-power" v-model.number="form.power" type="number" min="0.501" max="0.999" step="0.001" required :disabled="busy" />
                </label>
                <label for="initial-enrollment">初始流量（万分比，bps）
                  <input id="initial-enrollment" v-model.number="form.enrollment_bps" type="number" min="1" max="10000" step="1" required :disabled="busy" />
                </label>
                <label for="candidate-allocation">候选分配（万分比，bps）
                  <input id="candidate-allocation" v-model.number="form.candidate_allocation_bps" type="number" min="1" max="9999" step="1" required :disabled="busy" />
                </label>
                <label for="minimum-duration">最短时长（小时）
                  <input id="minimum-duration" v-model.number="form.min_duration_hours" type="number" min="1" step="1" required :disabled="busy" />
                </label>
                <label for="maximum-duration">最长时长（小时）
                  <input id="maximum-duration" v-model.number="form.max_duration_hours" type="number" min="1" step="1" required :disabled="busy" />
                </label>
              </div>
              <button class="online-button primary" type="submit" :disabled="busy || !canManage || !canCreateExperiment">创建预注册实验</button>
            </form>
          </details>

          <section class="online-card audit-card" aria-labelledby="audit-title">
            <div class="mini-head"><h3 id="audit-title">审计记录</h3><span>{{ experiments.auditEvents.length }} 条</span></div>
            <ol v-if="experiments.auditEvents.length">
              <li v-for="event in experiments.auditEvents" :key="event.id">
                <span class="audit-action">{{ auditActionText(event.action) }}</span>
                <strong>{{ event.actor || '系统身份' }}</strong>
                <small>{{ event.from_status ? `${statusText(event.from_status)} → ` : '' }}{{ statusText(event.to_status) }} · 第 {{ knownNumber(event.generation) }} 代</small>
                <p v-if="event.details?.reason">{{ event.details.reason }}</p>
                <time v-if="event.created_at">{{ dateTime(event.created_at) }}</time>
              </li>
            </ol>
            <div v-else class="side-empty">尚无可显示的审计事件。</div>
          </section>
        </aside>
      </div>
    </template>
  </main>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useExperiments } from '../stores/experiments'

const experiments = useExperiments()
const proposalId = ref('')
const actionReason = ref('')
const rollbackConfirm = ref('')
const rampTarget = ref(2500)
const form = reactive({
  name: '',
  hypothesis: '',
  candidate_deployment_id: '',
  baseline_rate: 0.30,
  minimum_detectable_effect: 0.05,
  alpha: 0.05,
  power: 0.80,
  enrollment_bps: 1000,
  candidate_allocation_bps: 5000,
  min_duration_hours: 168,
  max_duration_hours: 672,
  attribution_window_hours: 168,
})

const selected = computed(() => experiments.selectedExperiment)
const analysis = computed(() => experiments.analysis)
const busy = computed(() => experiments.loading || experiments.detailLoading || experiments.actionLoading)
const blockers = computed(() => Array.isArray(analysis.value?.claim_blockers) ? analysis.value.claim_blockers : [])
const canManage = computed(() => experiments.readiness?.rbac?.can_manage === true)
const canApprove = computed(() => experiments.readiness?.rbac?.can_approve === true)
const canRamp = computed(() => ['canary', 'running'].includes(selected.value?.status) && Number(selected.value?.enrollment_bps) < 10000)
const canRollback = computed(() => ['canary', 'running', 'paused', 'safety_paused'].includes(selected.value?.status))
const rampOptions = computed(() => [1000, 2500, 5000, 7500, 10000].filter(value => value > Number(selected.value?.enrollment_bps || 0)))
const canCreateExperiment = computed(() => form.name && form.candidate_deployment_id
  && Number(form.baseline_rate) > 0
  && Number(form.minimum_detectable_effect) > 0
  && Number(form.baseline_rate) + Number(form.minimum_detectable_effect) < 1
  && Number(form.max_duration_hours) >= Number(form.min_duration_hours))

const truthState = computed(() => {
  const status = selected.value?.status
  if (status === 'rolled_back') return { title: '已回滚', detail: '候选配置已退出流量分配；审计与历史证据保留。', tone: 'danger' }
  if (status === 'safety_paused') return { title: '安全暂停', detail: '可信安全门禁已触发，恢复必须由具备权限的人工明确确认。', tone: 'danger' }
  if (status === 'paused') return { title: '安全暂停', detail: '实验已由人工暂停，不再分配新增流量；现有证据仍需经过全部门禁。', tone: 'warning' }
  if (experiments.canAnalyze) return {
    title: '可下结论',
    detail: experiments.canClaimEffect
      ? '真实生产曝光、预注册样本与时长、分流比例异常检查（SRM）和安全门禁均满足；观察到统计显著效果，仍需人工决策。'
      : '真实生产证据门禁均满足，但没有统计显著赢家；不得宣称线上提升。',
    tone: experiments.canClaimEffect ? 'success' : 'warning',
  }
  if (experiments.hasProductionTraffic && ['canary', 'running'].includes(status)) {
    return { title: '正在收集真实曝光', detail: '已经观察到认证生产流量，但证据尚未通过全部预注册与安全门禁。', tone: 'active' }
  }
  if (status === 'completed' && experiments.hasProductionTraffic) return { title: '尚不能宣称效果', detail: '实验已经停止收集，但真实证据未通过全部预注册或安全门禁。', tone: 'neutral' }
  if (experiments.provenance === 'internal') return { title: '内部演练', detail: '内部流量不可证明线上提升；当前数据仅用于验证分桶、曝光、结果反馈（Outcome）与审计链路。', tone: 'warning' }
  return { title: '未接生产流量', detail: '生产流量未配置或已禁用；任何通过率或内部数据都不能当作线上效果。', tone: 'neutral' }
})

const readinessTitle = computed(() => {
  const ready = experiments.readiness?.ready ?? experiments.readiness?.control_plane?.ready
  if (ready === true) return '控制面已就绪'
  if (ready === false) return '控制面受限'
  return '后端未声明'
})
const readinessDetail = computed(() => {
  const reasons = experiments.readiness?.blockers || experiments.readiness?.reasons || experiments.readiness?.blocked_reasons || experiments.readiness?.control_plane?.reasons
  if (Array.isArray(reasons) && reasons.length) return blockerText(reasons[0])
  return provenanceText(experiments.provenance)
})
const outcomeCoverage = computed(() => {
  const sample = analysis.value?.sample
  const exposures = Number(analysis.value?.real_exposure_count)
  const outcomes = Number(sample?.control_outcomes) + Number(sample?.candidate_outcomes)
  if (!Number.isFinite(exposures) || !Number.isFinite(outcomes) || exposures <= 0) return '—'
  return `${outcomes.toLocaleString('zh-CN')} / ${exposures.toLocaleString('zh-CN')}（${(outcomes / exposures * 100).toFixed(1)}%）`
})
const sampleStatus = computed(() => analysis.value?.sample?.sufficient === true ? '样本已足量' : '样本未足量')
const canShowSrmP = computed(() => experiments.canAnalyze
  && analysis.value?.srm?.sufficient_sample === true
  && analysis.value?.srm?.p_value != null)
const srmStatus = computed(() => {
  const status = analysis.value?.srm?.status
  if (status === 'mismatch') return '发现分流比例异常，已阻断'
  if (status === 'pass') return '比例一致'
  if (status === 'insufficient') return '样本不足'
  return '尚未检查'
})
const srmTone = computed(() => analysis.value?.srm?.status === 'mismatch' ? 'metric-danger' : analysis.value?.srm?.status === 'pass' ? 'metric-ok' : '')
const guardrailTitle = computed(() => selected.value?.status === 'safety_paused'
  ? '可信安全事件已暂停'
  : blockers.value.length ? `${blockers.value.length} 项阻断` : experiments.canClaimEffect ? '全部通过' : '等待证据')

watch(() => selected.value?.id, () => {
  actionReason.value = ''
  rollbackConfirm.value = ''
  rampTarget.value = rampOptions.value[0] || 10000
})
watch(() => experiments.deployments, value => {
  if (!form.candidate_deployment_id && value.length) form.candidate_deployment_id = value[0].id
}, { deep: true })

async function selectExperiment(event) {
  await experiments.selectExperiment(event.target.value).catch(() => {})
}
async function createDeployment() {
  await experiments.createDeployment(proposalId.value).catch(() => {})
  proposalId.value = ''
}
async function createExperiment() {
  if (!canCreateExperiment.value) return
  await experiments.createExperiment(form).catch(() => {})
  form.name = ''
  form.hypothesis = ''
}
async function runAction(action) {
  const labels = {
    submit: '提交审批', approve: '批准实验', reject: '拒绝实验', start: '启动灰度（Canary）', ramp: '提升真实流量',
    pause: '安全暂停', resume: '恢复实验', complete: '停止收集并完成实验', rollback: '回滚候选配置',
  }
  if (!window.confirm(`确认${labels[action]}？\n\n当前代次：${selected.value?.generation ?? '未知'}。操作会写入不可变审计记录。`)) return
  try {
    if (action === 'approve' || action === 'reject') await experiments.decide(action === 'approve' ? 'approve' : 'reject', actionReason.value)
    else if (action === 'ramp') await experiments.ramp(rampTarget.value, actionReason.value)
    else await experiments[action](actionReason.value)
    actionReason.value = ''
    rollbackConfirm.value = ''
  } catch { /* store 已提供可恢复错误 */ }
}

function shortId(value) { return String(value || '').slice(0, 10) || '未知' }
function checksumText(value) { const text = String(value || ''); return text ? `${text.slice(0, 12)}…` : '尚未生成' }
function knownNumber(value) { const n = Number(value); return value !== '' && value != null && Number.isFinite(n) ? n.toLocaleString('zh-CN') : '—' }
function basisPoints(value) { const n = Number(value); return Number.isFinite(n) ? `${(n / 100).toFixed(n % 100 ? 2 : 0)}%` : '—' }
function ratioPercent(value) { const n = Number(value); return value != null && Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : '—' }
function decimal(value, digits = 3) { const n = Number(value); return value != null && Number.isFinite(n) ? n.toFixed(digits) : '—' }
function signedPercent(value) { const n = Number(value); return value != null && Number.isFinite(n) ? `${n > 0 ? '+' : ''}${(n * 100).toFixed(2)}%` : '—' }
function intervalText(value) { return Array.isArray(value) && value.length === 2 ? `[${signedPercent(value[0])}, ${signedPercent(value[1])}]` : '—' }
function winnerText(value) { return value === 'candidate' ? '候选组显著胜出' : value === 'control' ? '对照组显著胜出' : '无显著赢家' }
function reasonText(value) { return typeof value === 'object' ? String(value.message || value.reason || value.detail || value.code || '未说明原因') : String(value || '') }
function provenanceText(value) { return ({ disabled: '未接生产流量', internal: '内部演练', production_authenticated: '认证生产流量' })[value] || '来源未声明' }
function statusText(value) {
  return ({ draft: '草稿', pending_review: '等待审批', approved: '已批准', rejected: '已拒绝', canary: '灰度（Canary）运行中', running: '全量运行中', paused: '已暂停', safety_paused: '安全暂停', completed: '已完成', rolled_back: '已回滚' })[value] || value || '未知状态'
}
function blockerText(value) {
  const key = typeof value === 'object' ? value.code || value.reason || value.message : value
  return ({
    traffic_provenance_disabled: '未接生产流量', traffic_provenance_internal: '仅内部演练流量', no_real_traffic: '没有真实生产曝光', experiment_not_completed: '实验尚未完成',
    minimum_duration_not_reached: '未达到预注册最短时长', insufficient_sample_size: '每组结果反馈未达到预注册样本量', srm_not_checked: '分流比例异常检查（SRM）尚未执行',
    srm_insufficient_sample: '分流比例异常检查（SRM）样本不足', sample_ratio_mismatch: '发现样本比例失衡（SRM）', trusted_s0_s1_safety_event: '可信 S0/S1 安全事件触发门禁',
    no_statistically_significant_effect: '未观察到统计显著效果', stale_started_exposure: '存在长期未结束的曝光记录',
    outcome_attribution_window_open: '结果归因窗口尚未结束', hmac_secret_missing: '实验分流密钥尚未配置',
    production_evidence_store_not_ready: '生产证据库尚未使用共享的非 SQLite 存储', baseline_runtime_config_incomplete: '对照组运行配置不完整',
    runtime_component_manifest_unverified: '运行环境组件清单尚未通过可信校验', runtime_identity_drift: '运行环境身份与预注册时不一致',
    audience_evidence_integrity_failed: '实验受众资格证据校验失败', audit_chain_integrity_failed: '实验审计链完整性校验失败',
    runtime_environment_integrity_failed: '运行环境完整性校验失败', assignment_key_integrity_failed: '分流密钥完整性校验失败',
    traffic_provenance_integrity_failed: '流量来源完整性校验失败', deployment_integrity_failed: '候选部署完整性校验失败',
    preregistration_integrity_failed: '预注册证据完整性校验失败', exposure_integrity_failed: '曝光证据完整性校验失败',
    experiment_integrity_failed: '实验完整性校验失败',
  })[key] || reasonText(value)
}
function friendlyOnlineError(value) {
  const text = String(value || '')
  if (text === 'unauthorized') return '登录状态已失效，请重新登录'
  if (text.includes('experiment_admin')) return '当前账号没有实验管理权限'
  if (text.includes('experiment_approver')) return '当前账号没有实验审批权限'
  if (text.includes('generation changed') || text.includes('generation conflict')) return '实验已被其他操作更新，请刷新后重试'
  if (text.includes('creator cannot approve')) return '创建者不能审批自己的实验，请由另一位审批人操作'
  if (text.includes('runtime component') || text.includes('runtime identity')) return '运行环境组件证据校验失败，生产实验已安全关闭'
  if (text.includes('audit chain')) return '实验审计链校验失败，系统已阻止继续操作'
  if (/^[a-z0-9_.:-]+$/i.test(text)) return `操作未完成（错误代码：${text}）`
  return text || '在线实验操作失败'
}
function auditActionText(value) { return ({ create: '创建', submit: '提交审批', approve: '批准', reject: '拒绝', start_canary: '启动灰度（Canary）', start_running: '启动全量', ramp: '扩量', pause: '暂停', resume: '恢复', complete: '完成', rollback: '回滚' })[value] || value || '状态变更' }
function dateTime(value) { const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', { hour12: false }) }

onMounted(() => experiments.refresh())
</script>

<style scoped>
.online-panel{--online-blue:#1e40af;--online-blue-soft:#eef4ff;--online-ink:#172033;--online-muted:#647086;--online-line:#dfe5ee;min-height:0;flex:1;overflow:auto;padding:16px 26px 28px;background:#f5f7fa;color:var(--online-ink)}
.truth-banner{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:13px;align-items:center;border:1px solid var(--online-line);border-left:5px solid #64748b;border-radius:12px;background:#fff;padding:14px 16px}.truth-signal{width:11px;height:11px;border:3px solid #64748b;border-radius:50%}.truth-banner h3{margin:1px 0 2px;font-size:17px}.truth-banner p{margin:0;color:var(--online-muted);font-size:11px;line-height:1.55}.truth-banner>strong{max-width:190px;border-radius:7px;background:#f1f3f6;color:#4b5565;padding:8px 10px;text-align:center;font-size:11px}.truth-banner .claim-ready{background:#e7f7ef;color:#176244}.truth-success{border-left-color:#18794e}.truth-success .truth-signal{border-color:#18794e}.truth-active{border-left-color:#1e40af}.truth-active .truth-signal{border-color:#1e40af}.truth-warning{border-left-color:#a05b0e}.truth-warning .truth-signal{border-color:#a05b0e}.truth-danger{border-left-color:#b4233f}.truth-danger .truth-signal{border-color:#b4233f}
.eyebrow{color:var(--online-blue)!important;font:750 9px/1.2 ui-monospace,SFMono-Regular,Consolas,monospace!important;letter-spacing:.11em}.online-feedback{display:flex;gap:8px;margin-top:10px;border-radius:9px;padding:10px 12px;font-size:11px;line-height:1.5}.online-feedback.error{border:1px solid #efbec8;background:#fff2f4;color:#861d34}.online-feedback.success{border:1px solid #b6ddcb;background:#f2fbf6;color:#176244}.online-loading{min-height:260px;display:flex;align-items:center;justify-content:center;gap:10px;color:var(--online-muted);font-size:12px}.online-loading span{width:20px;height:20px;border:2px solid #b9c9e8;border-top-color:var(--online-blue);border-radius:50%;animation:online-spin .8s linear infinite}
.control-strip{display:grid;grid-template-columns:minmax(200px,1fr) minmax(260px,1.35fr) auto;gap:12px;align-items:end;margin-top:12px;border:1px solid var(--online-line);border-radius:11px;background:#fff;padding:12px 14px}.readiness-block{display:grid;gap:2px}.readiness-block span{color:var(--online-muted);font-size:10px}.readiness-block strong{font-size:13px}.readiness-block small{color:#69758a;font-size:10px}.permission-line{display:flex;flex-wrap:wrap;gap:5px;margin-top:4px}.permission-line b{border-radius:4px;background:#fff0f3;color:#9c2743;padding:3px 6px;font-size:9px}.permission-line b.allowed{background:#e7f6ee;color:#176244}.control-strip label,.action-card label,.create-card label{display:grid;gap:5px;color:#46536a;font-size:10px;font-weight:700}.control-strip select,.action-card select,.action-card textarea,.rollback-zone input,.create-card input,.create-card select,.create-card textarea{width:100%;min-height:44px;border:1px solid #cbd3df;border-radius:8px;background:#fff;padding:9px 10px;color:#263149;font:inherit;font-size:12px;outline:none}.control-strip select:focus,.action-card select:focus,.action-card textarea:focus,.rollback-zone input:focus,.create-card input:focus,.create-card select:focus,.create-card textarea:focus{border-color:var(--online-blue);box-shadow:0 0 0 3px rgba(30,64,175,.14)}
.online-layout{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:14px;margin-top:14px}.online-main,.online-side{min-width:0;display:flex;flex-direction:column;gap:12px}.online-card{border:1px solid var(--online-line);border-radius:11px;background:#fff;padding:15px}.card-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding-bottom:13px;border-bottom:1px solid #edf0f4}.card-head.compact{padding-bottom:10px}.card-head h3{margin:2px 0 0;font-size:16px}.card-head p{margin:4px 0 0;color:var(--online-muted);font-size:11px;line-height:1.5}.state-pill{flex:none;border-radius:999px;background:#eef1f5;color:#4f5b6f;padding:6px 9px;font-size:10px;font-weight:750}.state-canary,.state-running{background:#e8efff;color:#1e40af}.state-safety_paused,.state-rolled_back,.state-rejected{background:#fde9ed;color:#a51f3c}.state-completed{background:#e6f5ed;color:#176244}.state-paused,.state-pending_review{background:#fff1d8;color:#815600}
.facts-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));margin:13px 0 0;border:1px solid #e6eaf0;border-radius:9px;overflow:hidden}.facts-grid>div{min-width:0;padding:10px;border-right:1px solid #edf0f4;border-bottom:1px solid #edf0f4}.facts-grid>div:nth-child(4n){border-right:0}.facts-grid>div:nth-last-child(-n+4){border-bottom:0}.facts-grid dt,.paired-metrics dt,.conclusion-values dt{color:#748095;font-size:9px}.facts-grid dd,.paired-metrics dd,.conclusion-values dd{margin:4px 0 0;color:#263149;font-size:12px;font-weight:750;overflow-wrap:anywhere}.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}.evidence-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}.evidence-grid article{min-width:0;border:1px solid #e4e8ef;border-radius:9px;background:#fbfcfe;padding:11px}.evidence-grid .guardrail-card{grid-column:1/3}.mini-head{display:flex;align-items:center;justify-content:space-between;gap:10px}.mini-head h3,.mini-head h4{margin:0;font-size:12px}.mini-head span{color:#68758a;font-size:10px;font-weight:700}.mini-head .metric-ok{color:#176244}.mini-head .metric-danger{color:#a51f3c}.paired-metrics{display:grid;grid-template-columns:1fr 1fr;margin-top:9px;gap:8px}.paired-metrics>div{border-left:2px solid #d9e2f3;padding-left:8px}.metric-note{margin:10px 0 0;color:#738095;font-size:9px;line-height:1.55}.guardrail-card ul{margin:8px 0 0;padding-left:18px;color:#7b263b;font-size:10px;line-height:1.6}.guardrail-card p{margin:8px 0 0;color:#667389;font-size:10px}.conclusion{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:18px;margin-top:10px;border:1px solid #e0e5ed;border-left:4px solid #758095;border-radius:9px;background:#f8fafc;padding:12px}.conclusion.ready{border-left-color:#18794e;background:#f5fbf8}.conclusion h4{margin:2px 0 0;font-size:14px}.conclusion>p{max-width:580px;margin:0;color:#657187;font-size:10px;line-height:1.55}.conclusion>p.no-lift-claim{flex-basis:100%;max-width:none;border-top:1px solid #e6d9ac;padding-top:9px;color:#7c4b0d;font-weight:750}.conclusion-values{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:10px 18px}.conclusion-values>div{text-align:right}
.action-card{display:grid;gap:11px}.action-buttons{display:flex;flex-wrap:wrap;gap:8px}.online-button{min-height:44px;border-radius:8px;padding:9px 13px;font-size:11px;font-weight:750;cursor:pointer;touch-action:manipulation}.online-button.primary{border:1px solid var(--online-blue);background:var(--online-blue);color:#fff}.online-button.secondary{border:1px solid #c7d0de;background:#fff;color:#344057}.online-button.approve{border:1px solid #18794e;background:#18794e;color:#fff}.online-button.warning{border:1px solid #9a5b12;background:#fff9ee;color:#7c480c}.online-button.danger-outline{border:1px solid #b4233f;background:#fff;color:#a51f3c}.online-button.danger{border:1px solid #a81f3a;background:#a81f3a;color:#fff}.online-button:disabled{opacity:.45;cursor:not-allowed}.online-button:active:not(:disabled){filter:brightness(.93)}.action-help{margin:0;color:#657187;font-size:10px}.rollback-zone{display:grid;grid-template-columns:minmax(180px,1fr) minmax(220px,1fr) auto;gap:11px;align-items:end;margin-top:3px;border:1px solid #efbec8;border-radius:9px;background:#fff5f6;padding:11px}.rollback-zone strong{color:#92213a;font-size:11px}.rollback-zone p{margin:3px 0 0;color:#7c5260;font-size:9px;line-height:1.5}
.create-card{padding:0;overflow:hidden}.create-card summary{min-height:48px;display:flex;align-items:center;justify-content:space-between;padding:12px 14px;color:#263149;font-size:12px;font-weight:750;cursor:pointer;list-style:none}.create-card summary::after{content:'+';color:var(--online-blue);font-size:18px}.create-card[open] summary::after{content:'−'}.create-card>p{margin:0;padding:0 14px 10px;color:#6b778c;font-size:10px;line-height:1.5}.create-card form{display:grid;gap:10px;border-top:1px solid #edf0f4;padding:12px 14px 14px}.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:9px}.audit-card{padding:13px}.audit-card ol{list-style:none;margin:10px 0 0;padding:0;max-height:440px;overflow:auto}.audit-card li{position:relative;padding:10px 3px 11px 16px;border-bottom:1px solid #edf0f4}.audit-card li::before{content:'';position:absolute;left:2px;top:15px;width:6px;height:6px;border:2px solid var(--online-blue);border-radius:50%;background:#fff}.audit-card li:last-child{border-bottom:0}.audit-action{display:block;color:var(--online-blue);font-size:9px;font-weight:800}.audit-card li strong{display:block;margin-top:2px;font-size:11px}.audit-card li small,.audit-card li time{display:block;margin-top:3px;color:#748095;font-size:9px}.audit-card li p{margin:4px 0 0;color:#59667c;font-size:10px;line-height:1.45}.side-empty,.online-empty{min-height:110px;display:grid;place-items:center;align-content:center;gap:6px;color:#748095;text-align:center;font-size:11px}.online-empty strong{color:#344057;font-size:13px}.online-empty span{max-width:620px;line-height:1.55}
.online-button:focus-visible,.control-strip select:focus-visible,.action-card select:focus-visible,.action-card textarea:focus-visible,.rollback-zone input:focus-visible,.create-card summary:focus-visible,.create-card input:focus-visible,.create-card select:focus-visible,.create-card textarea:focus-visible{outline:3px solid rgba(30,64,175,.3);outline-offset:2px}@keyframes online-spin{to{transform:rotate(360deg)}}
@media(prefers-reduced-motion:reduce){.online-loading span{animation:none}.online-button{transition:none}}
@media(max-width:1100px){.online-layout{grid-template-columns:minmax(0,1fr) 300px}.facts-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.facts-grid>div,.facts-grid>div:nth-child(4n){border-right:1px solid #edf0f4;border-bottom:1px solid #edf0f4}.facts-grid>div:nth-child(2n){border-right:0}.facts-grid>div:nth-last-child(-n+2){border-bottom:0}.rollback-zone{grid-template-columns:1fr 1fr}.rollback-zone .online-button{grid-column:1/3}}
@media(max-width:850px){.online-panel{overflow:visible;padding:13px 14px 24px}.truth-banner{grid-template-columns:auto 1fr}.truth-banner>strong{grid-column:1/3;max-width:none}.online-layout{grid-template-columns:1fr}.control-strip{grid-template-columns:1fr 1fr}.control-strip .online-button{grid-column:1/3}.online-side{display:grid;grid-template-columns:1fr 1fr}.audit-card{grid-column:1/3}}
@media(max-width:560px){.truth-banner{grid-template-columns:1fr}.truth-signal{display:none}.truth-banner>strong{grid-column:1}.control-strip,.online-side,.evidence-grid,.form-grid{grid-template-columns:1fr}.control-strip .online-button,.audit-card,.evidence-grid .guardrail-card{grid-column:1}.facts-grid{grid-template-columns:1fr 1fr}.conclusion{display:block}.conclusion-values{justify-content:flex-start;margin-top:12px}.conclusion-values>div{text-align:left}.rollback-zone{grid-template-columns:1fr}.rollback-zone .online-button{grid-column:1}.control-strip select,.action-card select,.action-card textarea,.rollback-zone input,.create-card input,.create-card select,.create-card textarea{font-size:16px}.action-buttons{display:grid;grid-template-columns:1fr 1fr}.online-button{white-space:normal}}
</style>
