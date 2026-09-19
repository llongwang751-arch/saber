import { defineStore } from 'pinia'
import { fetchJSON } from '../api/client'

const ENDPOINTS = {
  readiness: '/api/online-experiments/readiness',
  deployments: '/api/online-experiments/deployments',
  experiments: '/api/online-experiments/experiments',
  audit: '/api/online-experiments/audit-events',
}
const TRANSITIONS = {
  submit: '/submit',
  decision: '/decision',
  start: '/start',
  ramp: '/ramp',
  pause: '/pause',
  resume: '/resume',
  complete: '/complete',
  rollback: '/rollback',
}

function requestKey(prefix) {
  const uuid = globalThis.crypto?.randomUUID?.()
  if (uuid) return `${prefix}-${uuid}`
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function itemsFrom(payload, ...keys) {
  if (Array.isArray(payload)) return payload
  for (const key of keys) {
    if (Array.isArray(payload?.[key])) return payload[key]
  }
  return Array.isArray(payload?.items) ? payload.items : []
}

function objectFrom(payload, ...keys) {
  for (const key of keys) {
    if (payload?.[key] && typeof payload[key] === 'object') return payload[key]
  }
  return payload && typeof payload === 'object' ? payload : null
}

function mutationBody(experiment, reason = '') {
  return {
    expected_generation: Number(experiment?.generation || 0),
    idempotency_key: requestKey('online-experiment'),
    reason,
  }
}

export const useExperiments = defineStore('online-experiments', {
  state: () => ({
    readiness: null,
    deployments: [],
    experiments: [],
    selectedExperiment: null,
    analysis: null,
    auditEvents: [],
    loading: false,
    detailLoading: false,
    actionLoading: false,
    error: '',
    message: '',
  }),
  getters: {
    selectedId: state => state.selectedExperiment?.id || '',
    provenance(state) {
      return state.selectedExperiment?.traffic_provenance
        || state.analysis?.traffic_provenance
        || state.analysis?.provenance
        || state.readiness?.traffic_provenance
        || state.readiness?.provenance
        || 'disabled'
    },
    hasProductionTraffic() {
      return this.provenance === 'production_authenticated'
        && this.analysis?.has_real_traffic === true
        && Number(this.analysis?.real_exposure_count || 0) > 0
    },
    canAnalyze(state) {
      return this.hasProductionTraffic
        && state.analysis?.sample?.sufficient === true
        && state.analysis?.analysis_status === 'confirmatory'
        && state.analysis?.truth === 'observed'
    },
    canClaimEffect(state) {
      return this.canAnalyze && state.analysis?.can_claim_effect === true
    },
  },
  actions: {
    async refresh() {
      this.loading = true
      this.error = ''
      this.readiness = null
      try {
        const readiness = await fetchJSON(ENDPOINTS.readiness)
        this.readiness = objectFrom(readiness, 'readiness')
        const [deployments, experiments] = await Promise.all([
          fetchJSON(`${ENDPOINTS.deployments}?limit=200`),
          fetchJSON(`${ENDPOINTS.experiments}?limit=200`),
        ])
        this.deployments = itemsFrom(deployments, 'deployments')
        this.experiments = itemsFrom(experiments, 'experiments')
        const selectedId = this.selectedExperiment?.id
        const selected = this.experiments.find(item => item.id === selectedId)
          || this.experiments[0]
          || null
        if (selected) await this.selectExperiment(selected.id, false)
        else {
          this.selectedExperiment = null
          this.analysis = null
          this.auditEvents = []
        }
      } catch (error) {
        this.deployments = []
        this.experiments = []
        this.selectedExperiment = null
        this.analysis = null
        this.auditEvents = []
        this.error = error.message || '真实在线实验控制面加载失败'
      } finally {
        this.loading = false
      }
    },

    async selectExperiment(id, setLoading = true) {
      if (!id) return null
      if (setLoading) this.detailLoading = true
      this.error = ''
      try {
        const encoded = encodeURIComponent(id)
        const [detail, analysis, audit] = await Promise.all([
          fetchJSON(`${ENDPOINTS.experiments}/${encoded}`),
          fetchJSON(`${ENDPOINTS.experiments}/${encoded}/analysis`).catch(error => ({ __error: error.message })),
          fetchJSON(`${ENDPOINTS.experiments}/${encoded}/audit?limit=200`)
            .catch(() => fetchJSON(`${ENDPOINTS.audit}?experiment_id=${encoded}&limit=200`))
            .catch(error => ({ __error: error.message })),
        ])
        this.selectedExperiment = objectFrom(detail, 'experiment')
        this.analysis = analysis?.__error ? null : objectFrom(analysis, 'analysis')
        this.auditEvents = audit?.__error ? [] : itemsFrom(audit, 'audit_events', 'events')
        return this.selectedExperiment
      } catch (error) {
        this.error = error.message || '在线实验详情加载失败'
        throw error
      } finally {
        if (setLoading) this.detailLoading = false
      }
    },

    async createDeployment(proposalId) {
      return this._act(
        '正在创建候选部署…',
        async () => objectFrom(await fetchJSON(ENDPOINTS.deployments, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            proposal_id: proposalId,
            idempotency_key: requestKey('online-deployment'),
          }),
        }), 'deployment'),
        '候选部署已从获批离线证据编译；它尚未接入生产流量。',
      )
    },

    async loadDeployment(id) {
      if (!id) return null
      return objectFrom(
        await fetchJSON(`${ENDPOINTS.deployments}/${encodeURIComponent(id)}`),
        'deployment',
      )
    },

    async createExperiment(values) {
      return this._act(
        '正在创建预注册实验…',
        async () => objectFrom(await fetchJSON(ENDPOINTS.experiments, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: values.name,
            hypothesis: values.hypothesis || '',
            candidate_deployment_id: values.candidate_deployment_id,
            surface: 'rag_chat',
            primary_metric: 'positive_feedback',
            baseline_rate: Number(values.baseline_rate),
            minimum_detectable_effect: Number(values.minimum_detectable_effect),
            alpha: Number(values.alpha),
            power: Number(values.power),
            enrollment_bps: Number(values.enrollment_bps),
            candidate_allocation_bps: Number(values.candidate_allocation_bps),
            control_overrides: {},
            min_duration_hours: Number(values.min_duration_hours),
            max_duration_hours: Number(values.max_duration_hours),
            attribution_window_hours: Number(values.attribution_window_hours),
            idempotency_key: requestKey('online-experiment-create'),
          }),
        }), 'experiment'),
        '预注册实验已创建，尚未提交审批，也未分配任何流量。',
        true,
      )
    },

    async submit(reason = '') {
      return this._transition('submit', mutationBody(this.selectedExperiment, reason), '实验已提交人工审批。')
    },

    async decide(decision, reason = '') {
      return this._transition('decision', {
        ...mutationBody(this.selectedExperiment, reason),
        decision,
      }, decision === 'approve' ? '实验已批准，仍需显式启动灰度（Canary）。' : '实验已拒绝，不会接入流量。')
    },

    async start(reason = '', targetStatus = 'canary') {
      return this._transition('start', {
        ...mutationBody(this.selectedExperiment, reason),
        target_status: targetStatus,
      }, targetStatus === 'canary' ? '灰度（Canary）已启动，请持续观察安全与数据门禁。' : '实验已启动。')
    },

    async ramp(targetEnrollmentBps, reason = '') {
      return this._transition('ramp', {
        ...mutationBody(this.selectedExperiment, reason),
        target_enrollment_bps: Number(targetEnrollmentBps),
      }, `流量已显式提升到 ${(Number(targetEnrollmentBps) / 100).toFixed(1)}%。`)
    },

    async pause(reason = '') {
      return this._transition('pause', mutationBody(this.selectedExperiment, reason), '实验已安全暂停，不再继续分配新增流量。')
    },

    async resume(reason = '') {
      return this._transition('resume', mutationBody(this.selectedExperiment, reason), '实验已恢复，请继续观察门禁。')
    },

    async complete(reason = '') {
      return this._transition('complete', mutationBody(this.selectedExperiment, reason), '实验已停止收集并进入结论检查。')
    },

    async rollback(reason = '') {
      return this._transition('rollback', mutationBody(this.selectedExperiment, reason), '实验已回滚，候选配置不再承接流量。')
    },

    async _transition(action, body, successMessage) {
      const experiment = this.selectedExperiment
      if (!experiment?.id) throw new Error('请先选择一个在线实验')
      return this._act(
        '正在提交受控变更…',
        async () => objectFrom(await fetchJSON(`${ENDPOINTS.experiments}/${encodeURIComponent(experiment.id)}${TRANSITIONS[action]}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        }), 'experiment'),
        successMessage,
        true,
      )
    },

    async _act(pendingMessage, request, successMessage, selectResult = false) {
      this.actionLoading = true
      this.error = ''
      this.message = pendingMessage
      try {
        const result = await request()
        if (selectResult && result?.id) this.selectedExperiment = result
        this.message = successMessage
        await this.refresh()
        if (result?.id && this.experiments.some(item => item.id === result.id)) {
          await this.selectExperiment(result.id, false)
        }
        return result
      } catch (error) {
        this.error = error.message || '在线实验操作失败'
        this.message = ''
        throw error
      } finally {
        this.actionLoading = false
      }
    },

    reset() {
      this.readiness = null
      this.deployments = []
      this.experiments = []
      this.selectedExperiment = null
      this.analysis = null
      this.auditEvents = []
      this.loading = false
      this.detailLoading = false
      this.actionLoading = false
      this.error = ''
      this.message = ''
    },
  },
})
