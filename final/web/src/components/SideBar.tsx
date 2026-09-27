import { useRef, useState } from 'react'
import { useDocs } from '../stores/docs'
import { useSessions } from '../stores/sessions'
import { useAuth, selectLoggedIn } from '../stores/auth'
import { useChat } from '../stores/chat'

// 侧边栏：个人文件上传、本地文档库、近期对话、新建对话、用户栏。
// 由 App 传入 id / className / onKeyDown 以保留 Vue 版的挂载点行为（移动端抽屉 + Esc 关闭）。
export default function SideBar(props: {
  id?: string
  className?: string
  onKeyDown?: (e: React.KeyboardEvent) => void
}) {
  const docs = useDocs()
  const sessions = useSessions()
  const auth = useAuth()
  const loggedIn = useAuth(selectLoggedIn)
  const sessionsList = useSessions(s => s.sessions)
  const currentId = useSessions(s => s.currentId)
  const [dragging, setDragging] = useState(false)
  const [sessionError, setSessionError] = useState('')

  const fileInputRef = useRef<HTMLInputElement>(null)

  async function onPick(e: React.ChangeEvent<HTMLInputElement>) {
    for (const f of e.target.files || []) await docs.uploadFile(f)
    e.target.value = ''
  }
  async function onDrop(e: React.DragEvent) {
    setDragging(false)
    for (const f of e.dataTransfer.files) await docs.uploadFile(f)
  }
  function docTitle(d: { name: string; pages?: number; textChars?: number; parser?: string }) {
    return `${d.name}${d.pages ? ' · ' + d.pages + ' 页' : ''}${d.textChars ? ' · ' + d.textChars + ' 字' : ''}${d.parser ? ' · ' + d.parser : ''}`
  }
  async function removeDoc(d: { name: string; docHash: string }) {
    if (confirm('确定删除「' + d.name + '」？')) await docs.deleteDoc(d.docHash)
  }
  async function removeLibraryDoc(d: { title?: string; id: string }) {
    if (confirm('确定删除本地文档「' + (d.title || '未命名文档') + '」？')) await docs.deleteLibraryDoc(d.id)
  }
  function fmtDate(ts: number) { return new Date(ts).toLocaleDateString('zh', { month: 'short', day: 'numeric' }) }
  function switchTo(id: string) { useChat.getState().abortInflight(); sessions.switchSession(id) }
  function del(id: string) { sessions.deleteSession(id) }
  async function newChat() {
    useChat.getState().stop()
    setSessionError('')
    try { await useSessions.getState().newSession() }
    catch (e: any) { setSessionError('创建会话失败：' + e.message) }
  }
  function logout() { useChat.getState().abortInflight(); auth.logout() }

  return (
    <aside className={`sidebar${props.className ? ' ' + props.className : ''}`} id={props.id} onKeyDown={props.onKeyDown}>
      <div className="sidebar-logo">
        <div className="logo-mark">◈</div>
        <div>
          <div className="logo-name">AGI-saber</div>
          <div className="logo-sub">智能协作 · 一站直达</div>
        </div>
      </div>

      {/* 个人文件 */}
      <div className="sec-label">个人文件</div>
      <div className="blackhole">
        <label
          htmlFor="personalFileInput"
          className={`upload-zone${dragging ? ' drag' : ''}${docs.uploading ? ' busy' : ''}`}
          onDragOver={e => { e.preventDefault(); setDragging(true) }}
          onDragLeave={() => setDragging(false)}
          onDrop={e => { e.preventDefault(); onDrop(e) }}
        >
          <div className="uz-icon">{docs.uploading ? '↻' : '⬆'}</div>
          <div className="uz-text">{docs.uploading ? '正在处理…' : '上传个人文件'}</div>
          <div className="uz-sub">{docs.uploading ? `${docs.uploadProgress}%` : '点击或拖拽到此处'}</div>
        </label>
        <input
          ref={fileInputRef}
          id="personalFileInput"
          className="file-input-accessible"
          type="file"
          accept=".txt,.md,.pdf,text/plain,text/markdown,application/pdf"
          multiple
          disabled={docs.uploading}
          onChange={onPick}
        />
        {docs.uploading && (
          <div className="upload-progress" aria-live="polite">
            <div className="upload-progress-track"><span style={{ width: `${docs.uploadProgress}%` }}></span></div>
            <span>{docs.uploadProgress}%</span>
          </div>
        )}
        {docs.uploadStatus && <div className="upload-message">{docs.uploadStatus}</div>}
        {docs.uploadError && <div className="upload-error" role="alert">{docs.uploadError}</div>}
        <div className="doc-list">
          {!docs.uploaded.length && <div style={{ fontSize: 11, color: 'var(--text3)', padding: '6px 0' }}>暂无文档</div>}
          {docs.uploaded.slice(0, 6).map(d => (
            <div key={d.name} className="doc-item">
              <span className="doc-icon">{d.needsOCR ? '!' : '✓'}</span>
              <span className="doc-name" title={docTitle(d)}>{d.name}</span>
              <span className="doc-chunks">{d.needsOCR ? '需 OCR' : `${d.chunks || 0} 块${d.indexed ? '/' + d.indexed : ''}`}</span>
              {d.docHash && <span className="doc-del" title="删除" onClick={() => removeDoc(d)}>×</span>}
            </div>
          ))}
        </div>
      </div>

      {/* 本地文档库 */}
      <div className="sec-label sec-label-row">
        <span>本地文档库</span>
        <button className="mini-refresh" type="button" disabled={docs.libraryLoading} onClick={() => docs.loadLibrary()}>
          {docs.libraryLoading ? '刷新中…' : '刷新'}
        </button>
      </div>
      {docs.libraryStatus && <div className="library-status" aria-live="polite">{docs.libraryStatus}</div>}
      <div className="blackhole library-panel">
        <div className="doc-list">
          {!docs.library.length && <div className="doc-empty">暂无本地文档。上传或保存的文档会出现在这里。</div>}
          {docs.library.slice(0, 8).map(d => (
            <div key={d.id} className="doc-item" role="button" onClick={() => docs.openViewer(d.id)}>
              <span className="doc-icon">§</span>
              <span className="doc-name" title={d.title || ''}>{d.title || '未命名文档'}</span>
              <span className="doc-chunks">
                {docs.ingestResults[d.id]
                  ? `${docs.ingestResults[d.id].indexed}/${docs.ingestResults[d.id].chunks} 块`
                  : `${d.rag_chunk_count || 0} 块 · v${d.latest_version || 0}`}
              </span>
              <span
                className={`doc-ingest${docs.ingestingIds[d.id] ? ' spinning' : ''}`}
                title={docs.ingestingIds[d.id] ? '正在入库' : '重新入库 RAG'}
                onClick={e => { e.stopPropagation(); docs.ingest(d.id) }}
              >
                {docs.ingestingIds[d.id] ? '…' : '↻'}
              </span>
              <span className="doc-del" title="删除本地文档" onClick={e => { e.stopPropagation(); removeLibraryDoc(d) }}>×</span>
            </div>
          ))}
        </div>
      </div>

      {/* 近期对话 */}
      <div className="sec-label">近期对话</div>
      <div className="recent">
        {!sessionsList.length && <div className="sess-empty">暂无对话记录</div>}
        {sessionsList.map(s => (
          <div
            key={s.id}
            className={`session-item${s.id === currentId ? ' active' : ''}`}
            onClick={() => switchTo(s.id)}
          >
            <span className="sess-dot">▹</span>
            <div className="sess-info">
              <div className="sess-title">{s.title}</div>
              <div className="sess-meta">{fmtDate(s.ts)}</div>
            </div>
            <div className="sess-del" title="删除" onClick={e => { e.stopPropagation(); del(s.id) }}>✕</div>
          </div>
        ))}
      </div>

      <button className="new-chat-btn" onClick={newChat}>＋ 新建对话</button>
      {sessionError && <p role="alert">{sessionError}</p>}

      {loggedIn && (
        <div className="user-bar" style={{ display: 'flex' }}>
          <span className="uname">{auth.username}</span>
          <button className="logout-btn" type="button" onClick={logout}>登出</button>
        </div>
      )}
    </aside>
  )
}
