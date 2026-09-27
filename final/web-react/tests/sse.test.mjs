// 移植自 final/web/tests/sse.test.js — SSE 解析器契约测试（逐字对齐）。
import test from 'node:test'
import assert from 'node:assert/strict'
import { createSSEParser, readSSE } from '../src/lib/sse.ts'

test('fragmented CRLF, multiline JSON, comments and replay ids', () => {
  const events = []
  const parser = createSSEParser((...args) => events.push(args))
  const wire = ': ping\r\nid: 9\r\nretry: 1000\r\nevent: token\r\ndata: {"content":\r\ndata: "中文"}\r\n\r\nevent: done\ndata:{"ok":true}\n\n'
  for (const char of wire) parser.feed(char)
  parser.feed('', true)
  assert.deepEqual(events, [
    ['token', { content: '中文' }, { id: '9', retry: 1000 }],
    ['done', { ok: true }, { id: '9', retry: 1000 }],
  ])
})

test('incomplete tail retains event type and never fabricates completion', () => {
  const events = []
  const parser = createSSEParser((type, data) => events.push([type, data]))
  parser.feed('event: token\ndata: {"content":"tail"}', true)
  assert.deepEqual(events, [['token', { content: 'tail' }]])
})

test('UTF-8 split across every byte decodes correctly and releases reader', async () => {
  const bytes = new TextEncoder().encode('event: token\ndata: {"content":"中文🙂"}\n\n')
  const response = new Response(new ReadableStream({
    start(controller) { for (const byte of bytes) controller.enqueue(Uint8Array.of(byte)); controller.close() },
  }))
  const events = []
  await readSSE(response, (type, data) => events.push([type, data]))
  assert.deepEqual(events, [['token', { content: '中文🙂' }]])
  assert.equal(response.body.locked, false)
})

test('consumer stop cancels the source and callback errors propagate', async () => {
  let cancelled = false
  const response = new Response(new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode('data: {"ok":true}\n\n')) },
    cancel() { cancelled = true },
  }))
  await readSSE(response, () => false)
  assert.equal(cancelled, true)
  await assert.rejects(readSSE(new Response('data: {"ok":true}\n\n'), () => { throw new Error('consumer') }), /consumer/)
})
