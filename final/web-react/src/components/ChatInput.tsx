import { useRef, useState } from 'react'
import { useChat } from '../stores/chat'

export default function ChatInput() {
  const chat = useChat()
  const [text, setText] = useState('')
  const [error, setError] = useState('')
  const ta = useRef<HTMLTextAreaElement>(null)

  function resize() {
    const el = ta.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 140) + 'px'
  }
  function onKey(e: React.KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); onSend() }
  }
  async function onSend() {
    if (chat.loading) { chat.stop(); return }
    const msg = text.trim()
    if (!msg) return
    setText('')
    setTimeout(resize, 0)
    setError('')
    try { await chat.send(msg) }
    catch (e: any) { setText(msg); setError('发送失败：' + e.message) }
  }
  async function quick(m: string) { setText(m); onSend() }

  return (
    <div className="input-area">
      {error && <p role="alert">{error}</p>}
      <div className="input-row">
        <textarea
          className="input-box"
          value={text}
          ref={ta}
          rows={1}
          placeholder="输入消息，Enter 发送，Shift+Enter 换行…"
          onChange={e => { setText(e.target.value); resize() }}
          onKeyDown={onKey}
        />
        <button className={`send-btn${chat.loading ? ' stop' : ''}`} onClick={onSend}>{chat.loading ? '■' : '➤'}</button>
      </div>
      <div className="input-hint">
        <span className="hint-chip" onClick={() => quick('你是谁？')}>你是谁</span>
        <span className="hint-chip" onClick={() => quick('现在几点？')}>现在几点</span>
        <span className="hint-chip" onClick={() => quick('北京天气怎么样？')}>北京天气</span>
        <span className="hint-chip" onClick={() => quick('我喜欢周杰伦的音乐')}>记住偏好</span>
      </div>
    </div>
  )
}
