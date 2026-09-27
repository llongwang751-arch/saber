// stores/evaluation.ts — 智能体评测工作台状态。Pinia → Zustand 移植：
// 全部 action 与端点调用逐字对齐；Pinia getters 转为导出的纯选择器（selectXxx），
// 组件中以原始 state 切片在渲染期计算派生值，保证引用稳定。
import { create } from 'zustand'
import { apiFetch, fetchJSON } from '../api/client'

const terminal = new Set(['completed', 'failed', 'cancelled'])
export const EMPTY: any = {}

function requestKey(prefix: string): string {
  const uuid = globalThis.crypto?.randomUUID?.()
  if (uuid) return `${prefix}-${uuid}`
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function messagesFrom(value: any): string[] {
  const items = Array.isArray(value) ? value : value ? [value] : []
  return items.map((item: any) => {
    if (!item || typeof item !== 'object') return String(item)
    return String(item.message || item.detail || item.reason || item.code || '').trim()
  }).filter(Boolean)
}

export interface EvaluationState {
  datasets: any[]
  runs: any[]
  badcases: any[]
  results: any[]
  selectedRun: any
  selectedTrace: any
  comparison: any
  comparisonRunIds: string[]
  metrics: any
  readiness: any
  readinessError: string
  executionByRunId: Record<string, any>
  activeOperation: any
  operationMessage: string
  strategies: any[]
  strategyPointer: any
  promotionProposals: any[]
  selectedProposal: any
  strategyLoading: boolean
  strategyActionLoading: boolean
  strategyError: string
  strategyMessage: string
  evolutionSuggestions: any[]
  selectedEvolutionSuggestion: any
  evolutionAuditEvents: any[]
  evolutionLoading: boolean
  evolutionActionLoading: boolean
  evolutionError: string
  evolutionMessage: string
  loading: boolean
  readinessLoading: boolean
  bootstrapping: boolean
  runningLive: boolean
  error: string

  refreshReadiness(): Promise<any>
  refresh(): Promise<void>
  bootstrapDemo(): Promise<void>
  runLiveDemo(): Promise<void>
  rememberExecution(ids: string[], execution: any): void
  waitForRuns(ids: string[], opts?: { kind?: string; totalHint?: number }): Promise<{ finished: boolean; progress: any[] }>
  loadRun(id: string, setLoading?: boolean): Promise<void>
  openTrace(caseRunId: string): Promise<void>
  triage(item: any, owner: string): Promise<void>
  loadStrategyWorkspace(): Promise<any>
  loadProposal(id: string): Promise<any>
  createStrategy(name: string, manifest: any): Promise<any>
  createPromotionProposal(baselineRunId: string, candidateRunId: string): Promise<any>
  decideProposal(id: string, decision: string, note?: string): Promise<any>
  activateProposal(id: string, note?: string): Promise<any>
  rollbackStrategy(reason?: string): Promise<any>
  loadEvolutionWorkspace(): Promise<any[]>
  selectEvolutionSuggestion(id: string): Promise<any>
  loadEvolutionAudit(id: string): Promise<any[]>
  createEvolutionSuggestion(sourceRunId: string): Promise<any>
  decideEvolutionSuggestion(id: string, decision: string, note?: string): Promise<any>
  materializeEvolutionSuggestion(id: string, name?: string): Promise<any>
  downloadReport(format?: string): Promise<void>
  clearTrace(): void
  clearStrategyMessage(): void
  reset(): void
}

export const useEvaluation = create<EvaluationState>()((set, get) => ({
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

  async refreshReadiness() {
    set({ readinessLoading: true, readinessError: '' })
    try {
      const readiness = await fetchJSON('/api/eval/readiness')
      set({ readiness })
    } catch (e: any) {
      set({
        readiness: {
          live: {
            ready: false,
            reasons: ['无法确认本地智能体是否就绪，请检查后端评测服务。'],
          },
        },
        readinessError: e.message || '评测就绪状态加载失败',
      })
    } finally {
      set({ readinessLoading: false })
    }
    return get().readiness
  },
  async refresh() {
    set({ loading: true, error: '' })
    try {
      const [datasets, runs, badcases, metrics] = await Promise.all([
        fetchJSON('/api/eval/datasets'),
        fetchJSON('/api/eval/runs?limit=100'),
        fetchJSON('/api/eval/badcases?limit=200'),
        fetchJSON('/api/eval/metrics/summary'),
        get().refreshReadiness(),
      ])
      const runList = runs.runs || []
      const prevSelected = get().selectedRun
      const nextSelected = prevSelected
        ? (runList.find((item: any) => item.id === prevSelected.id) || runList[0] || null)
        : (runList[0] || null)
      set({
        datasets: datasets.datasets || [],
        runs: runList,
        badcases: badcases.badcases || [],
        metrics: metrics || {},
        selectedRun: nextSelected,
      })
      if (get().selectedRun) await get().loadRun(get().selectedRun.id, false)
    } catch (e: any) {
      set({ error: e.message || '评测数据加载失败' })
    } finally {
      set({ loading: false })
    }
  },
  async bootstrapDemo() {
    set({ bootstrapping: true, error: '', comparison: null, comparisonRunIds: [], operationMessage: '', activeOperation: { kind: 'replay', completed: 0, total: 0, status: 'starting' } })
    try {
      const demo = await fetchJSON('/api/eval/demo/bootstrap', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ execute: true }),
      })
      const ids = [demo.baseline_run?.id, demo.candidate_run?.id].filter(Boolean)
      if (ids.length !== 2) throw new Error('合成回放未返回完整的基线与候选运行编号')
      get().rememberExecution(ids, demo.execution || {
        adapter: 'replay',
        execution: 'preauthored-output-replay',
        data_policy: demo.dataset?.metadata?.data_policy || 'synthetic-only',
        real_vs_mock: 'synthetic',
      })
      const caseCount = Number(demo.dataset_version?.case_count || 0)
      const outcome = await get().waitForRuns(ids, { kind: 'replay', totalHint: caseCount * ids.length })
      await get().refresh()
      if (ids.length === 2) {
        const comparison = await fetchJSON('/api/eval/runs/compare', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ baseline_run_id: ids[0], candidate_run_id: ids[1] }),
        })
        set({ comparison, comparisonRunIds: [...ids] })
        await get().loadRun(ids[1])
      }
      set({
        operationMessage: !outcome.finished
          ? '合成回放仍在后台运行，可稍后刷新查看结果。'
          : ids.length
          ? `合成回放已完成，共执行 ${get().activeOperation?.total || caseCount * ids.length || 0} 次。`
          : '合成回放数据已创建。',
      })
    } catch (e: any) {
      set({ error: e.message || '演示评测执行失败' })
    } finally {
      set({ bootstrapping: false, activeOperation: null })
    }
  },
  async runLiveDemo() {
    const state = get()
    if (!selectLiveReady(state)) {
      set({ error: selectLiveReasons(state)[0] || '本地智能体尚未就绪，暂时无法运行本地实测。' })
      return
    }
    set({ runningLive: true, error: '', comparison: null, comparisonRunIds: [], operationMessage: '', activeOperation: { kind: 'live', completed: 0, total: 0, status: 'starting' } })
    try {
      const demo = await fetchJSON('/api/eval/demo/live', { method: 'POST' })
      const run = demo.run || demo.eval_run || null
      const ids = [run?.id].filter(Boolean)
      if (!ids.length) throw new Error('本地智能体实测未返回运行编号')
      get().rememberExecution(ids, demo.execution || {
        adapter: 'local',
        execution: 'live-agent',
        data_policy: demo.dataset?.metadata?.data_policy || 'unknown',
        real_vs_mock: 'unknown',
      })
      const caseCount = Number(demo.dataset_version?.case_count || run?.summary?.total || 0)
      const outcome = await get().waitForRuns(ids, { kind: 'live', totalHint: caseCount })
      await get().refresh()
      await get().loadRun(ids[0])
      set({
        operationMessage: outcome.finished
          ? `本地智能体实测已完成，共执行 ${get().activeOperation?.total || caseCount || 0} 条用例。`
          : '本地智能体实测仍在后台运行，可稍后刷新查看结果。',
      })
    } catch (e: any) {
      set({ error: e.message || '本地智能体实测执行失败' })
    } finally {
      set({ runningLive: false, activeOperation: null })
      await get().refreshReadiness()
    }
  },
  rememberExecution(ids, execution) {
    if (!execution || typeof execution !== 'object') return
    set(state => ({
      executionByRunId: {
        ...state.executionByRunId,
        ...Object.fromEntries(ids.map(id => [id, { ...execution }])),
      },
    }))
  },
  async waitForRuns(ids, { kind = 'run', totalHint = 0 } = {}) {
    const deadline = Date.now() + 300000
    while (ids.length && Date.now() < deadline) {
      const progress = await Promise.all(ids.map(id => fetchJSON(`/api/eval/runs/${id}/progress`)))
      const completed = progress.reduce((sum: number, item: any) => sum + Number(item.completed || 0), 0)
      const reportedTotal = progress.reduce((sum: number, item: any) => sum + Number(item.total || 0), 0)
      set({
        activeOperation: {
          kind,
          completed,
          total: reportedTotal || Number(totalHint || 0),
          status: progress.every((item: any) => terminal.has(item.status)) ? 'completed' : 'running',
        },
      })
      if (progress.every((item: any) => terminal.has(item.status))) return { finished: true, progress }
      await new Promise(resolve => setTimeout(resolve, 500))
    }
    return { finished: false, progress: [] }
  },
  async loadRun(id, setLoading = true) {
    if (setLoading) set({ loading: true })
    set({ error: '' })
    try {
      const [run, results, badcases] = await Promise.all([
        fetchJSON(`/api/eval/runs/${id}`),
        fetchJSON(`/api/eval/runs/${id}/results`),
        fetchJSON(`/api/eval/runs/${id}/badcases`),
      ])
      set(state => ({
        selectedRun: run,
        results: results.results || [],
        badcases: badcases.badcases || [],
        selectedTrace: null,
        comparison: state.comparisonRunIds.length && !state.comparisonRunIds.includes(id) ? null : state.comparison,
        comparisonRunIds: state.comparisonRunIds.length && !state.comparisonRunIds.includes(id) ? [] : state.comparisonRunIds,
      }))
    } catch (e: any) {
      set({ error: e.message || '评测运行加载失败' })
    } finally {
      if (setLoading) set({ loading: false })
    }
  },
  async openTrace(caseRunId) {
    try {
      set({ selectedTrace: await fetchJSON(`/api/eval/case-runs/${caseRunId}/trace`) })
    } catch (e: any) {
      set({ error: e.message || '执行轨迹加载失败' })
    }
  },
  async triage(item, owner) {
    await fetchJSON(`/api/eval/badcases/${item.id}/triage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: 'triaged', owner: owner || 'interviewer-demo' }),
    })
    if (get().selectedRun) await get().loadRun(get().selectedRun.id)
  },
  async loadStrategyWorkspace() {
    set({ strategyLoading: true, strategyError: '' })
    try {
      const [strategiesPayload, proposalsPayload, pointerPayload] = await Promise.all([
        fetchJSON('/api/eval/strategies?limit=200'),
        fetchJSON('/api/eval/promotion-proposals?limit=200'),
        fetchJSON('/api/eval/strategies/current').catch(() => null),
      ])
      const strategies = strategiesPayload.strategies || strategiesPayload.items || []
      const proposals = proposalsPayload.proposals || proposalsPayload.items || []
      const pointer = pointerPayload?.pointer || pointerPayload || null
      const selectedId = get().selectedProposal?.id
      const selectedProposal = proposals.find((item: any) => item.id === selectedId) || proposals[0] || null
      set({ strategies, promotionProposals: proposals, strategyPointer: pointer, selectedProposal })
      return { strategies, proposals, pointer }
    } catch (error: any) {
      set({ strategyError: error.message || '离线策略审批数据加载失败' })
      throw error
    } finally {
      set({ strategyLoading: false })
    }
  },
  async loadProposal(id) {
    if (!id) return null
    set({ strategyError: '' })
    try {
      const payload = await fetchJSON(`/api/eval/promotion-proposals/${encodeURIComponent(id)}`)
      const selectedProposal = payload.proposal || payload
      set({ selectedProposal })
      return selectedProposal
    } catch (error: any) {
      set({ strategyError: error.message || '策略候选详情加载失败' })
      throw error
    }
  },
  async createStrategy(name, manifest) {
    set({ strategyActionLoading: true, strategyError: '', strategyMessage: '' })
    try {
      const payload = await fetchJSON('/api/eval/strategies', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, manifest }),
      })
      const strategy = payload.strategy || payload
      set({
        strategyMessage: strategy.created === false
          ? '相同内容的策略版本已存在，已返回原版本。'
          : '离线策略版本已创建；它尚未激活，也不会自动上线。',
      })
      await get().loadStrategyWorkspace()
      return strategy
    } catch (error: any) {
      set({ strategyError: error.message || '离线策略创建失败' })
      throw error
    } finally {
      set({ strategyActionLoading: false })
    }
  },
  async createPromotionProposal(baselineRunId, candidateRunId) {
    set({ strategyActionLoading: true, strategyError: '', strategyMessage: '' })
    try {
      const payload = await fetchJSON('/api/eval/promotion-proposals', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ baseline_run_id: baselineRunId, candidate_run_id: candidateRunId }),
      })
      const selectedProposal = payload.proposal || payload
      set({
        selectedProposal,
        strategyMessage: selectedProposal.status === 'blocked'
          ? '候选已生成，但统计或门禁证据不足，当前不可审批激活。'
          : '候选已生成，等待人工审阅；不会自动激活。',
      })
      await get().loadStrategyWorkspace()
      return selectedProposal
    } catch (error: any) {
      set({ strategyError: error.message || '策略候选创建失败' })
      throw error
    } finally {
      set({ strategyActionLoading: false })
    }
  },
  async decideProposal(id, decision, note = '') {
    set({ strategyActionLoading: true, strategyError: '', strategyMessage: '' })
    try {
      const payload = await fetchJSON(`/api/eval/promotion-proposals/${encodeURIComponent(id)}/decision`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision, note }),
      })
      const selectedProposal = payload.proposal || payload
      set({
        selectedProposal,
        strategyMessage: decision === 'approve'
          ? '候选已批准，但仍未激活；需要再次显式确认。'
          : '候选已拒绝，不会进入激活流程。',
      })
      await get().loadStrategyWorkspace()
      return selectedProposal
    } catch (error: any) {
      set({ strategyError: error.message || '审批决定提交失败' })
      throw error
    } finally {
      set({ strategyActionLoading: false })
    }
  },
  async activateProposal(id, note = '') {
    set({ strategyActionLoading: true, strategyError: '', strategyMessage: '' })
    try {
      const payload = await fetchJSON(`/api/eval/promotion-proposals/${encodeURIComponent(id)}/activate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ note }),
      })
      set({
        strategyMessage: payload.idempotent
          ? '该候选已经激活，本次请求未重复变更策略指针。'
          : '离线评测策略指针已显式激活；这不是线上流量发布。',
      })
      await get().loadStrategyWorkspace()
      return payload
    } catch (error: any) {
      set({ strategyError: error.message || '策略激活失败' })
      throw error
    } finally {
      set({ strategyActionLoading: false })
    }
  },
  async rollbackStrategy(reason = '') {
    const expectedCurrent = get().strategyPointer?.current_strategy_version_id || ''
    set({ strategyActionLoading: true, strategyError: '', strategyMessage: '' })
    try {
      const body: any = { idempotency_key: requestKey('strategy-rollback'), reason }
      if (expectedCurrent) body.expected_current_strategy_version_id = expectedCurrent
      const payload = await fetchJSON('/api/eval/strategies/rollback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      set({
        strategyMessage: payload.idempotent
          ? '该回滚请求已经处理，没有重复变更。'
          : '离线评测策略已人工回滚到上一版本。',
      })
      await get().loadStrategyWorkspace()
      return payload
    } catch (error: any) {
      set({ strategyError: error.message || '人工回滚失败' })
      throw error
    } finally {
      set({ strategyActionLoading: false })
    }
  },
  async loadEvolutionWorkspace() {
    set({ evolutionLoading: true, evolutionError: '' })
    try {
      const payload = await fetchJSON('/api/eval/evolution-suggestions?limit=200')
      const suggestions = payload.suggestions || payload.items || []
      const selectedId = get().selectedEvolutionSuggestion?.id
      const selected = suggestions.find((item: any) => item.id === selectedId) || suggestions[0] || null
      set({ evolutionSuggestions: suggestions, selectedEvolutionSuggestion: selected })
      await get().loadEvolutionAudit(selected?.id)
      return suggestions
    } catch (error: any) {
      set({ evolutionError: error.message || '受控策略建议加载失败' })
      throw error
    } finally {
      set({ evolutionLoading: false })
    }
  },
  async selectEvolutionSuggestion(id) {
    if (!id) {
      set({ selectedEvolutionSuggestion: null, evolutionAuditEvents: [] })
      return null
    }
    set({ evolutionError: '' })
    try {
      const payload = await fetchJSON(`/api/eval/evolution-suggestions/${encodeURIComponent(id)}`)
      const selected = payload.suggestion || payload
      set({ selectedEvolutionSuggestion: selected })
      await get().loadEvolutionAudit(id)
      return selected
    } catch (error: any) {
      set({ evolutionError: error.message || '受控策略建议详情加载失败' })
      throw error
    }
  },
  async loadEvolutionAudit(id) {
    if (!id) {
      set({ evolutionAuditEvents: [] })
      return []
    }
    const payload = await fetchJSON(`/api/eval/evolution-suggestions/audit-events?suggestion_id=${encodeURIComponent(id)}&limit=100`)
    const events = payload.events || []
    set({ evolutionAuditEvents: events })
    return events
  },
  async createEvolutionSuggestion(sourceRunId) {
    set({ evolutionActionLoading: true, evolutionError: '', evolutionMessage: '' })
    try {
      const payload = await fetchJSON('/api/eval/evolution-suggestions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source_run_id: sourceRunId, idempotency_key: requestKey('evolution-create') }),
      })
      const selected = payload.suggestion || payload
      set({
        selectedEvolutionSuggestion: selected,
        evolutionMessage: payload.created === false
          ? '同一运行和规则版本的建议已经存在，本次没有重复创建。'
          : '实验假设已生成，尚未接受、物化或应用，也不代表效果提升。',
      })
      await get().loadEvolutionWorkspace()
      return selected
    } catch (error: any) {
      set({ evolutionError: error.message || '实验假设生成失败' })
      throw error
    } finally {
      set({ evolutionActionLoading: false })
    }
  },
  async decideEvolutionSuggestion(id, decision, note = '') {
    const current = get().evolutionSuggestions.find(item => item.id === id) || get().selectedEvolutionSuggestion
    set({ evolutionActionLoading: true, evolutionError: '', evolutionMessage: '' })
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
      const selected = payload.suggestion || payload
      set({
        selectedEvolutionSuggestion: selected,
        evolutionMessage: decision === 'accept'
          ? '建议已由人工接受，但尚未物化为策略版本，更没有上线。'
          : '建议已被人工拒绝，不会进入后续流程。',
      })
      await get().loadEvolutionWorkspace()
      return selected
    } catch (error: any) {
      set({ evolutionError: error.message || '受控策略建议审批失败' })
      throw error
    } finally {
      set({ evolutionActionLoading: false })
    }
  },
  async materializeEvolutionSuggestion(id, name = '') {
    const current = get().evolutionSuggestions.find(item => item.id === id) || get().selectedEvolutionSuggestion
    set({ evolutionActionLoading: true, evolutionError: '', evolutionMessage: '' })
    try {
      const body: any = {
        expected_generation: Number(current?.generation || 0),
        idempotency_key: requestKey('evolution-materialize'),
      }
      if (String(name || '').trim()) body.name = String(name).trim()
      const payload = await fetchJSON(`/api/eval/evolution-suggestions/${encodeURIComponent(id)}/materialize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      set({
        selectedEvolutionSuggestion: payload.suggestion || get().selectedEvolutionSuggestion,
        evolutionMessage: '已显式物化为离线策略版本；仍未创建晋级候选、未激活、未部署。',
      })
      await Promise.all([get().loadEvolutionWorkspace(), get().loadStrategyWorkspace()])
      return payload
    } catch (error: any) {
      set({ evolutionError: error.message || '受控策略建议物化失败' })
      throw error
    } finally {
      set({ evolutionActionLoading: false })
    }
  },
  async downloadReport(format = 'markdown') {
    const selectedRun = get().selectedRun
    if (!selectedRun) return
    const response = await apiFetch(`/api/eval/runs/${selectedRun.id}/report?format=${format}`)
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `eval-${selectedRun.id}.${format === 'csv' ? 'csv' : 'md'}`
    link.click()
    URL.revokeObjectURL(url)
  },
  clearTrace() { set({ selectedTrace: null }) },
  clearStrategyMessage() { set({ strategyMessage: '' }) },
  reset() {
    set({
      datasets: [], runs: [], badcases: [], results: [],
      selectedRun: null, selectedTrace: null, comparison: null, comparisonRunIds: [],
      metrics: {}, readiness: null, readinessError: '', executionByRunId: {},
      activeOperation: null, operationMessage: '',
      strategies: [], strategyPointer: null, promotionProposals: [], selectedProposal: null,
      strategyLoading: false, strategyActionLoading: false, strategyError: '', strategyMessage: '',
      evolutionSuggestions: [], selectedEvolutionSuggestion: null, evolutionAuditEvents: [],
      evolutionLoading: false, evolutionActionLoading: false, evolutionError: '', evolutionMessage: '',
      error: '',
    })
  },
}))

// ── Pinia getters → 纯选择器（接受 state 快照，供组件渲染期计算） ──
export function selectLatestRun(state: EvaluationState): any {
  return state.selectedRun || state.runs[0] || null
}
export function selectSummary(state: EvaluationState): any {
  return selectLatestRun(state)?.summary || EMPTY
}
export function selectLiveReadiness(state: EvaluationState): any {
  return state.readiness?.live || EMPTY
}
export function selectLiveReady(state: EvaluationState): boolean {
  return selectLiveReadiness(state).ready === true
}
export function selectLiveReasons(state: EvaluationState): string[] {
  const reasons = selectLiveReadiness(state).reasons || selectLiveReadiness(state).blockers || []
  return messagesFrom(reasons)
}
export function selectLiveWarnings(state: EvaluationState): string[] {
  return messagesFrom(selectLiveReadiness(state).warnings)
}
export function selectSelectedExecution(state: EvaluationState): any {
  const run = state.selectedRun
  if (!run) return null
  return state.executionByRunId[run.id]
    || run.execution
    || run.metadata?.execution
    || run.config?.execution
    || null
}
export function selectCompletedRuns(state: EvaluationState): any[] {
  return state.runs.filter(run => run.status === 'completed')
}
export function selectCurrentStrategy(state: EvaluationState): any {
  return state.strategyPointer?.current_strategy
    || state.strategies.find(item => item.id === state.strategyPointer?.current_strategy_version_id)
    || null
}
export function selectPreviousStrategy(state: EvaluationState): any {
  return state.strategyPointer?.previous_strategy
    || state.strategies.find(item => item.id === state.strategyPointer?.previous_strategy_version_id)
    || null
}
