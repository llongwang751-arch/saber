import { useEffect, useRef, useState } from 'react'
import { useAuth } from '../stores/auth'
import { useEvaluation, selectCompletedRuns, selectCurrentStrategy, selectLiveReadiness, selectLiveReady, selectLiveReasons, selectLiveWarnings, selectSelectedExecution, selectSummary, selectPreviousStrategy } from '../stores/evaluation'
import EvolutionSuggestionPanel from './EvolutionSuggestionPanel'

// 智能体质量评测工作台：KPI、评测记录、失败用例、执行轨迹、离线策略审批、受控策略建议。
// 逻辑与展示逐字移植自 Vue 版 EvaluationDashboard.vue / EvolutionSuggestionPanel.vue。

const caseNames: Record<string, string> = {
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

const statusNames: Record<string, string> = {
  completed: '已完成', passed: '已通过', failed: '未通过', error: '出错',
  running: '运行中', cancelled: '已取消', pending: '等待中',
  open: '待处理', triaged: '已分诊', resolved: '已解决', verified: '已复验',
  ok: '正常', success: '成功', timeout: '超时', proposed: '待审批', blocked: '已阻断',
  approved: '已批准 / 未激活', rejected: '已拒绝', activated: '已激活', inconclusive: '证据不足',
}

const eventTypeNames: Record<string, string> = {
  user_message: '用户消息', intent_predicted: '识别用户意图', slot_extracted: '提取关键信息',
  retrieval: '检索知识库', llm_call: '调用语言模型', tool_call: '调用工具',
  tool_result: '工具返回结果', guardrail: '安全规则检查', final_response: '生成最终回复',
  fallback: '执行兜底方案', error: '发生错误', route: '选择处理路径', memory: '读取记忆',
  rag_result: '知识库检索结果', done: '处理完成', event: '处理事件', plan: '生成任务计划',
  planning: '生成任务计划', validation: '验证执行结果', memory_read: '读取记忆',
  memory_write: '写入记忆', retry: '重试任务', clarification: '请求补充信息',
}

const commonNames: Record<string, string> = {
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

const categoryNames: Record<string, string> = {
  INFRA: '基础设施', TOOL: '工具调用', SAFETY: '安全问题', RESPONSE: '回复质量',
  RAG: '知识库检索', INTENT: '意图识别', FALLBACK: '兜底处理', TRACE: '执行轨迹',
  SLOT_OR_STATE: '信息与记忆', TOOL_ARGUMENT: '工具参数', GENERATION: '回复生成',
  RETRIEVAL: '知识检索', TOOL_RUNTIME: '工具运行',
  UNCLASSIFIED: '尚未分类',
}

const adapterNames: Record<string, string> = {
  replay: '合成回放适配器', 'replay-agent': '合成回放适配器',
  local: '本地智能体适配器', 'local-agent': '本地智能体适配器',
  http: 'HTTP 智能体适配器', 'http-agent': 'HTTP 智能体适配器',
}

const executionModeNames: Record<string, string> = {
  'preauthored-output-replay': '读取预置输出与轨迹', replay: '读取预置输出与轨迹',
  'synthetic-replay': '读取预置输出与轨迹', synthetic_replay: '读取预置输出与轨迹',
  'live-agent': '调用本地智能体主链', live_agent: '调用本地智能体主链',
  live: '调用本地智能体主链', local: '调用本地智能体主链',
}

const dataPolicyNames: Record<string, string> = {
  'synthetic-only': '仅合成、脱敏数据', synthetic: '仅合成、脱敏数据',
  'synthetic-evaluation-inputs': '合成输入 / 真实执行',
  'versioned-evaluation-dataset': '版本化评测数据', 'versioned-local-dataset': '版本化本地评测数据',
  desensitized: '脱敏业务数据', 'desensitized-real': '脱敏真实业务数据',
}

const truthNames: Record<string, string> = {
  synthetic: '合成回放（非真实调用）', replay: '合成回放（非真实调用）',
  real: '真实模型链路', live: '真实模型链路',
  fallback: '降级链路', degraded: '降级链路', mock: '模拟模型链路（Mock）', unknown: '后端未声明',
}

function percent(value: any) { return value == null ? '0%' : `${(Number(value) * 100).toFixed(1)}%` }
function score(value: any) { return value == null ? '暂无' : Number(value).toFixed(3) }
function shortId(value: any) { return String(value || '').slice(0, 8) || '未知' }
function signed(value: any) { const n = Number(value || 0); return `${n > 0 ? '+' : ''}${(n * 100).toFixed(1)}%` }
function statusClass(value: any) { return `status status-${String(value || '').toLowerCase()}` }
function severityClass(value: any) { return `severity severity-${String(value || 'medium').toLowerCase()}` }
function caseName(value: any) { return caseNames[value] || value || '未命名用例' }
function runName(value: any) { return commonNames[value] || value || '' }
function statusText(value: any) { return statusNames[String(value || '').toLowerCase()] || value || '未知状态' }
function severityText(value: any) { return ({ S0: '严重', S1: '高危', S2: '中等', S3: '较低' } as Record<string, string>)[value] || value || '未定级' }
function categoryText(value: any) { return categoryNames[String(value || 'UNCLASSIFIED').toUpperCase()] || value || '尚未分类' }
function eventTypeText(value: any) { return eventTypeNames[String(value || '').toLowerCase()] || value || '处理事件' }
function firstMeaningful(...values: any[]) { return values.find(value => value !== undefined && value !== null && value !== '') || '' }
function objectLabel(value: any) {
  if (!value || typeof value !== 'object') return value
  return firstMeaningful(value.type, value.name, value.adapter, value.mode)
}
function adapterText(value: any) {
  const key = String(value || '').toLowerCase()
  return adapterNames[key] || value || '未声明'
}
function executionModeText(value: any, adapter: any) {
  const key = String(value || '').toLowerCase()
  if (executionModeNames[key]) return executionModeNames[key]
  const adapterKey = String(adapter || '').toLowerCase()
  if (adapterKey.includes('replay')) return executionModeNames.replay
  if (adapterKey.includes('local')) return executionModeNames.local
  return value || '未声明'
}
function dataPolicyText(value: any) {
  const key = String(value || '').toLowerCase()
  return dataPolicyNames[key] || value || '未声明'
}
function truthText(value: any) {
  const key = String(value || '').toLowerCase()
  return truthNames[key] || value || '后端未声明'
}
function truthTone(value: any) {
  const key = String(value || '').toLowerCase()
  if (['real', 'live'].includes(key)) return 'real'
  if (['mock', 'fallback', 'degraded'].includes(key)) return 'warning'
  if (['synthetic', 'replay'].includes(key)) return 'synthetic'
  return 'unknown'
}
function runModeText(run: any) {
  const adapter = String(run?.config?.adapter?.type || run?.summary?.adapter?.name || '').toLowerCase()
  if (adapter.includes('replay')) return '合成回放'
  if (adapter.includes('local')) return '本地实测'
  if (adapter.includes('http')) return '真实 HTTP'
  return '模式待确认'
}
function eventDetailText(value: any) {
  const key = String(value ?? '')
  return commonNames[key] || statusNames[key.toLowerCase()] || key || '正常'
}
function hasPayload(value: any) {
  if (value == null) return false
  if (Array.isArray(value)) return value.length > 0
  if (typeof value === 'object') return Object.keys(value).length > 0
  return String(value).length > 0
}
function failedMetrics(item: any) {
  const value = item.failed_metrics || item.resolution?.failed_metrics || []
  return value.length ? value.map((name: string) => commonNames[name] || categoryNames[String(name).toUpperCase()] || name).join(' / ') : '等待分析原因'
}
function formatFieldValue(value: any): string {
  if (value == null) return '无'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (Array.isArray(value)) {
    if (!value.length) return '无'
    return value.map((item: any) => formatFieldValue(item)).join('；')
  }
  if (typeof value === 'object') {
    const entries = Object.entries(value).filter(([, item]) => hasPayload(item))
    if (!entries.length) return '无'
    return entries.map(([key, item]) => `${commonNames[key] || key}：${formatFieldValue(item)}`).join('；')
  }
  const key = String(value)
  return String(commonNames[key] || statusNames[key.toLowerCase()] || value)
}
function traceFields(event: any) {
  const payload = event?.payload
  if (!hasPayload(payload) || typeof payload !== 'object') return []
  if (Array.isArray(payload)) return [{ key: 'data', label: '数据', value: formatFieldValue(payload) }]
  return Object.entries(payload)
    .filter(([, value]) => hasPayload(value))
    .map(([key, value]) => ({ key, label: commonNames[key] || key, value: formatFieldValue(value) }))
}
function durationText(value: any) {
  const duration = Number(value)
  if (!Number.isFinite(duration)) return ''
  return duration >= 1000 ? `${(duration / 1000).toFixed(2)} 秒` : `${Math.round(duration)} 毫秒`
}
function shortChecksum(value: any) {
  const checksum = String(value || '')
  return checksum ? `${checksum.slice(0, 12)}…` : '未提供'
}
function numberOrDash(value: any, digits = 3) {
  const number = Number(value)
  return Number.isFinite(number) ? number.toFixed(digits) : '—'
}
function confidenceInterval(value: any) {
  if (!value || typeof value !== 'object') return '—'
  const low = numberOrDash(value.ci_low, 4)
  const high = numberOrDash(value.ci_high, 4)
  return low === '—' && high === '—' ? '—' : `[${low}, ${high}]`
}
function statisticsConclusion(value: any) {
  if (!value || typeof value !== 'object') return '尚无统计结论'
  const status = String(value.status || '').toLowerCase()
  if (value.passed === true || status === 'passed') return '候选显著优于基线'
  if (status === 'inconclusive') return '统计证据不足'
  if (status === 'blocked' || value.passed === false) return '未证明候选有稳定增益'
  return statusText(status)
}
function evidenceConclusion(value: any) {
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
function uniqueReasons(values: any[]) {
  const normalised = values.flatMap(value => {
    if (!value) return []
    if (typeof value === 'string') return [value]
    if (typeof value === 'object') return [value.message || value.reason || value.detail || value.code || '未说明的阻断项']
    return [String(value)]
  }).map(value => strategyReasonText(String(value).trim())).filter(Boolean)
  return [...new Set(normalised)]
}
function strategyReasonText(value: string) {
  const exact: Record<string, string> = {
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
function friendlyEvaluationError(value: any) {
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

export default function EvaluationDashboard({ onClose }: { onClose: () => void }) {
  const evaluation = useEvaluation()
  const auth = useAuth()
  const summary = selectSummary(evaluation)
  const gateText = summary.release_gate_passed === true ? '通过' : summary.release_gate_passed === false ? '阻断' : '待评测'
  const gateClass = summary.release_gate_passed === true ? 'pass' : summary.release_gate_passed === false ? 'fail' : ''
  const isBusy = evaluation.bootstrapping || evaluation.runningLive || evaluation.strategyActionLoading
  const operationCompleted = Number(evaluation.activeOperation?.completed || 0)
  const operationTotal = Number(evaluation.activeOperation?.total || 0)
  const operationPercent = operationTotal > 0
    ? Math.min(100, Math.round(operationCompleted / operationTotal * 100))
    : 0
  const replayButtonText = !evaluation.bootstrapping
    ? '运行合成回放'
    : operationTotal
      ? `正在回放 ${operationCompleted}/${operationTotal} 次执行`
      : '正在准备合成回放'
  const liveButtonText = evaluation.runningLive
    ? (operationTotal
        ? `本地实测 ${operationCompleted}/${operationTotal} 条用例`
        : '正在准备本地实测')
    : evaluation.readinessLoading
      ? '正在检查本地实测'
      : selectLiveReadiness(evaluation)?.llm?.mode === 'mock'
        ? '运行本地智能体实测（模拟模型）'
        : '运行本地智能体实测'
  const readinessMessages = selectLiveReady(evaluation)
    ? selectLiveWarnings(evaluation)
    : selectLiveReasons(evaluation)
  const liveReadinessTitle = evaluation.readinessLoading
    ? '正在检查本地实测条件'
    : !selectLiveReady(evaluation)
      ? '本地智能体实测暂不可用'
      : selectLiveReadiness(evaluation)?.llm?.mode === 'mock'
        ? '本地智能体可运行，当前使用模拟模型'
        : '本地智能体实测已就绪'
  const liveReadinessClass = evaluation.readinessLoading
    ? 'checking'
    : selectLiveReady(evaluation)
      ? (selectLiveReadiness(evaluation)?.llm?.mode === 'mock' ? 'limited' : 'ready')
      : 'blocked'
  const operationStatusText = !isBusy
    ? evaluation.operationMessage
    : (() => {
        const action = evaluation.activeOperation?.kind === 'live' ? '本地智能体实测' : '合成回放'
        if (!operationTotal) return `${action}正在准备测试数据和执行环境。`
        return `${action}正在运行：已完成 ${operationCompleted}/${operationTotal} 次执行。`
      })()
  const runProgressText = (() => {
    const completed = Number(summary.completed || 0)
    const total = Number(summary.total || 0)
    if (evaluation.selectedRun?.status === 'running') return `${completed} / ${total} 条已完成`
    return `${Number(summary.passed || 0)} / ${total} 条通过`
  })()
  const selectedRunFacts = (() => {
    const run = evaluation.selectedRun || {}
    const execution = selectSelectedExecution(evaluation) || {}
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
  })()
  const selectedProposal = evaluation.selectedProposal || null
  const proposalBlockers = (() => {
    const proposal = selectedProposal || {}
    return uniqueReasons([
      ...(proposal.blocked_reasons || []),
      ...(proposal.statistics?.reasons || []),
      ...(proposal.release_gate?.reasons || []),
      ...(proposal.safety?.reasons || []),
    ])
  })()
  const canDecideProposal = selectedProposal?.status === 'proposed'
  const canApproveProposal = selectedProposal?.status === 'proposed'
    && selectedProposal?.ready_for_review !== false
    && proposalBlockers.length === 0

  const [strategyPanelOpen, setStrategyPanelOpen] = useState(false)
  const [evolutionPanelOpen, setEvolutionPanelOpen] = useState(false)
  const strategyTrigger = useRef<HTMLButtonElement>(null)
  const evolutionTrigger = useRef<HTMLButtonElement>(null)
  const strategyClose = useRef<HTMLButtonElement>(null)
  const [strategyForm, setStrategyForm] = useState({
    name: '',
    manifest: '{\n  "prompt_version": "candidate-v1"\n}',
  })
  const [proposalForm, setProposalForm] = useState({ baselineRunId: '', candidateRunId: '' })
  const [strategyFormError, setStrategyFormError] = useState('')
  const [proposalFormError, setProposalFormError] = useState('')
  const [reviewNote, setReviewNote] = useState('')
  const [activationNote, setActivationNote] = useState('')
  const [rollbackReason, setRollbackReason] = useState('')

  const completedRuns = selectCompletedRuns(evaluation)
  const currentStrategy = selectCurrentStrategy(evaluation)
  const previousStrategy = selectPreviousStrategy(evaluation)
  const selectedProposalStrategy = strategyById(selectedProposal?.candidate_strategy_version_id)

  function strategyById(id: any) {
    if (!id) return null
    return evaluation.strategies.find(item => item.id === id)
      || (selectCurrentStrategy(evaluation)?.id === id ? selectCurrentStrategy(evaluation) : null)
      || (selectPreviousStrategy(evaluation)?.id === id ? selectPreviousStrategy(evaluation) : null)
  }
  function strategyName(strategy: any, fallback = '未命名策略') {
    if (!strategy) return fallback
    const version = strategy.version ? `v${strategy.version} · ` : ''
    return `${version}${strategy.name || shortId(strategy.id)}`
  }
  function runOption(run: any) {
    const strategy = strategyById(run.strategy_version_id)
    const strategyLabel = run.strategy_version_id
      ? strategyName(strategy, `策略 ${shortId(run.strategy_version_id)}`)
      : '未绑定策略'
    return `${runName(run.name) || shortId(run.id)} · ${strategyLabel}`
  }
  function proposalStatusText(value: any) {
    return statusNames[String(value || '').toLowerCase()] || value || '未知状态'
  }

  async function openStrategyPanel() {
    evaluation.clearTrace()
    setStrategyPanelOpen(true)
    evaluation.clearStrategyMessage()
    setTimeout(() => strategyClose.current?.focus(), 0)
    await evaluation.loadStrategyWorkspace().catch(() => {})
    const completed = selectCompletedRuns(useEvaluation.getState())
    const candidate = completed.find(run => run.strategy_version_id)
    if (!proposalForm.candidateRunId && candidate) setProposalForm(f => ({ ...f, candidateRunId: candidate.id }))
    if (!proposalForm.baselineRunId) {
      const baseline = completed.find(run => run.id !== proposalForm.candidateRunId
        && (!candidate || run.dataset_version_id === candidate.dataset_version_id))
        || completed.find(run => run.id !== proposalForm.candidateRunId)
      if (baseline) setProposalForm(f => ({ ...f, baselineRunId: baseline.id }))
    }
  }
  async function closeStrategyPanel() {
    setStrategyPanelOpen(false)
    setTimeout(() => strategyTrigger.current?.focus(), 0)
  }
  async function openEvolutionPanel() {
    evaluation.clearTrace()
    setStrategyPanelOpen(false)
    setEvolutionPanelOpen(true)
  }
  async function closeEvolutionPanel() {
    setEvolutionPanelOpen(false)
    setTimeout(() => evolutionTrigger.current?.focus(), 0)
  }
  async function createStrategy() {
    setStrategyFormError('')
    let manifest: any
    try {
      manifest = JSON.parse(strategyForm.manifest)
    } catch {
      setStrategyFormError('策略清单不是有效 JSON，请检查引号、逗号和括号。')
      return
    }
    if (!manifest || Array.isArray(manifest) || typeof manifest !== 'object' || !Object.keys(manifest).length) {
      setStrategyFormError('策略清单必须是至少包含一个字段的 JSON 对象。')
      return
    }
    await evaluation.createStrategy(strategyForm.name.trim(), manifest).catch(() => {})
  }
  async function createProposal() {
    setProposalFormError('')
    if (proposalForm.baselineRunId === proposalForm.candidateRunId) {
      setProposalFormError('基线运行和候选运行不能相同。')
      return
    }
    const candidate = selectCompletedRuns(useEvaluation.getState()).find(run => run.id === proposalForm.candidateRunId)
    if (!candidate?.strategy_version_id) {
      setProposalFormError('候选运行未绑定策略版本，不能建立可审计候选。')
      return
    }
    await evaluation.createPromotionProposal(
      proposalForm.baselineRunId,
      proposalForm.candidateRunId,
    ).catch(() => {})
  }
  async function decideProposal(decision: 'approve' | 'reject') {
    const proposal = selectedProposal
    if (!proposal) return
    const action = decision === 'approve' ? '批准' : '拒绝'
    if (!window.confirm(`确认${action}这个离线策略候选？该决定会写入审计记录，但不会自动上线。`)) return
    await evaluation.decideProposal(proposal.id, decision, reviewNote.trim()).catch(() => {})
    setReviewNote('')
  }
  async function activateProposal() {
    const proposal = selectedProposal
    if (!proposal) return
    const strategy = selectedProposalStrategy
    const confirmed = window.confirm(
      `确认显式激活“${strategyName(strategy)}”？\n\n这只会修改离线评测策略指针，不会发布线上流量。`,
    )
    if (!confirmed) return
    await evaluation.activateProposal(proposal.id, activationNote.trim()).catch(() => {})
    setActivationNote('')
  }
  async function rollbackStrategy() {
    if (!currentStrategy || !previousStrategy || !rollbackReason) return
    const confirmed = window.confirm(
      `危险操作：确认从“${strategyName(currentStrategy)}”回滚到“${strategyName(previousStrategy)}”？\n\n回滚原因：${rollbackReason}`,
    )
    if (!confirmed) return
    await evaluation.rollbackStrategy(rollbackReason).catch(() => {})
    setRollbackReason('')
  }
  function closeTopLayer() {
    if (evaluation.selectedTrace) evaluation.clearTrace()
    else if (evolutionPanelOpen) closeEvolutionPanel()
    else if (strategyPanelOpen) closeStrategyPanel()
    else onClose()
  }

  useEffect(() => { evaluation.refresh() }, [])

  return (
    <div className="eval-backdrop" onClick={e => { if (e.target === e.currentTarget) onClose() }}>
      <section
        className="eval-workbench"
        role="dialog"
        aria-modal="true"
        aria-label="智能体质量评测工作台"
        aria-busy={isBusy}
        onKeyDown={e => { if (e.key === 'Escape') closeTopLayer() }}
      >
        <header className="eval-head">
          <div>
            <p className="eval-kicker">智能体质量评测</p>
            <h2>智能体质量评测工作台</h2>
            <p>集中管理测试用例、自动执行、执行轨迹、失败用例与发布检查</p>
          </div>
          <div className="eval-actions" role="group" aria-label="评测操作">
            <button className="eval-btn secondary" type="button" disabled={isBusy || evaluation.loading} onClick={() => evaluation.refresh()}>刷新状态</button>
            <button className="eval-btn replay" type="button" disabled={isBusy} aria-describedby="replay-truth-note" onClick={() => evaluation.bootstrapDemo()}>
              {replayButtonText}
            </button>
            <button className="eval-btn primary" type="button" disabled={isBusy || !selectLiveReady(evaluation)} aria-describedby="live-readiness-reason" onClick={() => evaluation.runLiveDemo()}>
              {liveButtonText}
            </button>
            <button ref={strategyTrigger} className="eval-btn strategy" type="button" disabled={isBusy} onClick={openStrategyPanel}>离线策略审批</button>
            <button ref={evolutionTrigger} className="eval-btn evolution" type="button" disabled={isBusy} onClick={openEvolutionPanel}>受控策略建议</button>
            <button className="eval-close" type="button" aria-label="关闭" onClick={onClose}>×</button>
          </div>
        </header>

        <div className="offline-workspace">
          <div className="eval-mode-summary">
            <section id="replay-truth-note" className="truth-note" aria-label="合成回放说明">
              <span className="mode-mark">回放</span>
              <div>
                <strong>合成回放只验证评测平台，不代表真实智能体效果</strong>
                <p>它读取测试集中预置的输出和轨迹，<b>不会调用真实模型、工具或 RAG</b>；即使通过率为 100%，也不能当作线上效果。</p>
              </div>
            </section>
            <section id="live-readiness-reason" className={`readiness-card ${liveReadinessClass}`} role="status" aria-live="polite">
              <div className="readiness-title">
                <span className="readiness-dot" aria-hidden="true"></span>
                <strong>{liveReadinessTitle}</strong>
              </div>
              {evaluation.readinessLoading && <p>正在检查本地智能体和运行依赖。</p>}
              {!evaluation.readinessLoading && readinessMessages.length > 0 && (
                <ul>
                  {readinessMessages.slice(0, 3).map(reason => <li key={reason}>{reason}</li>)}
                </ul>
              )}
              {!evaluation.readinessLoading && !readinessMessages.length && (
                <p>{selectLiveReady(evaluation) ? '可以运行真实模型、工具与检索链路评测。' : '后端尚未声明真实评测已就绪。'}</p>
              )}
            </section>
          </div>

          {evaluation.error && (
            <div className="eval-error" role="alert">
              <strong>操作失败</strong><span>{friendlyEvaluationError(evaluation.error)}</span>
            </div>
          )}

          {(isBusy || evaluation.operationMessage) && (
            <div className="operation-status" aria-live="polite" aria-atomic="true">
              <span>{operationStatusText}</span>
              {isBusy && operationTotal > 0 && (
                <div className="operation-progress" role="progressbar" aria-valuenow={operationCompleted} aria-valuemin={0} aria-valuemax={operationTotal}>
                  <i style={{ width: `${operationPercent}%` }}></i>
                </div>
              )}
            </div>
          )}

          <div className="eval-kpis" aria-label="评测概览">
            <div className="eval-kpi"><span>评测集</span><strong>{evaluation.datasets.length}</strong><small>版本化数据资产</small></div>
            <div className="eval-kpi"><span>通过率</span><strong>{percent(summary.pass_rate)}</strong><small>{runProgressText}</small></div>
            <div className="eval-kpi"><span>失败用例</span><strong>{evaluation.badcases.length}</strong><small>当前运行中等待处理</small></div>
            <div className={`eval-kpi gate ${gateClass}`}><span>发布检查</span><strong>{gateText}</strong><small>严重与高危问题优先阻止发布</small></div>
          </div>

          {evaluation.loading && !evaluation.runs.length ? (
            <div className="eval-skeleton" aria-label="正在加载">
              {Array.from({ length: 7 }, (_, n) => <span key={n}></span>)}
            </div>
          ) : (
            <div className="eval-body">
              <section className="eval-main">
                <div className="panel-head">
                  <div><h3>评测记录</h3><p>选择一条记录，查看测试结果与执行轨迹</p></div>
                  {evaluation.selectedRun && (
                    <div className="report-actions">
                      <button type="button" onClick={() => evaluation.downloadReport('markdown')}>导出报告</button>
                      <button type="button" onClick={() => evaluation.downloadReport('csv')}>导出表格</button>
                    </div>
                  )}
                </div>
                {!evaluation.runs.length && (
                  <div className="eval-empty">
                    <strong>还没有评测运行</strong>
                    <span>可先运行合成回放验证评测闭环；本地智能体就绪后，再运行真实链路评测。</span>
                  </div>
                )}
                {evaluation.runs.length > 0 && (
                  <div className="run-strip">
                    {evaluation.runs.slice(0, 20).map(run => (
                      <button
                        key={run.id}
                        type="button"
                        className={`run-chip${evaluation.selectedRun?.id === run.id ? ' active' : ''}`}
                        aria-pressed={evaluation.selectedRun?.id === run.id}
                        onClick={() => evaluation.loadRun(run.id)}
                      >
                        <span>{runName(run.name) || shortId(run.id)}</span><small>{runModeText(run)}</small>
                        <b className={statusClass(run.status)}>{statusText(run.status)}</b>
                      </button>
                    ))}
                  </div>
                )}

                {evaluation.selectedRun && (
                  <dl className="run-facts" aria-label="当前评测运行的执行口径">
                    {selectedRunFacts.map(fact => (
                      <div key={fact.label}>
                        <dt>{fact.label}</dt>
                        <dd className={fact.tone ? `fact-${fact.tone}` : ''}>{fact.value}</dd>
                      </div>
                    ))}
                  </dl>
                )}

                {evaluation.comparison && (
                  <div className="compare-line">
                    <span>修复 {evaluation.comparison.fixed?.length || 0} 条</span>
                    <span>回归 {evaluation.comparison.regressions?.length || 0} 条</span>
                    <span>净变化 {signed(evaluation.comparison.pass_rate_delta)}</span>
                  </div>
                )}

                {evaluation.results.length > 0 && (
                  <div className="result-table">
                    <div className="result-row result-head"><span>用例</span><span>场景</span><span>分数</span><span>状态</span><span>链路</span></div>
                    {evaluation.results.map(item => (
                      <button
                        key={item.id}
                        type="button"
                        className="result-row"
                        aria-label={`查看${caseName(item.case_id)}的执行轨迹`}
                        onClick={() => evaluation.openTrace(item.id)}
                      >
                        <span className="case-id">{caseName(item.case_id)}</span>
                        <span>{item.scenario || item.payload?.scenario || '智能体场景'}</span>
                        <span>{score(item.metrics?.overall_score)}</span>
                        <span><b className={statusClass(item.status)}>{statusText(item.status)}</b></span>
                        <span className="trace-link">查看轨迹</span>
                      </button>
                    ))}
                  </div>
                )}
              </section>

              <aside className="eval-aside">
                <div className="panel-head"><div><h3>失败用例处理</h3><p>分类、定级、分析原因并重新验证</p></div></div>
                {!evaluation.badcases.length && (
                  <div className="eval-empty compact"><strong>暂无失败用例</strong><span>当前运行没有发现失败用例。</span></div>
                )}
                {evaluation.badcases.length > 0 && (
                  <div className="badcase-list">
                    {evaluation.badcases.slice(0, 30).map(item => (
                      <article key={item.id} className="badcase-item">
                        <div className="badcase-top"><b>{caseName(item.case_id || shortId(item.eval_case_id))}</b><span className={severityClass(item.severity)}>{severityText(item.severity)}</span></div>
                        <p>{categoryText(item.category)} · {statusText(item.status)}</p>
                        <small>{failedMetrics(item)}</small>
                        {item.status === 'open'
                          ? <button type="button" onClick={() => evaluation.triage(item, auth.username)}>标记已分诊</button>
                          : <span className="owner">负责人：{item.owner || '未分配'}</span>}
                      </article>
                    ))}
                  </div>
                )}
              </aside>
            </div>
          )}

          {strategyPanelOpen && (
            <aside className="strategy-drawer" role="dialog" aria-modal="true" aria-labelledby="strategy-panel-title">
              <header className="strategy-head">
                <div><span>离线策略审查</span><h3 id="strategy-panel-title">离线策略候选审批</h3><p>用成对离线运行和安全门禁约束策略变更。</p></div>
                <button ref={strategyClose} type="button" aria-label="关闭离线策略审批" onClick={closeStrategyPanel}>×</button>
              </header>

              <div className="strategy-scroll">
                <section className="strategy-truth" role="note">
                  <strong>仅离线评测，不代表线上 A/B；不会自动上线</strong>
                  <p>策略版本来源固定为 <code>offline_eval</code>。审批只记录离线证据，批准后仍需人工显式激活；系统不会分配线上流量。</p>
                </section>

                {evaluation.strategyError && (
                  <div className="strategy-feedback error" role="alert">
                    <strong>操作失败</strong><span>{friendlyEvaluationError(evaluation.strategyError)}</span>
                  </div>
                )}
                {evaluation.strategyMessage && (
                  <div className="strategy-feedback success" role="status" aria-live="polite">
                    {evaluation.strategyMessage}
                  </div>
                )}

                {evaluation.strategyLoading && (
                  <div className="strategy-loading" aria-live="polite">
                    <span aria-hidden="true"></span>正在加载离线策略与审批记录…
                  </div>
                )}

                {!evaluation.strategyLoading && (
                  <>
                    <section className="strategy-section" aria-labelledby="strategy-pointer-title">
                      <div className="strategy-section-head">
                        <div><h4 id="strategy-pointer-title">当前策略指针</h4><p>仅影响离线评测默认策略，不代表生产发布。</p></div>
                        <span>第 {evaluation.strategyPointer?.generation || 0} 代</span>
                      </div>
                      <div className="strategy-pointer-grid">
                        <article className="strategy-version-card current">
                          <span>当前策略</span>
                          <strong>{strategyName(currentStrategy, '尚未激活')}</strong>
                          {currentStrategy
                            ? <small title={currentStrategy.manifest_checksum}>校验和 {shortChecksum(currentStrategy.manifest_checksum)}</small>
                            : <small>审批和显式激活后才会建立指针</small>}
                        </article>
                        <article className="strategy-version-card previous">
                          <span>上一策略</span>
                          <strong>{strategyName(previousStrategy, '暂无可回滚版本')}</strong>
                          {previousStrategy
                            ? <small title={previousStrategy.manifest_checksum}>校验和 {shortChecksum(previousStrategy.manifest_checksum)}</small>
                            : <small>首次激活前没有上一版本</small>}
                        </article>
                      </div>
                      <div className="rollback-box">
                        <div><strong>人工回滚</strong><p>危险操作：会把当前离线策略指针切回上一版本，请先填写原因并再次确认。</p></div>
                        <label htmlFor="strategy-rollback-reason">回滚原因
                          <input
                            id="strategy-rollback-reason"
                            value={rollbackReason}
                            type="text"
                            maxLength={500}
                            placeholder="例如：候选策略出现回归"
                            disabled={evaluation.strategyActionLoading}
                            onChange={e => setRollbackReason(e.target.value)}
                          />
                        </label>
                        <button className="strategy-danger" type="button" disabled={evaluation.strategyActionLoading || !previousStrategy || !rollbackReason} onClick={rollbackStrategy}>确认人工回滚</button>
                      </div>
                    </section>

                    <div className="strategy-form-grid">
                      <section className="strategy-section" aria-labelledby="strategy-create-title">
                        <div className="strategy-section-head"><div><h4 id="strategy-create-title">创建不可变策略版本</h4><p>填写名称和最小策略清单（JSON）；内容相同会复用已有版本。</p></div></div>
                        <form className="strategy-form" onSubmit={e => { e.preventDefault(); createStrategy() }}>
                          <label htmlFor="strategy-name">策略名称
                            <input
                              id="strategy-name"
                              value={strategyForm.name}
                              maxLength={200}
                              required
                              placeholder="例如：RAG 召回候选 v2"
                              disabled={evaluation.strategyActionLoading}
                              onChange={e => setStrategyForm(f => ({ ...f, name: e.target.value }))}
                            />
                          </label>
                          <label htmlFor="strategy-manifest">策略清单（JSON）
                            <textarea
                              id="strategy-manifest"
                              value={strategyForm.manifest}
                              rows={5}
                              required
                              spellCheck={false}
                              aria-describedby="strategy-manifest-help"
                              disabled={evaluation.strategyActionLoading}
                              onChange={e => setStrategyForm(f => ({ ...f, manifest: e.target.value }))}
                            />
                          </label>
                          <small id="strategy-manifest-help">必须是非空 JSON 对象；创建后内容和校验和不可修改。</small>
                          {strategyFormError && <p className="strategy-inline-error" role="alert">{strategyFormError}</p>}
                          <button className="strategy-primary" type="submit" disabled={evaluation.strategyActionLoading || !strategyForm.name || !strategyForm.manifest.trim()}>
                            {evaluation.strategyActionLoading ? '正在处理…' : '创建离线策略'}
                          </button>
                        </form>
                      </section>

                      <section className="strategy-section" aria-labelledby="proposal-create-title">
                        <div className="strategy-section-head"><div><h4 id="proposal-create-title">生成晋级候选</h4><p>只能比较两个已完成运行，候选运行必须绑定策略版本。</p></div></div>
                        <form className="strategy-form" onSubmit={e => { e.preventDefault(); createProposal() }}>
                          <label htmlFor="baseline-run">基线运行
                            <select id="baseline-run" value={proposalForm.baselineRunId} required disabled={evaluation.strategyActionLoading} onChange={e => setProposalForm(f => ({ ...f, baselineRunId: e.target.value }))}>
                              <option value="" disabled>选择已完成的基线</option>
                              {completedRuns.map(run => <option key={run.id} value={run.id}>{runOption(run)}</option>)}
                            </select>
                          </label>
                          <label htmlFor="candidate-run">候选运行
                            <select id="candidate-run" value={proposalForm.candidateRunId} required disabled={evaluation.strategyActionLoading} onChange={e => setProposalForm(f => ({ ...f, candidateRunId: e.target.value }))}>
                              <option value="" disabled>选择已完成的候选</option>
                              {completedRuns.map(run => <option key={run.id} value={run.id}>{runOption(run)}</option>)}
                            </select>
                          </label>
                          {!completedRuns.length && <p className="strategy-empty-inline">暂无已完成运行，请先完成评测。</p>}
                          {proposalFormError && <p className="strategy-inline-error" role="alert">{proposalFormError}</p>}
                          <button className="strategy-primary" type="submit" disabled={evaluation.strategyActionLoading || !proposalForm.baselineRunId || !proposalForm.candidateRunId}>
                            {evaluation.strategyActionLoading ? '正在计算证据…' : '创建离线候选'}
                          </button>
                        </form>
                      </section>
                    </div>

                    <section className="strategy-section proposal-workspace" aria-labelledby="proposal-list-title">
                      <div className="strategy-section-head">
                        <div><h4 id="proposal-list-title">候选审批记录</h4><p>统计证据不足、发布门禁或安全检查失败时会阻断。</p></div>
                        <span>{evaluation.promotionProposals.length} 条</span>
                      </div>
                      {!evaluation.promotionProposals.length && (
                        <div className="strategy-empty">
                          <strong>暂无策略候选</strong><span>先创建策略版本，并用绑定该版本的候选运行与基线运行进行比较。</span>
                        </div>
                      )}
                      {evaluation.promotionProposals.length > 0 && (
                        <div className="proposal-grid">
                          <nav className="proposal-list" aria-label="策略候选列表">
                            {evaluation.promotionProposals.map(proposal => (
                              <button
                                key={proposal.id}
                                type="button"
                                className={evaluation.selectedProposal?.id === proposal.id ? 'active' : ''}
                                aria-pressed={evaluation.selectedProposal?.id === proposal.id}
                                onClick={() => evaluation.loadProposal(proposal.id)}
                              >
                                <span>{strategyName(strategyById(proposal.candidate_strategy_version_id), '策略候选')}</span>
                                <small>{shortId(proposal.id)} · {proposalStatusText(proposal.status)}</small>
                              </button>
                            ))}
                          </nav>

                          {selectedProposal && (
                            <article className="proposal-detail">
                              <div className="proposal-title">
                                <div><span>候选 {shortId(selectedProposal.id)}</span><h5>{strategyName(selectedProposalStrategy, '未命名策略')}</h5></div>
                                <b className={`proposal-status status-${selectedProposal.status}`}>{proposalStatusText(selectedProposal.status)}</b>
                              </div>
                              <dl className="proposal-evidence">
                                <div><dt>统计结论</dt><dd>{statisticsConclusion(selectedProposal.statistics)}</dd></div>
                                <div><dt>McNemar 检验 p 值</dt><dd>{numberOrDash(selectedProposal.statistics?.mcnemar?.p_value, 4)}</dd></div>
                                <div><dt>Bootstrap 置信区间</dt><dd>{confidenceInterval(selectedProposal.statistics?.paired_bootstrap)}</dd></div>
                                <div><dt>通过率变化</dt><dd>{selectedProposal.comparison?.pass_rate_delta == null ? '—' : signed(selectedProposal.comparison.pass_rate_delta)}</dd></div>
                                <div><dt>修复 / 回归</dt><dd>{selectedProposal.comparison?.fixed?.length ?? '—'} / {selectedProposal.comparison?.regressions?.length ?? '—'}</dd></div>
                                <div><dt>审阅就绪</dt><dd>{selectedProposal.ready_for_review === true ? '是' : '否'}</dd></div>
                                <div><dt>发布门禁</dt><dd>{evidenceConclusion(selectedProposal.release_gate)}</dd></div>
                                <div><dt>安全检查</dt><dd>{evidenceConclusion(selectedProposal.safety)}</dd></div>
                                <div><dt>自动上线</dt><dd>{selectedProposal.auto_activate === false ? '关闭（必须显式激活）' : '后端未声明'}</dd></div>
                              </dl>
                              {proposalBlockers.length > 0 && (
                                <div className="proposal-blockers">
                                  <strong>阻断原因</strong>
                                  <ul>{proposalBlockers.map(reason => <li key={reason}>{reason}</li>)}</ul>
                                </div>
                              )}
                              {proposalBlockers.length === 0 && <p className="proposal-ready">未发现阻断原因，仍需人工复核统计结果和门禁。</p>}

                              <label className="proposal-note" htmlFor="proposal-review-note">审批备注
                                <textarea
                                  id="proposal-review-note"
                                  value={reviewNote}
                                  rows={3}
                                  maxLength={1000}
                                  placeholder="记录判断依据或拒绝原因"
                                  disabled={evaluation.strategyActionLoading}
                                  onChange={e => setReviewNote(e.target.value)}
                                />
                              </label>
                              {canDecideProposal && (
                                <div className="proposal-actions">
                                  <button className="strategy-secondary" type="button" disabled={evaluation.strategyActionLoading} onClick={() => decideProposal('reject')}>拒绝候选</button>
                                  <button className="strategy-primary" type="button" disabled={evaluation.strategyActionLoading || !canApproveProposal} onClick={() => decideProposal('approve')}>批准候选</button>
                                </div>
                              )}
                              {selectedProposal.status === 'approved' && (
                                <div className="activation-box">
                                  <strong>批准不等于激活</strong>
                                  <p>显式激活只更新离线评测策略指针，不会触发线上发布。</p>
                                  <label htmlFor="activation-note">激活备注
                                    <input
                                      id="activation-note"
                                      value={activationNote}
                                      maxLength={1000}
                                      placeholder="可选：记录本次激活依据"
                                      disabled={evaluation.strategyActionLoading}
                                      onChange={e => setActivationNote(e.target.value)}
                                    />
                                  </label>
                                  <button className="strategy-activate" type="button" disabled={evaluation.strategyActionLoading} onClick={activateProposal}>显式激活离线策略</button>
                                </div>
                              )}
                              {selectedProposal.review_note && (
                                <div className="proposal-audit">
                                  <strong>最近审批记录</strong><p>{selectedProposal.review_note}</p>
                                </div>
                              )}
                            </article>
                          )}
                        </div>
                      )}
                    </section>
                  </>
                )}
              </div>
            </aside>
          )}

          {evolutionPanelOpen && <EvolutionSuggestionPanel onClose={closeEvolutionPanel} />}

          {evaluation.selectedTrace && (
            <aside className="trace-drawer" role="dialog" aria-modal="true" aria-label="执行轨迹">
              <header>
                <div><span>执行轨迹</span><h3>{caseName(evaluation.selectedTrace.case_id)}</h3></div>
                <button type="button" aria-label="关闭执行轨迹" onClick={() => evaluation.clearTrace()}>×</button>
              </header>
              {!evaluation.selectedTrace.trace?.length && (
                <div className="eval-empty compact"><span>该用例没有执行轨迹。</span></div>
              )}
              {evaluation.selectedTrace.trace?.length > 0 && (
                <ol aria-label="按执行顺序排列的轨迹事件">
                  {evaluation.selectedTrace.trace.map((event: any, index: number) => {
                    const fields = traceFields(event)
                    return (
                      <li key={event.id || `${event.sequence}-${index}`}>
                        <span className="trace-seq">{String(event.sequence ?? index + 1).padStart(2, '0')}</span>
                        <article className="trace-card">
                          <div className="trace-card-head">
                            <b>{eventTypeText(event.event_type || event.type)}</b>
                            {event.duration_ms != null && <span>{durationText(event.duration_ms)}</span>}
                          </div>
                          {(event.name || event.status) && <p>{eventDetailText(event.name || event.status)}</p>}
                          {fields.length > 0 && (
                            <dl className="trace-fields">
                              {fields.map(field => (
                                <div key={field.key}>
                                  <dt>{field.label}</dt><dd>{field.value}</dd>
                                </div>
                              ))}
                            </dl>
                          )}
                          {fields.length === 0 && <p className="trace-no-data">此步骤没有附加数据。</p>}
                        </article>
                      </li>
                    )
                  })}
                </ol>
              )}
            </aside>
          )}
        </div>
      </section>
    </div>
  )
}
