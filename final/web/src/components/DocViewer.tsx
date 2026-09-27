import { useEffect, useState } from 'react'
import { useDocs } from '../stores/docs'

// 本地文档查看器：始终挂载（与 Vue 版一致），由 docs.viewer.open 控制显示。
export default function DocViewer() {
  const docs = useDocs()
  const viewer = useDocs(s => s.viewer)

  async function removeViewer() {
    if (confirm('确定删除当前本地文档？')) await docs.deleteViewer()
  }
  function pageLabel() {
    if (viewer.loading) return '正在加载…'
    if (!viewer.totalChars) return '0 字符'
    const start = viewer.offset + 1
    const end = Math.min(viewer.totalChars, viewer.offset + viewer.content.length)
    return `${start.toLocaleString()}–${end.toLocaleString()} / ${viewer.totalChars.toLocaleString()} 字符`
  }

  useEffect(() => {
    function onEsc(e: KeyboardEvent) { if (e.key === 'Escape') useDocs.getState().closeViewer() }
    document.addEventListener('keydown', onEsc)
    return () => document.removeEventListener('keydown', onEsc)
  }, [])

  return (
    <div className={`doc-viewer-backdrop${viewer.open ? ' open' : ''}`} onClick={e => { if (e.target === e.currentTarget) docs.closeViewer() }}>
      <section className="doc-viewer" role="dialog" aria-modal="true">
        <div className="doc-viewer-head">
          <div className="doc-viewer-title-wrap">
            <div className="doc-viewer-kicker">本地文档库</div>
            <div className="doc-viewer-title">{viewer.title}</div>
            <div className="doc-viewer-meta">{viewer.meta}</div>
          </div>
          <div className="doc-viewer-actions">
            <button className="doc-viewer-action" type="button" disabled={!viewer.id || viewer.ingesting} onClick={() => docs.ingestViewer()}>↻ 入库</button>
            <button className="doc-viewer-action" type="button" disabled={!viewer.id} onClick={removeViewer}>删除</button>
            <button className="doc-viewer-close" type="button" aria-label="关闭文档" onClick={() => docs.closeViewer()}>×</button>
          </div>
        </div>
        <div className="doc-viewer-pager">
          <button type="button" disabled={viewer.loading || !viewer.hasPrevious} onClick={() => docs.viewerPrevious()}>上一段</button>
          <span>{pageLabel()}</span>
          <button type="button" disabled={viewer.loading || !viewer.hasMore} onClick={() => docs.viewerNext()}>下一段</button>
        </div>
        <div className="doc-viewer-body">
          <pre className="doc-viewer-content">{viewer.content}</pre>
        </div>
      </section>
    </div>
  )
}
