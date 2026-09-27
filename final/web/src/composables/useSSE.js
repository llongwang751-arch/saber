// Shared streaming decoder for chat and durable run observation.
export function createSSEParser(onEvent) {
  let buffer = '', event = '', id = '', retry = null, data = [], stopped = false
  function dispatch() {
    if (data.length) {
      let parsed
      try { parsed = JSON.parse(data.join('\n')) } catch { data = []; event = ''; return }
      if (onEvent(event || 'message', parsed, { id, retry }) === false) stopped = true
    }
    data = []
    event = ''
  }
  function line(value) {
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
    feed(text, final = false) {
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

export async function readSSE(resp, onEvent) {
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
