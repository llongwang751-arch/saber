// 新增：SSE 解析器语义补充测试（Last-Event-ID 持久化语义、默认事件名、多字段、停播语义）。
import test from 'node:test'
import assert from 'node:assert/strict'
import { createSSEParser } from '../src/lib/sse.ts'

test('default event name is "message" and retry persists across events', () => {
  const events = []
  const parser = createSSEParser((type, data, meta) => events.push([type, data, { id: meta.id, retry: meta.retry }]))
  assert.equal(parser.feed('retry: 2500\nid: 41\ndata: {"a":1}\n\n', true), true)
  assert.deepEqual(events, [['message', { a: 1 }, { id: '41', retry: 2500 }]])
})

test('id is sticky across events and Last-Event-ID semantics keep the last id on replay feed', () => {
  const events = []
  const parser = createSSEParser((type, _data, meta) => events.push([type, meta.id]))
  parser.feed('id: 100\nevent: token\ndata: 1\n\n', true)
  parser.feed('data: 2\n\n', true) // no new id → sticky (client would resend Last-Event-ID: 100)
  assert.deepEqual(events, [['token', '100'], ['message', '100']])
})

test('id containing NUL is ignored, non-numeric retry is ignored', () => {
  const events = []
  const parser = createSSEParser((type, _d, meta) => events.push({ type, id: meta.id, retry: meta.retry }))
  parser.feed('id: a\0b\nretry: fast\nid: good\ndata: {"x":1}\n\n', true)
  assert.equal(events.length, 1)
  assert.equal(events[0].type, 'message')
  assert.equal(events[0].id, 'good')
  assert.equal(events[0].retry, null)
})

test('CR-only newlines buffer an ambiguous trailing \\r until final flush', () => {
  const events = []
  const parser = createSSEParser((type, data) => events.push([type, data]))
  // \r-terminated lines; the last \r is ambiguous mid-stream → buffered, nothing dispatched yet
  assert.equal(parser.feed('event: t\rdata: {"x":\rdata: 1}\r', false), true)
  assert.deepEqual(events, [])
  // final flush completes the buffered line and dispatches multi-line JSON joined with \n
  parser.feed('', true)
  assert.deepEqual(events, [['t', { x: 1 }]])
})

test('malformed JSON data silently resets the event but keeps the stream alive', () => {
  const events = []
  const parser = createSSEParser((type, data) => events.push([type, data]))
  assert.equal(parser.feed('event: token\ndata: not-json\n\n', true), true)
  assert.deepEqual(events, [])
  assert.equal(parser.feed('event: token\ndata: {"ok":true}\n\n', true), true)
  assert.deepEqual(events, [['token', { ok: true }]])
})

test('callback returning false stops parsing mid-stream', () => {
  const events = []
  const parser = createSSEParser(type => { events.push(type); return false })
  assert.equal(parser.feed('event: a\ndata: 1\n\nevent: b\ndata: 2\n\n', true), false)
  assert.deepEqual(events, ['a'])
})
