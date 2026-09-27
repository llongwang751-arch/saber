// utils/research.ts — 研究工作台纯函数工具。逐字移植自 Vue 版 src/utils/research.js。
// 计划审批载荷、来源 URL 白名单、引用 token 化（只产出文本节点，绝不产出 HTML）、
// 报告分块、运行事件标签。

export type ResearchEvent = { type: string; data?: any }

export const isRunActive = (status?: string) => ['pending', 'running', 'cancelling'].includes(status || '')
export const isRunObservable = (status?: string) => isRunActive(status) || status === 'awaiting_plan_review'

export function safeSourceURL(value: unknown): string {
  try {
    const url = new URL(String(value || ''))
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : ''
  } catch { return '' }
}

export function planReviewPayload(action: string, version: number, plan?: any): any {
  if (!['approve', 'edit', 'reject'].includes(action)) throw new Error('无效的计划操作')
  if (!Number.isInteger(version) || version < 1) throw new Error('计划版本无效，请刷新任务')
  const payload: any = { action, version }
  if (action === 'edit') {
    if (!String(plan?.objective || '').trim()) throw new Error('请填写研究目标')
    if (!Array.isArray(plan?.steps) || !plan.steps.length) throw new Error('计划至少需要一个步骤')
    const ids = new Set(plan.steps.map((step: any) => step.id))
    if (ids.size !== plan.steps.length) throw new Error('步骤标识不能重复')
    for (const step of plan.steps) {
      if (!step.id || !String(step.title || '').trim()) throw new Error('请填写每个步骤的标题')
      if ((step.depends_on || []).some((id: string) => !ids.has(id) || id === step.id)) throw new Error('步骤依赖无效')
    }
    payload.plan = JSON.parse(JSON.stringify(plan))
  }
  return payload
}

// Return text/citation nodes, never HTML. Unknown references remain visible as text.
export type InlineToken =
  | { type: 'text'; text: string }
  | { type: 'citation'; text: string; sourceId: string }

export function citationTokens(text: string, sources: any[] = [], references: any[] = []): InlineToken[] {
  const nodes: InlineToken[] = []
  const lookup = new Map<string, string>()
  sources.forEach((source, index) => {
    const id = String(source.source_id || `S${index + 1}`)
    lookup.set(id, id)
    lookup.set(String(index + 1), id)
  })
  references.forEach(reference => {
    const id = String(reference.source_id || '')
    if (lookup.has(id)) lookup.set(String(reference.number), id)
  })
  const pattern = /\[([A-Za-z0-9_-]+)\](?:\(#source-[A-Za-z0-9_-]+\))?/g
  let offset = 0
  for (const match of String(text).matchAll(pattern)) {
    if (!lookup.has(match[1])) continue
    if (match.index! > offset) nodes.push({ type: 'text', text: text.slice(offset, match.index) })
    nodes.push({ type: 'citation', text: `[${match[1]}]`, sourceId: lookup.get(match[1])! })
    offset = match.index! + match[0].length
  }
  if (offset < text.length) nodes.push({ type: 'text', text: text.slice(offset) })
  return nodes.length ? nodes : [{ type: 'text', text: String(text) }]
}

export function researchResult(run: any): any {
  return run?.result?.response || run?.result || {}
}

export type RichToken =
  | { type: 'text'; text: string }
  | { type: 'citation'; text: string; sourceId: string }
  | { type: 'code'; text: string }
  | { type: 'strong'; text: string }
  | { type: 'em'; text: string }
  | { type: 'link'; text: string; href: string }

export function reportInlineTokens(text: string, sources: any[], references: any[]): RichToken[] {
  return citationTokens(text, sources, references).flatMap(node => {
    if (node.type !== 'text') return [node] as RichToken[]
    const result: RichToken[] = []
    const pattern = /`([^`]+)`|\*\*([^*]+)\*\*|\*([^*]+)\*|\[([^\]]+)\]\(([^\s)]+)\)/g
    let offset = 0
    for (const match of node.text.matchAll(pattern)) {
      if (match.index! > offset) result.push({ type: 'text', text: node.text.slice(offset, match.index) })
      if (match[1]) result.push({ type: 'code', text: match[1] })
      else if (match[2]) result.push({ type: 'strong', text: match[2] })
      else if (match[3]) result.push({ type: 'em', text: match[3] })
      else {
        const href = safeSourceURL(match[5])
        result.push(href ? { type: 'link', text: match[4], href } : { type: 'text', text: match[0] })
      }
      offset = match.index! + match[0].length
    }
    if (offset < node.text.length) result.push({ type: 'text', text: node.text.slice(offset) })
    return result.length ? result : [node as RichToken]
  })
}

export type ReportBlock =
  | { type: 'rule' }
  | { type: 'code'; text: string }
  | { type: 'heading'; tag: string; text: string }
  | { type: 'list'; tag: 'ul' | 'ol'; items: string[] }
  | { type: 'quote'; text: string }
  | { type: 'paragraph'; text: string }

export function reportBlocks(markdown: string): ReportBlock[] {
  const content = String(markdown || '').replace(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/, '')
  const lines = content.split(/\r?\n/)
  const blocks: ReportBlock[] = []
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index]
    if (/^#{1,6}\s+(?:References|Sources|参考文献|参考来源|引用来源)\s*$/i.test(line)) break
    if (!line.trim()) continue
    if (/^```/.test(line)) {
      const code: string[] = []
      while (++index < lines.length && !/^```/.test(lines[index])) code.push(lines[index])
      blocks.push({ type: 'code', text: code.join('\n') })
      continue
    }
    if (/^\s*(?:---|\*\*\*|___)\s*$/.test(line)) { blocks.push({ type: 'rule' }); continue }
    const heading = line.match(/^(#{1,6})\s+(.*)$/)
    if (heading) { blocks.push({ type: 'heading', tag: `h${Math.min(heading[1].length + 3, 6)}`, text: heading[2] }); continue }
    const list = line.match(/^\s*(?:([-*+])|\d+\.)\s+(.*)$/)
    if (list) {
      const tag = list[1] ? 'ul' : 'ol'
      const previous = blocks.at(-1)
      if (previous?.type === 'list' && previous.tag === tag) previous.items.push(list[2])
      else blocks.push({ type: 'list', tag, items: [list[2]] })
      continue
    }
    const quote = line.match(/^>\s?(.*)$/)
    blocks.push({ type: quote ? 'quote' : 'paragraph', text: quote ? quote[1] : line.replace(/<a id=["']source-[\w-]+["']><\/a>\s*/g, '') })
  }
  return blocks
}

export function runEventLabel(event: ResearchEvent): string {
  const data = event.data || {}
  const labels: Record<string, string> = {
    queued: '任务已进入队列', started: '开始执行', cancel_requested: '已请求停止',
    plan_created: '研究计划已生成', plan_ready: '研究计划等待审核',
    plan_review_required: '研究计划等待审核', plan_edited: '计划已保存新版本',
    plan_approved: '计划已批准，继续研究', plan_rejected: '计划已拒绝',
    awaiting_plan_review: '研究计划等待审核', report_ready: '研究报告已生成',
    completed: '任务完成', failed: '任务失败', cancelled: '任务已停止',
  }
  if (labels[event.type]) return labels[event.type]
  if (event.type === 'route') return `选择执行路径：${data.mode || 'agent'}`
  if (event.type === 'node_start') return `开始步骤：${data.title || data.tool || data.id || '工具'}`
  if (event.type === 'node_done') return `步骤结束：${data.title || data.tool || data.id || '工具'}`
  if (event.type === 'tool_call') return `调用工具：${data.tool || '工具'}`
  if (event.type === 'research_round') return `研究步骤 ${data.step_id || ''} · 第 ${data.round || 1} 轮 · ${data.status || '收集证据'}`
  if (event.type === 'source_found') return `收录来源：${data.title || data.source_id || '来源'}`
  if (event.type === 'code_exec') return data.status === 'code_only' ? '代码已生成（未执行）' : `代码步骤：${data.status || '执行中'}`
  return event.type.replace(/_/g, ' ')
}
