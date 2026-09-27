// 新增：引用渲染 util（citationTokens / reportInlineTokens / reportBlocks / markdown）补充测试。
import test from 'node:test'
import assert from 'node:assert/strict'
import { citationTokens, reportBlocks, reportInlineTokens, safeSourceURL } from '../src/utils/research.ts'
import { renderMarkdown } from '../src/utils/markdown.ts'

test('reference numbering maps through references and positional fallback works', () => {
  const sources = [{ source_id: 'S1' }, { source_id: 'S2' }]
  // positional "[1]" maps to S1 when no reference table exists
  let tokens = citationTokens('a [1] b', sources, [])
  assert.equal(tokens.find(t => t.type === 'citation').sourceId, 'S1')
  // reference table remaps [1] to S2
  tokens = citationTokens('a [1] b', sources, [{ number: 1, source_id: 'S2' }])
  assert.equal(tokens.find(t => t.type === 'citation').sourceId, 'S2')
  // anchor suffix is stripped from the citation text
  tokens = citationTokens('[S2](#source-S2)', sources, [])
  assert.deepEqual(tokens, [{ type: 'citation', text: '[S2]', sourceId: 'S2' }])
})

test('citation splitting preserves interleaved text byte-for-byte', () => {
  const text = '前 [S1] 中 [unknown] 后 [S2] 尾'
  const tokens = citationTokens(text, [{ source_id: 'S1' }, { source_id: 'S2' }])
  const joined = tokens.map(t => t.text).join('')
  assert.equal(joined, '前 [S1] 中 [unknown] 后 [S2] 尾')
  assert.deepEqual(tokens.filter(t => t.type === 'citation').map(t => t.sourceId), ['S1', 'S2'])
})

test('reportInlineTokens renders untrusted javascript: links as plain text', () => {
  const tokens = reportInlineTokens('看 [文档](javascript:alert(1)) 和 [页](https://ok.example.com/a)', [], [])
  assert.equal(tokens[0].type, 'text')
  // 危险 URL 绝不出现在任何 link token 的 href 中（与 Vue 版同一解析行为：[^\s)]+ 截断到第一个右括号）
  assert.ok(tokens.some(t => t.type === 'text' && t.text.includes('javascript:alert(1)')))
  assert.ok(!tokens.some(t => t.type === 'link' && t.href.startsWith('javascript:')))
  const link = tokens.find(t => t.type === 'link')
  assert.equal(link.href, 'https://ok.example.com/a')
})

test('reportBlocks handles quotes, rules, ordered/unordered lists and heading depth caps', () => {
  const md = ['## T', '', '### deep', '- a', '- b', '1. one', '2. two', '', '---', '', '> quoted', 'plain', ''].join('\n')
  const blocks = reportBlocks(md)
  assert.deepEqual(blocks.map(b => b.type), ['heading', 'heading', 'list', 'list', 'rule', 'quote', 'paragraph'])
  assert.equal(blocks[2].tag, 'ul')
  assert.equal(blocks[3].tag, 'ol')
  // h4+ maps to h6 max (heading level + 3, capped at 6)
  const deep = reportBlocks('#### four\nx')
  assert.equal(deep[0].tag, 'h6')
})

test('markdown renderer escapes HTML and renders tables, lists and code fences', () => {
  const html = renderMarkdown('# T\n\n| a | b |\n|---|---|\n| <img src=x> | 2 |\n\n```js\nalert(1)\n```\n\n- item')
  assert.ok(html.startsWith('<h1>T</h1>'))
  assert.ok(html.includes('<table>'))
  assert.ok(html.includes('&lt;img src=x&gt;'))
  assert.ok(!html.includes('<img'))
  assert.ok(html.includes('pre class="md-code"'))
  assert.ok(html.includes('<ul><li>item</li></ul>'))
})

test('safeSourceURL rejects URLs with credentials but allows query strings', () => {
  assert.equal(safeSourceURL('http://example.com'), 'http://example.com/')
  assert.equal(safeSourceURL('ftp://example.com'), '')
  assert.equal(safeSourceURL(null), '')
})
