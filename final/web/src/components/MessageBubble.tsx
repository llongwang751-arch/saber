import { useMemo } from 'react'
import { renderMarkdown } from '../utils/markdown'
import type { ChatMessage } from '../stores/sessions'
import ThinkPanel from './ThinkPanel'

export default function MessageBubble({ msg }: { msg: ChatMessage }) {
  const rendered = useMemo(() => renderMarkdown(msg.answer || ''), [msg.answer])

  return (
    <div className={`msg-row ${msg.role}`}>
      <div className={`avatar ${msg.role}`}>{msg.role === 'user' ? '◇' : '◈'}</div>
      <div className="msg-body">
        <div className="msg-name">{msg.role === 'user' ? '你' : 'AGI-saber'}</div>

        {/* 用户消息：纯文本 */}
        {msg.role === 'user' && <div className="bubble user">{msg.text}</div>}

        {/* 兼容旧格式（存的是 html 串） */}
        {msg.role !== 'user' && msg.html && <div className="bubble ai" dangerouslySetInnerHTML={{ __html: msg.html }} />}

        {/* AI 消息：结构化渲染 */}
        {msg.role !== 'user' && !msg.html && (
          <div className="bubble ai">
            {msg.memory && (
              <div className="memory-note">
                <span className="memory-tag">🧠 记忆</span>
                <span className="memory-content">{msg.memory}</span>
              </div>
            )}

            <ThinkPanel msg={msg} />

            {msg.interrupted && <div className="interrupted-badge">🛑 已中断</div>}

            {!msg.answer && msg.streaming
              ? <div className="typing"><span></span><span></span><span></span></div>
              : <div className="answer-text" dangerouslySetInnerHTML={{ __html: rendered }} />}
          </div>
        )}
      </div>
    </div>
  )
}
