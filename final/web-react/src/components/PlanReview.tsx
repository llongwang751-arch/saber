import { useEffect, useRef, useState } from 'react'
import { planReviewPayload } from '../utils/research'

// 计划审核：编辑目标/约束/步骤（增删、改标题/要求/验收）→ approve / edit / reject。
// 轮询同版本绝不丢弃用户未完成的编辑：仅当 version 变化时重置草稿（与 Vue 版 watch 一致）。
export default function PlanReview(props: {
  plan: any
  version: number
  status?: string
  reviewStatus?: string
  busy?: boolean
  onReview: (payload: any) => void
}) {
  const { plan, version, status, reviewStatus, busy } = props
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<any>({ steps: [] })
  const [constraints, setConstraints] = useState('')
  const [validationError, setValidationError] = useState('')

  const awaiting = status === 'awaiting_plan_review'
  const reviewLabel = ({ approved: '已批准', rejected: '已拒绝', pending: '待审核' } as Record<string, string>)[reviewStatus || ''] || '已保存'
  const kindLabel = (kind: string) => ({ research: '资料研究', code: '代码分析', write: '报告撰写' } as Record<string, string>)[kind] || kind

  function resetDraft() {
    const current = planRef.current
    setDraft(JSON.parse(JSON.stringify(current)))
    setConstraints((current.constraints || []).join('\n'))
    setEditing(false)
    setValidationError('')
  }

  // Polling the same version must never discard a user's unfinished edits.
  // 仅在 version 变化（或首次挂载）时重置草稿——与 Vue 版 watch(() => props.version) 一致。
  const planRef = useRef(props.plan)
  planRef.current = props.plan
  useEffect(() => { resetDraft() }, [version])

  useEffect(() => { if (!awaiting) setEditing(false) }, [awaiting])

  function addStep() {
    const used = new Set(draft.steps.map((step: any) => step.id))
    let number = draft.steps.length + 1
    while (used.has(`step-${number}`)) number++
    setDraft((d: any) => ({ ...d, steps: [...d.steps, { id: `step-${number}`, title: '', kind: 'research', guidance: '', tool_policy: [], depends_on: [], acceptance: '' }] }))
  }
  function removeStep(index: number) {
    setDraft((d: any) => {
      const removed = d.steps[index]
      const steps = d.steps
        .filter((_: any, i: number) => i !== index)
        .map((step: any) => ({ ...step, depends_on: (step.depends_on || []).filter((id: string) => id !== removed.id) }))
      return { ...d, steps }
    })
  }
  function review(action: 'approve' | 'edit' | 'reject') {
    if (busy) return
    setValidationError('')
    try {
      const payloadPlan = { ...JSON.parse(JSON.stringify(draft)), constraints: constraints.split('\n').map(item => item.trim()).filter(Boolean) }
      props.onReview(planReviewPayload(action, version, payloadPlan))
    } catch (error: any) {
      setValidationError(error.message)
    }
  }

  return (
    <section className="plan-review" aria-labelledby="plan-heading" aria-busy={!!busy}>
      <header className="section-heading">
        <div><span className="section-kicker">PLAN / V{version}</span><h3 id="plan-heading">{awaiting ? '审核研究计划' : '研究计划'}</h3></div>
        <span className="plan-state">{awaiting ? '等待你的决定' : reviewLabel}</span>
      </header>
      {awaiting && <p className="section-note">确认范围与步骤后开始研究。修改会保存为新版本，需再次批准。</p>}
      <fieldset disabled={!!busy} className="plan-fields">
        {editing ? (
          <>
            <label>研究目标<textarea value={draft.objective || ''} rows={2} maxLength={20000} onChange={e => setDraft((d: any) => ({ ...d, objective: e.target.value }))}></textarea></label>
            <label>约束条件（每行一项）<textarea value={constraints} rows={2} maxLength={10000} onChange={e => setConstraints(e.target.value)}></textarea></label>
          </>
        ) : (
          <p className="plan-objective">{plan.objective}</p>
        )}
        {!editing && plan.constraints?.length > 0 && (
          <ul className="plan-constraints">{plan.constraints.map((constraint: string, index: number) => <li key={index}>{constraint}</li>)}</ul>
        )}
        <ol className="plan-steps">
          {(editing ? draft.steps : plan.steps || []).map((step: any, index: number) => (
            <li key={step.id} className="plan-step">
              <div className="step-heading">
                <span className="step-number">{index + 1}</span><strong>{kindLabel(step.kind)}</strong><code>{step.id}</code>
                {editing && (
                  <button type="button" className="text-button" disabled={draft.steps.length < 2} aria-label={`删除步骤 ${index + 1}`} onClick={() => removeStep(index)}>删除</button>
                )}
              </div>
              {editing ? (
                <>
                  <label>步骤标题<input value={step.title || ''} maxLength={500} onChange={e => setDraft((d: any) => ({ ...d, steps: d.steps.map((s: any, i: number) => i === index ? { ...s, title: e.target.value } : s) }))} /></label>
                  <label>执行要求<textarea value={step.guidance || ''} rows={2} maxLength={10000} onChange={e => setDraft((d: any) => ({ ...d, steps: d.steps.map((s: any, i: number) => i === index ? { ...s, guidance: e.target.value } : s) }))}></textarea></label>
                  <label>验收条件<input value={step.acceptance || ''} maxLength={2000} onChange={e => setDraft((d: any) => ({ ...d, steps: d.steps.map((s: any, i: number) => i === index ? { ...s, acceptance: e.target.value } : s) }))} /></label>
                </>
              ) : (
                <>
                  <h4>{step.title}</h4>
                  {step.guidance && <p>{step.guidance}</p>}
                  {step.acceptance && <p className="acceptance">验收：{step.acceptance}</p>}
                </>
              )}
              {step.depends_on?.length > 0 && <small>依赖步骤：{step.depends_on.join('、')}</small>}
              {step.tool_policy?.length > 0 && <small>可用工具：{step.tool_policy.join('、')}</small>}
            </li>
          ))}
        </ol>
        {editing && <button type="button" className="research-button secondary" onClick={addStep}>添加研究步骤</button>}
        {validationError && <p className="research-error" role="alert">{validationError}</p>}
        {awaiting && (
          <div className="plan-actions">
            {editing ? (
              <>
                <button type="button" className="research-button secondary" onClick={resetDraft}>放弃修改</button>
                <button type="button" className="research-button primary" onClick={() => review('edit')}>保存新版本</button>
              </>
            ) : (
              <>
                <button type="button" className="research-button danger" onClick={() => review('reject')}>拒绝计划</button>
                <button type="button" className="research-button secondary" onClick={() => setEditing(true)}>修改计划</button>
                <button type="button" className="research-button primary" onClick={() => review('approve')}>{busy ? '提交中…' : '批准并开始研究'}</button>
              </>
            )}
          </div>
        )}
      </fieldset>
    </section>
  )
}
