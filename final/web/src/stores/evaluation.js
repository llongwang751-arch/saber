import { defineStore } from 'pinia'
import { apiFetch, fetchJSON } from '../api/client'

const terminal = new Set(['completed', 'failed', 'cancelled'])

function requestKey(prefix) {
  const uuid = globalThis.crypto?.randomUUID?.()
  if (uuid) return `${prefix}-${uuid}`
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function messagesFrom(value) {
  const items = Array.isArray(value) ? value : value ? [value] : []
  return items.map(item => {
    if (!item || typeof item !== 'object') return String(item)
    return String(item.message || item.detail || item.reason || item.code || '').trim()
  }).filter(Boolean)
}

export const useEvaluation = defineStore('evaluation', {
  state: () => ({
    datasets: [],
    runs: [],
    badcases: [],
    results: [],
    selectedRun: null,
    selectedTrace: null,
    comparison: null,
    comparisonRunIds: [],
    metrics: {},
    readiness: null,
    readinessError: '',
    executionByRunId: {},
    activeOperation: null,
    operationMessage: '',
    strategies: [],
    strategyPointer: null,
    promotionProposals: [],
    selectedProposal: null,
    strategyLoading: false,
    strategyActionLoading: false,
    strategyError: '',
    strategyMessage: '',
    evolutionSuggestions: [],
    selectedEvolutionSuggestion: null,
    evolutionAuditEvents: [],
    evolutionLoading: false,
    evolutionActionLoading: false,
    evolutionError: '',
    evolutionMessage: '',
    loading: false,
    readinessLoading: false,
    bootstrapping: false,
    runningLive: false,
    error: '',
  }),
  getters: {
    latestRun: state => state.selectedRun || state.runs[0] || null,
    summary() { return this.latestRun?.summary || {} },
    liveReadiness: state => state.readiness?.live || {},
    liveReady() { return this.liveReadiness.ready === true },
    liveReasons() {
      const reasons = this.liveReadiness.reasons || this.liveReadiness.blockers || []
      return messagesFrom(reasons)
    },
    liveWarnings() { return messagesFrom(this.liveReadiness.warnings) },
    selectedExecution(state) {
      const run = state.selectedRun
      if (!run) return null
      return state.executionByRunId[run.id]
        || run.execution
        || run.metadata?.execution
        || run.config?.execution
        || null
    },
    completedRuns: state => state.runs.filter(run => run.status === 'completed'),
    currentStrategy(state) {
      return state.strategyPointer?.current_strategy
        || state.strategies.find(item => item.id === state.strategyPointer?.current_strategy_version_id)
        || null
    },
    previousStrategy(state) {
      return state.strategyPointer?.previous_strategy
        || state.strategies.find(item => item.id === state.strategyPointer?.previous_strategy_version_id)
        || null
    },
  },
  actions: {
    async refreshReadiness() {
      this.readinessLoading = true
      this.readinessError = ''
      try {
        this.readiness = await fetchJSON('/api/eval/readiness')
      } catch (e) {
        this.readiness = {
          live: {
            ready: false,
            reasons: ['无法确认本地智能体是否就绪，请检查后端评测服务。'],
          },
        }
        this.readinessError = e.message || '评测就绪状态加载失败'
      } finally {
        this.readinessLoading = false
      }
      return this.readiness
    },
    async refresh() {
      this.loading = true
      this.error = ''
      try {
        const [datasets, runs, badcases, metrics] = await Promise.all([
          fetchJSON('/api/eval/datasets'),
          fetchJSON('/api/eval/runs?limit=100'),
          fetchJSON('/api/eval/badcases?limit=200'),
          fetchJSON('/api/eval/metrics/summary'),
          this.refreshReadiness(),
        ])
        this.datasets = datasets.datasets || []
        this.runs = runs.runs || []
        this.badcases = badcases.badcases || []
        this.metrics = metrics || {}
        if (this.selectedRun) {
          this.selectedRun = this.runs.find(item => item.id === this.selectedRun.id) || this.runs[0] || null
        } else {
          this.selectedRun = this.runs[0] || null
        }
        if (this.selectedRun) await this.loadRun(this.selectedRun.id, false)
      } catch (e) {
        this.error = e.message || '评测数据加载失败'
      } finally {
        this.loading = false
      }
    },
    async bootstrapDemo() {
      this.bootstrapping = true
      this.error = ''
      this.comparison = null
      this.comparisonRunIds = []
      this.operationMessage = ''
      this.activeOperation = { kind: 'replay', completed: 0, total: 0, status: 'starting' }
      try {
        const demo = await fetchJSON('/api/eval/demo/bootstrap', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ execute: true }),
        })
        const ids = [demo.baseline_run?.id, demo.candidate_run?.id].filter(Boolean)
        if (ids.length !== 2) throw new Error('合成回放未返回完整的基线与候选运行编号')
        this.rememberExecution(ids, demo.execution || {
          adapter: 'replay',
          execution: 'preauthored-output-replay',
          data_policy: demo.dataset?.metadata?.data_policy || 'synthetic-only',
          real_vs_mock: 'synthetic',
        })
        const caseCount = Number(demo.dataset_version?.case_count || 0)
        const outcome = await this.waitForRuns(ids, { kind: 'replay', totalHint: caseCount * ids.length })
        await this.refresh()
        if (ids.length === 2) {
          this.comparison = await fetchJSON('/api/eval/runs/compare', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ baseline_run_id: ids[0], candidate_run_id: ids[1] }),
          })
          this.comparisonRunIds = [...ids]
          await this.loadRun(ids[1])
        }
        this.operationMessage = !outcome.finished
          ? '合成回放仍在后台运行，可稍后刷新查看结果。'
          : ids.length
          ? `合成回放已完成，共执行 ${this.activeOperation?.total || caseCount * ids.length || 0} 次。`
          : '合成回放数据已创建。'
      } catch (e) {
        this.error = e.message || '演示评测执行失败'
      } finally {
        this.bootstrapping = false
        this.activeOperation = null
      }
    },
    async runLiveDemo() {
      if (!this.liveReady) {
        this.error = this.liveReasons[0] || '本地智能体尚未就绪，暂时无法运行本地实测。'
        return
      }
      this.runningLive = true
      this.error = ''
      this.comparison = null
      this.comparisonRunIds = []
      this.operationMessage = ''
      this.activeOperation = { kind: 'live', completed: 0, total: 0, status: 'starting' }
      try {
        const demo = await fetchJSON('/api/eval/demo/live', { method: 'POST' })
        const run = demo.run || demo.eval_run || null
        const ids = [run?.id].filter(Boolean)
        if (!ids.length) throw new Error('本地智能体实测未返回运行编号')
        this.rememberExecution(ids, demo.execution || {
          adapter: 'local',
          execution: 'live-agent',
          data_policy: demo.dataset?.metadata?.data_policy || 'unknown',
          real_vs_mock: 'unknown',
        })
        const caseCount = Number(demo.dataset_version?.case_count || run?.summary?.total || 0)
        const outcome = await this.waitForRuns(ids, { kind: 'live', totalHint: caseCount })
        await this.refresh()
        await this.loadRun(ids[0])
        this.operationMessage = outcome.finished
          ? `本地智能体实测已完成，共执行 ${this.activeOperation?.total || caseCount || 0} 条用例。`
          : '本地智能体实测仍在后台运行，可稍后刷新查看结果。'
      } catch (e) {
        this.error = e.message || '本地智能体实测执行失败'
      } finally {
        this.runningLive = false
        this.activeOperation = null
        await this.refreshReadiness()
      }
    },
    rememberExecution(ids, execution) {
      if (!execution || typeof execution !== 'object') return
      this.executionByRunId = {
        ...this.executionByRunId,
        ...Object.fromEntries(ids.map(id => [id, { ...execution }])),
      }
    },
    async waitForRuns(ids, { kind = 'run', totalHint = 0 } = {}) {
      const deadline = Date.now() + 300000
      while (ids.length && Date.now() < deadline) {
        const progress = await Promise.all(ids.map(id => fetchJSON(`/api/eval/runs/${id}/progress`)))
        const completed = progress.reduce((sum, item) => sum + Number(item.completed || 0), 0)
        const reportedTotal = progress.reduce((sum, item) => sum + Number(item.total || 0), 0)
        this.activeOperation = {
          kind,
          completed,
          total: reportedTotal || Number(totalHint || 0),
          status: progress.every(item => terminal.has(item.status)) ? 'completed' : 'running',
        }
        if (progress.every(item => terminal.has(item.status))) return { finished: true, progress }
        await new Promise(resolve => setTimeout(resolve, 500))
      }
      return { finished: false, progress: [] }
    },
    async loadRun(id, setLoading = true) {
      if (setLoading) this.loading = true
      this.error = ''
      try {
        const [run, results, badcases] = await Promise.all([
          fetchJSON(`/api/eval/runs/${id}`),
          fetchJSON(`/api/eval/runs/${id}/results`),
          fetchJSON(`/api/eval/runs/${id}/badcases`),
        ])
        this.selectedRun = run
        this.results = results.results || []
        this.badcases = badcases.badcases || []
        this.selectedTrace = null
        if (this.comparisonRunIds.length && !this.comparisonRunIds.includes(id)) {
          this.comparison = null
          this.comparisonRunIds = []
        }
      } catch (e) {
        this.error = e.message || '评测运行加载失败'
      } finally {
        if (setLoading) this.loading = false
      }
    },
    async openTrace(caseRunId) {
      try {
        this.selectedTrace = await fetchJSON(`/api/eval/case-runs/${caseRunId}/trace`)
      } catch (e) {
        this.error = e.message || '执行轨迹加载失败'
      }
    },
    async triage(item, owner) {
      await fetchJSON(`/api/eval/badcases/${item.id}/triage`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: 'triaged', owner: owner || 'interviewer-demo' }),
      })
      if (this.selectedRun) await this.loadRun(this.selectedRun.id)
    },
    async loadStrategyWorkspace() {
      this.strategyLoading = true
      this.strategyError = ''
      try {
        const [strategiesPayload, proposalsPayload, pointerPayload] = await Promise.all([
          fetchJSON('/api/eval/strategies?limit=200'),
          fetchJSON('/api/eval/promotion-proposals?limit=200'),
          fetchJSON('/api/eval/strategies/current').catch(() => null),
        ])
        this.strategies = strategiesPayload.strategies || strategiesPayload.items || []
        this.promotionProposals = proposalsPayload.proposals || proposalsPayload.items || []
        this.strategyPointer = pointerPayload?.pointer || pointerPayload || null
        const selectedId = this.selectedProposal?.id
        this.selectedProposal = this.promotionProposals.find(item => item.id === selectedId)
          || this.promotionProposals[0]
          || null
        return {
          strategies: this.strategies,
          proposals: this.promotionProposals,
          pointer: this.strategyPointer,
        }
      } catch (error) {
        this.strategyError = error.message || '离线策略审批数据加载失败'
        throw error
      } finally {
        this.strategyLoading = false
      }
    },
    async loadProposal(id) {
      if (!id) return null
      this.strategyError = ''
      try {
        const payload = await fetchJSON(`/api/eval/promotion-proposals/${encodeURIComponent(id)}`)
        this.selectedProposal = payload.proposal || payload
        return this.selectedProposal
      } catch (error) {
        this.strategyError = error.message || '策略候选详情加载失败'
        throw error
      }
    },
    async createStrategy(name, manifest) {
      this.strategyActionLoading = true
      this.strategyError = ''
      this.strategyMessage = ''
      try {
        const payload = await fetchJSON('/api/eval/strategies', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, manifest }),
        })
        const strategy = payload.strategy || payload
        this.strategyMessage = strategy.created === false
          ? '相同内容的策略版本已存在，已返回原版本。'
          : '离线策略版本已创建；它尚未激活，也不会自动上线。'
        await this.loadStrategyWorkspace()
        return strategy
      } catch (error) {
        this.strategyError = error.message || '离线策略创建失败'
        throw error
      } finally {
        this.strategyActionLoading = false
      }
    },
    async createPromotionProposal(baselineRunId, candidateRunId) {
      this.strategyActionLoading = true
      this.strategyError = ''
      this.strategyMessage = ''
      try {
        const payload = await fetchJSON('/api/eval/promotion-proposals', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            baseline_run_id: baselineRunId,
            candidate_run_id: candidateRunId,
          }),
        })
        this.selectedProposal = payload.proposal || payload
        this.strategyMessage = this.selectedProposal.status === 'blocked'
          ? '候选已生成，但统计或门禁证据不足，当前不可审批激活。'
          : '候选已生成，等待人工审阅；不会自动激活。'
        await this.loadStrategyWorkspace()
        return this.selectedProposal
      } catch (error) {
        this.strategyError = error.message || '策略候选创建失败'
        throw error
      } finally {
        this.strategyActionLoading = false
      }
    },
    async decideProposal(id, decision, note = '') {
      this.strategyActionLoading = true
      this.strategyError = ''
      this.strategyMessage = ''
      try {
        const payload = await fetchJSON(`/api/eval/promotion-proposals/${encodeURIComponent(id)}/decision`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ decision, note }),
        })
        this.selectedProposal = payload.proposal || payload
        this.strategyMessage = decision === 'approve'
          ? '候选已批准，但仍未激活；需要再次显式确认。'
          : '候选已拒绝，不会进入激活流程。'
        await this.loadStrategyWorkspace()
        return this.selectedProposal
      } catch (error) {
        this.strategyError = error.message || '审批决定提交失败'
        throw error
      } finally {
        this.strategyActionLoading = false
      }
    },
    async activateProposal(id, note = '') {
      this.strategyActionLoading = true
      this.strategyError = ''
      this.strategyMessage = ''
      try {
        const payload = await fetchJSON(`/api/eval/promotion-proposals/${encodeURIComponent(id)}/activate`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ note }),
        })
        this.strategyMessage = payload.idempotent
          ? '该候选已经激活，本次请求未重复变更策略指针。'
          : '离线评测策略指针已显式激活；这不是线上流量发布。'
        await this.loadStrategyWorkspace()
        return payload
      } catch (error) {
        this.strategyError = error.message || '策略激活失败'
        throw error
      } finally {
        this.strategyActionLoading = false
      }
    },
    async rollbackStrategy(reason = '') {
      const expectedCurrent = this.strategyPointer?.current_strategy_version_id || ''
      this.strategyActionLoading = true
      this.strategyError = ''
      this.strategyMessage = ''
      try {
        const body = {
          idempotency_key: requestKey('strategy-rollback'),
          reason,
        }
        if (expectedCurrent) body.expected_current_strategy_version_id = expectedCurrent
        const payload = await fetchJSON('/api/eval/strategies/rollback', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        })
        this.strategyMessage = payload.idempotent
          ? '该回滚请求已经处理，没有重复变更。'
          : '离线评测策略已人工回滚到上一版本。'
        await this.loadStrategyWorkspace()
        return payload
      } catch (error) {
        this.strategyError = error.message || '人工回滚失败'
        throw error
      } finally {
        this.strategyActionLoading = false
      }
    },
    async loadEvolutionWorkspace() {
      this.evolutionLoading = true
      this.evolutionError = ''
      try {
        const payload = await fetchJSON('/api/eval/evolution-suggestions?limit=200')
        this.evolutionSuggestions = payload.suggestions || payload.items || []
        const selectedId = this.selectedEvolutionSuggestion?.id
        this.selectedEvolutionSuggestion = this.evolutionSuggestions.find(item => item.id === selectedId)
          || this.evolutionSuggestions[0]
          || null
        await this.loadEvolutionAudit(this.selectedEvolutionSuggestion?.id)
        return this.evolutionSuggestions
      } catch (error) {
        this.evolutionError = error.message || '受控策略建议加载失败'
        throw error
      } finally {
        this.evolutionLoading = false
      }
    },
    async selectEvolutionSuggestion(id) {
      if (!id) {
        this.selectedEvolutionSuggestion = null
        this.evolutionAuditEvents = []
        return null
      }
      this.evolutionError = ''
      try {
        const payload = await fetchJSON(`/api/eval/evolution-suggestions/${encodeURIComponent(id)}`)
        this.selectedEvolutionSuggestion = payload.suggestion || payload
        await this.loadEvolutionAudit(id)
        return this.selectedEvolutionSuggestion
      } catch (error) {
        this.evolutionError = error.message || '受控策略建议详情加载失败'
        throw error
      }
    },
    async loadEvolutionAudit(id) {
      if (!id) {
        this.evolutionAuditEvents = []
        return []
      }
      const payload = await fetchJSON(`/api/eval/evolution-suggestions/audit-events?suggestion_id=${encodeURIComponent(id)}&limit=100`)
      this.evolutionAuditEvents = payload.events || []
      return this.evolutionAuditEvents
    },
    async createEvolutionSuggestion(sourceRunId) {
      this.evolutionActionLoading = true
      this.evolutionError = ''
      this.evolutionMessage = ''
      try {
        const payload = await fetchJSON('/api/eval/evolution-suggestions', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            source_run_id: sourceRunId,
            idempotency_key: requestKey('evolution-create'),
          }),
        })
        this.selectedEvolutionSuggestion = payload.suggestion || payload
        this.evolutionMessage = payload.created === false
          ? '同一运行和规则版本的建议已经存在，本次没有重复创建。'
          : '实验假设已生成，尚未接受、物化或应用，也不代表效果提升。'
        await this.loadEvolutionWorkspace()
        return this.selectedEvolutionSuggestion
      } catch (error) {
        this.evolutionError = error.message || '实验假设生成失败'
        throw error
      } finally {
        this.evolutionActionLoading = false
      }
    },
    async decideEvolutionSuggestion(id, decision, note = '') {
      const current = this.evolutionSuggestions.find(item => item.id === id)
        || this.selectedEvolutionSuggestion
      this.evolutionActionLoading = true
      this.evolutionError = ''
      this.evolutionMessage = ''
      try {
        const payload = await fetchJSON(`/api/eval/evolution-suggestions/${encodeURIComponent(id)}/decision`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            decision,
            note,
            expected_generation: Number(current?.generation || 0),
            idempotency_key: requestKey('evolution-review'),
          }),
        })
        this.selectedEvolutionSuggestion = payload.suggestion || payload
        this.evolutionMessage = decision === 'accept'
          ? '建议已由人工接受，但尚未物化为策略版本，更没有上线。'
          : '建议已被人工拒绝，不会进入后续流程。'
        await this.loadEvolutionWorkspace()
        return this.selectedEvolutionSuggestion
      } catch (error) {
        this.evolutionError = error.message || '受控策略建议审批失败'
        throw error
      } finally {
        this.evolutionActionLoading = false
      }
    },
    async materializeEvolutionSuggestion(id, name = '') {
      const current = this.evolutionSuggestions.find(item => item.id === id)
        || this.selectedEvolutionSuggestion
      this.evolutionActionLoading = true
      this.evolutionError = ''
      this.evolutionMessage = ''
      try {
        const body = {
          expected_generation: Number(current?.generation || 0),
          idempotency_key: requestKey('evolution-materialize'),
        }
        if (String(name || '').trim()) body.name = String(name).trim()
        const payload = await fetchJSON(`/api/eval/evolution-suggestions/${encodeURIComponent(id)}/materialize`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        })
        this.selectedEvolutionSuggestion = payload.suggestion || this.selectedEvolutionSuggestion
        this.evolutionMessage = '已显式物化为离线策略版本；仍未创建晋级候选、未激活、未部署。'
        await Promise.all([this.loadEvolutionWorkspace(), this.loadStrategyWorkspace()])
        return payload
      } catch (error) {
        this.evolutionError = error.message || '受控策略建议物化失败'
        throw error
      } finally {
        this.evolutionActionLoading = false
      }
    },
    async downloadReport(format = 'markdown') {
      if (!this.selectedRun) return
      const response = await apiFetch(`/api/eval/runs/${this.selectedRun.id}/report?format=${format}`)
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `eval-${this.selectedRun.id}.${format === 'csv' ? 'csv' : 'md'}`
      link.click()
      URL.revokeObjectURL(url)
    },
    reset() {
      this.datasets = []
      this.runs = []
      this.badcases = []
      this.results = []
      this.selectedRun = null
      this.selectedTrace = null
      this.comparison = null
      this.comparisonRunIds = []
      this.metrics = {}
      this.readiness = null
      this.readinessError = ''
      this.executionByRunId = {}
      this.activeOperation = null
      this.operationMessage = ''
      this.strategies = []
      this.strategyPointer = null
      this.promotionProposals = []
      this.selectedProposal = null
      this.strategyLoading = false
      this.strategyActionLoading = false
      this.strategyError = ''
      this.strategyMessage = ''
      this.evolutionSuggestions = []
      this.selectedEvolutionSuggestion = null
      this.evolutionAuditEvents = []
      this.evolutionLoading = false
      this.evolutionActionLoading = false
      this.evolutionError = ''
      this.evolutionMessage = ''
      this.error = ''
    },
  },
})
