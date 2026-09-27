import { useEffect, useRef } from 'react'
import { useSessions, EMPTY_MESSAGES } from '../stores/sessions'
import MessageBubble from './MessageBubble'

export default function MessageList() {
  const sessionsArr = useSessions(s => s.sessions)
  const currentId = useSessions(s => s.currentId)
  const current = sessionsArr.find(x => x.id === currentId) ?? null
  const messages = current ? current.messages : EMPTY_MESSAGES
  const scroller = useRef<HTMLDivElement>(null)

  // 消息变化（含流式增量：每次事件都会产生新的 messages 数组引用）时滚到底
  useEffect(() => {
    if (scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight
  }, [messages])

  return (
    <div className="messages" ref={scroller}>
      {!messages.length && (
        <div className="welcome">
          <div className="welcome-icon">◈</div>
          <div className="welcome-title">AGI-saber 为你服务</div>
          <div className="welcome-desc">开启知识库可检索你上传的文件；选择工具后将自动推理并调用它们完成任务。</div>
        </div>
      )}
      {messages.map((m, i) => <MessageBubble key={i} msg={m} />)}
    </div>
  )
}
