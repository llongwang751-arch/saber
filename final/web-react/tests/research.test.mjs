// 移植自 final/web/tests/research.test.js — 研究工具纯函数契约测试（逐字对齐）。
import test from 'node:test'
import assert from 'node:assert/strict'
import { citationTokens, isRunActive, isRunObservable, planReviewPayload, reportBlocks, reportInlineTokens, researchResult, safeSourceURL } from '../src/utils/research.ts'

test('review actions carry exact saved version; edits clone complete plan metadata', () => {
  const plan = { objective: '验证证据', constraints: ['只能使用官方来源'], steps: [{ id: 's1', title: '检索', kind: 'research', depends_on: [], tool_policy: ['web_search'] }] }
  assert.deepEqual(planReviewPayload('approve', 3), { action: 'approve', version: 3 })
  assert.deepEqual(planReviewPayload('reject', 3), { action: 'reject', version: 3 })
  const edit = planReviewPayload('edit', 3, plan)
  edit.plan.steps[0].title = '修改'
  assert.equal(plan.steps[0].title, '检索')
  assert.deepEqual(edit.plan.constraints, plan.constraints)
  assert.deepEqual(edit.plan.steps[0].tool_policy, ['web_search'])
  assert.throws(() => planReviewPayload('approve', 0), /版本/)
  assert.throws(() => planReviewPayload('edit', 3, { ...plan, steps: [] }), /至少/)
  assert.throws(() => planReviewPayload('edit', 3, { ...plan, steps: [{ id: 's1', title: '检索', depends_on: ['missing'] }] }), /依赖/)
})

test('only real sources form citation links and reference numbering maps to correct source', () => {
  const sources = [{ source_id: 'S4' }, { source_id: 'S9' }]
  const tokens = citationTokens('证据 [1](#source-1)，另一项 [S4]，未知 [77]。', sources, [{ number: 1, source_id: 'S9' }])
  assert.deepEqual(tokens.filter(token => token.type === 'citation').map(token => token.sourceId), ['S9', 'S4'])
  assert.ok(tokens.some(token => token.text.includes('[77]')))
})

test('untrusted report HTML and links remain plain text nodes', () => {
  const attack = '<img src=x onerror="alert(1)"> [click](javascript:alert(1))'
  assert.deepEqual(citationTokens(attack), [{ type: 'text', text: attack }])
  for (const value of ['javascript:alert(1)', 'data:text/html,foo', '//example.com', 'file:///etc/passwd', 'https://user:password@example.com']) assert.equal(safeSourceURL(value), '')
  assert.equal(safeSourceURL('https://example.com/research?q=1'), 'https://example.com/research?q=1')
})

test('review pause remains observable and cancellable without pretending a worker is active', () => {
  assert.equal(isRunObservable('awaiting_plan_review'), true)
  assert.equal(isRunActive('awaiting_plan_review'), false)
  assert.equal(isRunObservable('cancelling'), true)
  assert.equal(isRunObservable('interrupted'), false)
  assert.deepEqual(researchResult({ result: { response: { answer: 'report' } } }), { answer: 'report' })
})

test('reading view omits frontmatter and generated references, preserves semantic markdown safely', () => {
  const report = '---\ntopic: private metadata\n---\n# Report\n- **Claim** [1](#source-1)\n- `code`\n\n```html\n<img src=x>\n```\n\n## References\n<a id="source-1"></a> [1] reference'
  const blocks = reportBlocks(report)
  assert.deepEqual(blocks.map(block => block.type), ['heading', 'list', 'code'])
  assert.equal(blocks[1].items.length, 2)
  assert.equal(blocks[2].text, '<img src=x>')
  const inline = reportInlineTokens(blocks[1].items[0], [{ source_id: 'S1' }])
  assert.ok(inline.some(token => token.type === 'strong' && token.text === 'Claim'))
  assert.ok(inline.some(token => token.type === 'citation' && token.sourceId === 'S1'))
  assert.ok(!inline.some(token => token.text.includes('#source-')))
  assert.equal(reportInlineTokens('[x](javascript:alert)', [], [])[0].type, 'text')
})
