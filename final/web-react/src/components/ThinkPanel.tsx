import { useState } from 'react'
import type { ChatMessage } from '../stores/sessions'

const LABELS: Record<string, string> = { react: '⚔ Saber · 拔剑中', tool: '工具调用', rag: '知识检索', chat: '对话' }
const CLASSES: Record<string, string> = { react: 'badge-react', tool: 'badge-tool', rag: 'badge-rag', chat: 'badge-react' }
const STEP: Record<string, [string, string]> = {
  Thought: ['step-thought', '💭 思考'],
  Action: ['step-action', '⚡ 动作'],
  Observation: ['step-observation', '🔍 观察'],
  'Final Answer': ['step-final', '✓ 汇总'],
}

function stepCls(t: string) { return (STEP[t] || ['step-thought'])[0] }
function stepLabel(t: string) { return (STEP[t] || [null as unknown as string, t])[1] }
function preview(r: any) {
  const raw = (r.chunk && r.chunk.content) || r.content || r.parent_content || ''
  const c = String(raw).slice(0, 120)
  return c + (c.length >= 120 ? '…' : '')
}
function similarity(r: any) {
  const value = Number(r?.similarity ?? r?.score ?? 0)
  return Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : '暂无'
}

export default function ThinkPanel({ msg }: { msg: ChatMessage }) {
  const [open, setOpen] = useState(true)

  const hasThink = !!(msg.steps && msg.steps.length) || !!msg.toolCall || !!(msg.ragResults && msg.ragResults.length) || !!msg.sandbox
  const badgeText = (msg.streaming && !msg.mode) ? '⚔ 出鞘…' : (LABELS[msg.mode || ''] || msg.mode || '⚔ Saber')
  const badgeClass = CLASSES[msg.mode || ''] || 'badge-react'
  const steps = msg.steps || []

  if (!hasThink) return null

  return (
    <div className="think-wrap">
      <div className={`think-header${open ? ' open' : ''}`} onClick={() => setOpen(v => !v)}>
        <span className="think-icon">▶</span>
        <span className="think-label">思考过程</span>
        <span className={`think-badge ${badgeClass}`}>{badgeText}</span>
      </div>
      <div className={`think-body${open ? ' open' : ''}`}>
        {msg.sandbox && (
          <div className="step-row">
            <span className="step-type step-action">📦 沙箱</span>
            <div className="step-content">工作目录已就绪：{msg.sandbox}（产物将生成于此，宿主机桌面可见）</div>
          </div>
        )}

        {steps.map((s, i) => (
          <div key={i} className="step-row">
            <span className={`step-type ${stepCls(s.type)}`}>{stepLabel(s.type)}</span>
            <div className="step-content">
              {s.content}
              {s.params && Object.keys(s.params).length ? <div className="step-params">{JSON.stringify(s.params)}</div> : null}
            </div>
          </div>
        ))}

        {msg.toolCall && (
          <div className="tool-call-block">
            <div className="tool-call-row"><span className="tool-call-label">工具</span><span className="tool-call-val">{msg.toolCall.tool_name}</span></div>
            <div className="tool-call-row"><span className="tool-call-label">参数</span><span className="tool-call-val" style={{ fontFamily: 'monospace' }}>{JSON.stringify(msg.toolCall.params || {})}</span></div>
            <div className="tool-call-row"><span className="tool-call-label">结果</span><span className="tool-call-val tool-call-result">{msg.toolCall.tool_result || ''}</span></div>
          </div>
        )}

        {msg.ragResults && msg.ragResults.length > 0 && (
          <div className="rag-sources">
            {msg.ragResults.slice(0, 3).map((r: any, i: number) => (
              <div key={i} className="rag-chunk">
                <span className="rag-chunk-score">相关度 {similarity(r)}</span>{preview(r)}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
