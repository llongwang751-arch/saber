import { useEffect, useRef, useState } from 'react'
import { useEvaluation, selectCompletedRuns } from '../stores/evaluation'

function shortId(value: any) { return String(value || '').slice(0, 10) || '未知' }
function valueOrDash(value: any) { return value == null || value === '' ? '—' : String(value) }
function decimalOrDash(value: any) { const n = Number(value); return value == null || !Number.isFinite(n) ? '—' : n.toFixed(3) }
function statusText(value: any) {
  return ({ proposed: '等待人工审批', accepted: '已接受为假设', rejected: '已拒绝' } as Record<string, string>)[value] || '未知状态'
}
function auditAction(value: any) {
  return ({ evolution_create: '生成建议', evolution_review: '人工审批', evolution_materialize: '显式物化' } as Record<string, string>)[value] || value || '未知操作'
}
function timeText(value: any) {
  if (!value) return '时间未知'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN', { hour12: false })
}
function friendlyError(value: any) {
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

// 受控策略建议：从已完成评测生成受限 RAG 参数假设；人工接受 → 显式物化，全程代次校验 + 审计。
export default function EvolutionSuggestionPanel({ onClose }: { onClose: () => void }) {
  const evaluation = useEvaluation()
  const runs = useEvaluation(s => s.runs)
  const suggestions = useEvaluation(s => s.evolutionSuggestions)
  const auditEvents = useEvaluation(s => s.evolutionAuditEvents)
  const selected = useEvaluation(s => s.selectedEvolutionSuggestion)
  const closeButton = useRef<HTMLButtonElement>(null)
  const [sourceRunId, setSourceRunId] = useState('')
  const [reviewNote, setReviewNote] = useState('')
  const [strategyName, setStrategyName] = useState('')
  const busy = evaluation.evolutionLoading || evaluation.evolutionActionLoading
  const ragManifest = selected?.suggestion_manifest?.runtime_overrides?.rag || {}
  const manifestText = JSON.stringify(selected?.suggestion_manifest || {}, null, 2)
  const truthTitle = selected?.truth?.synthetic || selected?.truth?.replay
    ? '合成或回放证据'
    : selected?.truth?.evidence_complete !== true ? '证据不完整' : '离线证据假设'
  const truthTone = selected?.truth?.synthetic || selected?.truth?.replay
    ? 'truth-warning'
    : selected?.truth?.evidence_complete === true ? 'truth-neutral' : 'truth-danger'
  const truthDescription = selected?.truth?.replay
    ? '来源包含回放预置输出，不调用真实模型、工具或 RAG；不得当作真实智能体效果。'
    : selected?.truth?.synthetic
    ? '来源包含合成数据，只能用于验证链路与提出待验证假设。'
    : selected?.truth?.evidence_complete !== true
    ? '来源证据字段不完整，不能据此宣称离线或线上改进。'
    : '来源是离线评测证据；它仍不能证明候选已提升，更不能代替真实线上实验。'

  useEffect(() => {
    setReviewNote('')
    setStrategyName('')
  }, [selected?.id])

  useEffect(() => {
    ;(async () => {
      if (!useEvaluation.getState().runs.length) await useEvaluation.getState().refresh().catch(() => {})
      await useEvaluation.getState().loadEvolutionWorkspace().catch(() => {})
      setTimeout(() => closeButton.current?.focus(), 0)
    })()
  }, [])

  function runName(id: any) { return runs.find((item: any) => item.id === id)?.name || `运行 ${shortId(id)}` }
  function runLabel(run: any) { return `${run.name || shortId(run.id)} · ${shortId(run.id)}` }
  function evidenceCount(key: string) { return Array.isArray(selected?.evidence?.[key]) ? selected.evidence[key].length : 0 }

  async function createSuggestion() {
    if (!sourceRunId) return
    await evaluation.createEvolutionSuggestion(sourceRunId).catch(() => {})
  }
  async function decide(decision: 'accept' | 'reject') {
    if (!selected) return
    const label = decision === 'accept' ? '接受为待验证实验假设' : '拒绝该建议'
    if (!window.confirm(`确认${label}？\n\n该操作使用第 ${selected.generation} 代状态并写入不可变审计记录。`)) return
    await evaluation.decideEvolutionSuggestion(selected.id, decision, reviewNote).catch(() => {})
  }
  async function materialize() {
    if (!selected) return
    if (!window.confirm('确认显式物化？\n\n只会创建离线策略版本，不会创建晋级候选、激活策略或部署线上流量。')) return
    await evaluation.materializeEvolutionSuggestion(selected.id, strategyName).catch(() => {})
  }

  return (
    <aside
      className="evolution-panel"
      role="dialog"
      aria-modal="true"
      aria-labelledby="evolution-title"
      aria-busy={busy}
      onKeyDown={e => { if (e.key === 'Escape') onClose() }}
    >
      <header className="evolution-header">
        <div>
          <span>证据驱动 · 人工闭环</span>
          <h3 id="evolution-title">受控策略建议</h3>
          <p>从已完成评测的失败指标与失败用例生成受限的 RAG 参数实验假设。</p>
        </div>
        <button ref={closeButton} type="button" aria-label="关闭受控策略建议" onClick={onClose}>关闭</button>
      </header>

      <div className="evolution-scroll">
        <section className="evolution-truth" role="note" aria-label="受控策略建议真实性说明">
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

        {evaluation.evolutionError && (
          <div className="evolution-feedback error" role="alert">
            <strong>操作失败</strong><span>{friendlyError(evaluation.evolutionError)}</span>
          </div>
        )}
        {evaluation.evolutionMessage && (
          <div className="evolution-feedback success" role="status" aria-live="polite">
            {evaluation.evolutionMessage}
          </div>
        )}

        <section className="evolution-create" aria-labelledby="evolution-create-title">
          <div>
            <h4 id="evolution-create-title">从已完成运行生成实验假设</h4>
            <p>只有检索或无答案判断存在可操作信号时才会生成；其他问题不会被强行归因给 RAG 参数。</p>
          </div>
          <form onSubmit={e => { e.preventDefault(); createSuggestion() }}>
            <label htmlFor="evolution-source-run">证据运行
              <select id="evolution-source-run" value={sourceRunId} required disabled={busy} onChange={e => setSourceRunId(e.target.value)}>
                <option value="" disabled>选择一条已完成评测</option>
                {selectCompletedRuns(evaluation).map((run: any) => (
                  <option key={run.id} value={run.id}>{runLabel(run)}</option>
                ))}
              </select>
            </label>
            <button type="submit" disabled={busy || !sourceRunId}>
              {evaluation.evolutionActionLoading ? '正在锁定证据…' : '生成受控建议'}
            </button>
          </form>
        </section>

        {evaluation.evolutionLoading && (
          <div className="evolution-loading" role="status" aria-live="polite">
            <span aria-hidden="true"></span>正在核对建议、校验和与审计记录…
          </div>
        )}

        {!evaluation.evolutionLoading && (
          <section className="evolution-workspace" aria-labelledby="evolution-record-title">
            <div className="evolution-section-head">
              <div><h4 id="evolution-record-title">建议记录</h4><p>证据与策略清单创建后不可修改，状态变更使用代次校验。</p></div>
              <span>{suggestions.length} 条</span>
            </div>
            {!suggestions.length ? (
              <div className="evolution-empty">
                <strong>还没有受控建议</strong>
                <p>先选择包含 RAG 失败信号的已完成评测。合成回放可以生成假设，但会明确标记为非真实效果。</p>
              </div>
            ) : (
              <div className="evolution-grid">
                <nav className="evolution-list" aria-label="受控策略建议列表">
                  {suggestions.map((item: any) => (
                    <button
                      key={item.id}
                      type="button"
                      className={selected?.id === item.id ? 'active' : ''}
                      aria-pressed={selected?.id === item.id}
                      onClick={() => { evaluation.selectEvolutionSuggestion(item.id).catch(() => {}) }}
                    >
                      <span>{runName(item.source_run_id)}</span>
                      <small>第 {item.generation} 代 · {statusText(item.status)}</small>
                      <b className={`status-${item.status}`}>{statusText(item.status)}</b>
                    </button>
                  ))}
                </nav>

                {selected && (
                  <article className="evolution-detail">
                    <div className="detail-title">
                      <div><span>实验假设 {shortId(selected.id)}</span><h5>{statusText(selected.status)}</h5></div>
                      <b className={truthTone}>{truthTitle}</b>
                    </div>

                    <section className="claim-boundary" aria-label="结论边界">
                      <strong>当前只能说：已生成待验证的参数假设</strong>
                      <p>{truthDescription}</p>
                    </section>

                    <dl className="evolution-facts">
                      <div><dt>建议召回数量（top_k）</dt><dd>{valueOrDash(ragManifest.top_k)}</dd></div>
                      <div><dt>建议无答案阈值</dt><dd>{decimalOrDash(ragManifest.no_answer_threshold)}</dd></div>
                      <div><dt>规则版本</dt><dd>{selected.rule_version || '—'}</dd></div>
                      <div><dt>当前代次</dt><dd>{valueOrDash(selected.generation)}</dd></div>
                      <div><dt>证据完整性</dt><dd>{selected.truth?.evidence_complete === true ? '完整' : '不完整，仅作假设'}</dd></div>
                      <div><dt>自动应用</dt><dd>{selected.no_auto_apply === true ? '关闭' : '后端未声明'}</dd></div>
                    </dl>

                    <div className="evolution-columns">
                      <section>
                        <h6>为什么提出这个假设</h6>
                        <ul>{(selected.rationale || []).map((reason: string) => <li key={reason}>{reason}</li>)}</ul>
                      </section>
                      <section className="limitations">
                        <h6>限制与后续门禁</h6>
                        <ul>{(selected.limitations || []).map((item: string) => <li key={item}>{item}</li>)}</ul>
                      </section>
                    </div>

                    <details className="evidence-details">
                      <summary>查看证据与校验信息</summary>
                      <dl>
                        <div><dt>来源运行</dt><dd>{selected.source_run_id || '—'}</dd></div>
                        <div><dt>数据集版本</dt><dd>{selected.dataset_version_id || '—'}</dd></div>
                        <div><dt>证据校验和</dt><dd>{selected.evidence_checksum || '—'}</dd></div>
                        <div><dt>建议校验和</dt><dd>{selected.manifest_checksum || '—'}</dd></div>
                        <div><dt>失败结果 / 失败用例</dt><dd>{evidenceCount('case_results')} / {evidenceCount('badcases')}</dd></div>
                      </dl>
                      <pre aria-label="受控建议策略清单">{manifestText}</pre>
                    </details>

                    {selected.status === 'proposed' && (
                      <section className="review-box">
                        <div><strong>必须由另一位审批人决定</strong><p>创建者不能审批自己的建议；权限与身份由服务端校验。</p></div>
                        <label htmlFor="evolution-review-note">审批备注
                          <textarea
                            id="evolution-review-note"
                            value={reviewNote}
                            rows={3}
                            maxLength={1000}
                            placeholder="记录接受为实验假设或拒绝的理由"
                            disabled={busy}
                            onChange={e => setReviewNote(e.target.value.trim())}
                          />
                        </label>
                        <div>
                          <button className="secondary" type="button" disabled={busy} onClick={() => decide('reject')}>拒绝建议</button>
                          <button type="button" disabled={busy} onClick={() => decide('accept')}>接受为实验假设</button>
                        </div>
                      </section>
                    )}

                    {selected.status === 'accepted' && !selected.materialized_strategy_version_id && (
                      <section className="materialize-box">
                        <div><strong>接受不等于策略已创建</strong><p>显式物化只创建一个不可变离线策略版本，不会创建晋级候选、激活策略或部署线上流量。</p></div>
                        <label htmlFor="evolution-strategy-name">离线策略名称
                          <input
                            id="evolution-strategy-name"
                            value={strategyName}
                            maxLength={200}
                            placeholder="例如：RAG 参数实验候选 v1"
                            disabled={busy}
                            onChange={e => setStrategyName(e.target.value.trim())}
                          />
                        </label>
                        <button type="button" disabled={busy} onClick={materialize}>显式物化为离线策略版本</button>
                      </section>
                    )}

                    {selected.materialized_strategy_version_id && (
                      <section className="materialized-box" role="status">
                        <strong>已物化，但尚未证明有效</strong>
                        <p>离线策略版本：<code>{selected.materialized_strategy_version_id}</code></p>
                        <p>下一步必须建立同数据集完整候选运行，与基线成对比较并通过 P2/P3 门禁。</p>
                      </section>
                    )}

                    <section className="audit-box" aria-labelledby="evolution-audit-title">
                      <div><h6 id="evolution-audit-title">审计记录</h6><span>{auditEvents.length} 条</span></div>
                      {auditEvents.length > 0 ? (
                        <ol>
                          {auditEvents.map((event: any) => (
                            <li key={event.id}>
                              <b>{auditAction(event.action)}</b><span>{event.actor || '未知操作者'}</span><time>{timeText(event.created_at)}</time>
                            </li>
                          ))}
                        </ol>
                      ) : (
                        <p>尚无可显示的审计记录。</p>
                      )}
                    </section>
                  </article>
                )}
              </div>
            )}
          </section>
        )}
      </div>
    </aside>
  )
}
