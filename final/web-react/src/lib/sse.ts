// lib/sse.ts — 共享流式解码器（chat 与持久 run 观测共用）。
// 逐字移植自 Vue 版 src/composables/useSSE.js：id/retry/Last-Event-ID 语义、
// 事件名分发、CR/CRLF 混合换行、多行 data、注释行、final 冲洗行为全部保持一致。
// 框架无关：不依赖 Vue/React，可被任何层调用，node --test 直接可测。

export interface SSEMetadata {
  id: string
  retry: number | null
}

export interface SSEParser {
  feed(text: string, final?: boolean): boolean
}

export function createSSEParser(onEvent: (event: string, data: any, metadata: SSEMetadata) => boolean | void): SSEParser {
  let buffer = '', event = '', id = '', retry: number | null = null, data: string[] = [], stopped = false
  function dispatch(): void {
    if (data.length) {
      let parsed: any
      try { parsed = JSON.parse(data.join('\n')) } catch { data = []; event = ''; return }
      if (onEvent(event || 'message', parsed, { id, retry }) === false) stopped = true
    }
    data = []
    event = ''
  }
  function line(value: string): void {
    if (!value) { dispatch(); return }
    if (value.startsWith(':')) return
    const colon = value.indexOf(':')
    const field = colon < 0 ? value : value.slice(0, colon)
    let payload = colon < 0 ? '' : value.slice(colon + 1)
    if (payload.startsWith(' ')) payload = payload.slice(1)
    if (field === 'data') data.push(payload)
    else if (field === 'event') event = payload
    else if (field === 'id' && !payload.includes('\0')) id = payload
    else if (field === 'retry' && /^\d+$/.test(payload)) retry = Number(payload)
  }
  return {
    feed(text: string, final = false): boolean {
      buffer += text
      while (!stopped) {
        const index = buffer.search(/[\r\n]/)
        if (index < 0) break
        if (!final && buffer[index] === '\r' && index === buffer.length - 1) break
        const width = buffer[index] === '\r' && buffer[index + 1] === '\n' ? 2 : 1
        const value = buffer.slice(0, index)
        buffer = buffer.slice(index + width)
        line(value)
      }
      if (final && !stopped) {
        if (buffer) line(buffer)
        buffer = ''
        dispatch()
      }
      return !stopped
    },
  }
}

export async function readSSE(resp: Response, onEvent: (event: string, data: any, metadata: SSEMetadata) => boolean | void): Promise<void> {
  if (!resp.body) throw new Error('Response has no readable stream')
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  const parser = createSSEParser(onEvent)
  let finished = false
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) { parser.feed(decoder.decode(), true); finished = true; break }
      if (!parser.feed(decoder.decode(value, { stream: true }))) break
    }
  } finally {
    if (!finished) await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}
