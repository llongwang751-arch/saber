// stores/docs.ts — 个人上传文件 + 本地文档库 + 文档查看器状态。
// Pinia → Zustand 移植：所有 action 与端点调用一一对应（XHR 上传进度、分页查看器、重新入库）。
import { create } from 'zustand'
import { apiFetch, apiURL, fetchJSON, getToken } from '../api/client'

const VIEWER_PAGE_SIZE = 30000

export interface UploadedDoc {
  name: string
  chunks: number
  indexed: number
  parser: string
  pages: number
  textChars: number
  needsOCR: boolean
  docHash: string
  ts: number
}

export interface ViewerState {
  open: boolean
  id: string | null
  title: string
  meta: string
  content: string
  ingesting: boolean
  loading: boolean
  offset: number
  pageSize: number
  totalChars: number
  hasPrevious: boolean
  hasMore: boolean
}

export interface DocsState {
  uploaded: UploadedDoc[]
  library: any[]
  libraryLoading: boolean
  libraryStatus: string
  uploading: boolean
  uploadProgress: number
  uploadStatus: string
  uploadError: string
  ingestingIds: Record<string, boolean>
  ingestResults: Record<string, { chunks: number; indexed: number }>
  viewer: ViewerState
  uploadFile(file: File): Promise<void>
  deleteDoc(docHash: string): Promise<void>
  loadLibrary(): Promise<void>
  openViewer(id: string): Promise<void>
  loadViewerPage(id: string, offset?: number): Promise<void>
  viewerPrevious(): Promise<void>
  viewerNext(): Promise<void>
  closeViewer(): void
  ingest(id: string): Promise<any>
  deleteLibraryDoc(id: string): Promise<void>
  ingestViewer(): Promise<void>
  deleteViewer(): Promise<void>
  reset(): void
}

const initialViewer: ViewerState = {
  open: false, id: null, title: '', meta: '', content: '', ingesting: false,
  loading: false, offset: 0, pageSize: VIEWER_PAGE_SIZE, totalChars: 0,
  hasPrevious: false, hasMore: false,
}

export const useDocs = create<DocsState>()((set, get) => ({
  uploaded: [],
  library: [],
  libraryLoading: false,
  libraryStatus: '',
  uploading: false,
  uploadProgress: 0,
  uploadStatus: '',
  uploadError: '',
  ingestingIds: {},
  ingestResults: {},
  viewer: initialViewer,

  async uploadFile(file) {
    if (!file) return
    if (!/\.(txt|md|pdf)$/i.test(file.name || '')) {
      set({ uploadError: '暂不支持该格式，请选择 TXT、Markdown 或 PDF 文件。' })
      return
    }
    set({ uploading: true, uploadProgress: 0, uploadStatus: `正在上传：${file.name}`, uploadError: '' })
    const form = new FormData()
    form.append('file', file)
    try {
      const res: any = await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest()
        xhr.open('POST', apiURL('/api/upload'))
        const token = getToken()
        if (token) xhr.setRequestHeader('Authorization', 'Bearer ' + token)
        xhr.timeout = 10 * 60 * 1000
        xhr.upload.onprogress = event => {
          if (!event.lengthComputable) return
          set({ uploadProgress: Math.min(99, Math.round(event.loaded * 100 / event.total)) })
        }
        xhr.upload.onload = () => {
          set({ uploadProgress: 100, uploadStatus: `正在解析、向量化并入库：${file.name}` })
        }
        xhr.onerror = () => reject(new Error('网络连接失败，文件没有到达服务器。'))
        xhr.ontimeout = () => reject(new Error('处理超过 10 分钟，请换小文件或检查 Embedding 服务。'))
        xhr.onload = () => {
          let payload: any = {}
          try { payload = JSON.parse(xhr.responseText || '{}') } catch { payload = {} }
          if (xhr.status >= 200 && xhr.status < 300) resolve(payload)
          else if (xhr.status === 401) reject(new Error('登录已过期，请重新登录后上传。'))
          else reject(new Error(payload.detail || payload.error || `上传失败：HTTP ${xhr.status}`))
        }
        xhr.send(form)
      })
      const doc: UploadedDoc = {
        name: file.name, chunks: res.chunk_count, indexed: res.indexed_count || 0,
        parser: res.parser || '', pages: res.pages || 0, textChars: res.text_chars || 0,
        needsOCR: !!res.needs_ocr, docHash: res.doc_hash, ts: Date.now(),
      }
      set(state => ({
        uploaded: [{ ...doc }, ...state.uploaded.filter(d => d.name !== file.name)],
      }))
      set({
        uploadStatus: res.needs_ocr
          ? `${file.name} 是扫描件，需要 OCR 后再入库。`
          : `上传完成：${file.name}（${res.indexed_count || res.chunk_count || 0} 块）`,
      })
      await get().loadLibrary()
    } catch (error: any) {
      set({ uploadError: error?.message || '上传失败，请稍后重试。', uploadStatus: '' })
    } finally {
      set({ uploading: false })
    }
  },
  async deleteDoc(docHash) {
    const res = await apiFetch('/api/docs/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ doc_hash: docHash }),
    })
    if (res.ok) set(state => ({ uploaded: state.uploaded.filter(d => d.docHash !== docHash) }))
  },
  async loadLibrary() {
    if (get().libraryLoading) return
    set({ libraryLoading: true })
    try {
      const res = await fetchJSON('/api/documents')
      const library = res.documents || []
      const time = new Date().toLocaleTimeString('zh-CN', { hour12: false })
      set({ library, libraryStatus: `已刷新 ${time} · ${library.length} 个文档` })
    } catch (error: any) {
      set({ libraryStatus: `刷新失败：${error?.message || '网络错误'}` })
    } finally {
      set({ libraryLoading: false })
    }
  },
  async openViewer(id) {
    set({
      viewer: {
        open: true, id, title: '正在读取文档…', meta: '', content: '正在加载预览…',
        ingesting: false, loading: true, offset: 0, pageSize: VIEWER_PAGE_SIZE,
        totalChars: 0, hasPrevious: false, hasMore: false,
      },
    })
    await get().loadViewerPage(id, 0)
  },
  async loadViewerPage(id, offset = 0) {
    const viewer = get().viewer
    if (!id || (viewer.loading && viewer.id === id && viewer.totalChars > 0)) return
    const pageSize = viewer.pageSize || VIEWER_PAGE_SIZE
    set(state => ({ viewer: { ...state.viewer, loading: true } }))
    try {
      const res = await fetchJSON(
        '/api/documents/' + encodeURIComponent(id) +
        `?offset=${Math.max(0, Number(offset) || 0)}&limit=${pageSize}`,
      )
      const doc = res.document || {}
      const ver = res.version || {}
      const page = res.content_page || {}
      const version = doc.latest_version || ver.version || 0
      set({
        viewer: {
          open: true, id: doc.id || null, title: doc.title || '本地文档',
          meta: `v${version} · ${doc.doc_type || 'document'} · ${doc.source || 'local'} · 分页预览`,
          content: ver.content_md || '（空文档）', ingesting: get().viewer.ingesting,
          loading: false, offset: page.offset || 0, pageSize,
          totalChars: page.total_chars ?? (ver.content_md || '').length,
          hasPrevious: !!page.has_previous, hasMore: !!page.has_more,
        },
      })
    } catch (e: any) {
      set(state => ({
        viewer: {
          ...state.viewer, open: true, title: '读取文档失败', content: e.message,
          loading: false, hasPrevious: false, hasMore: false,
        },
      }))
    }
  },
  async viewerPrevious() {
    const viewer = get().viewer
    if (!viewer.id || viewer.loading || !viewer.hasPrevious) return
    await get().loadViewerPage(viewer.id, Math.max(0, viewer.offset - viewer.pageSize))
  },
  async viewerNext() {
    const viewer = get().viewer
    if (!viewer.id || viewer.loading || !viewer.hasMore) return
    await get().loadViewerPage(viewer.id, viewer.offset + viewer.pageSize)
  },
  closeViewer() { set(state => ({ viewer: { ...state.viewer, open: false, id: null } })) },
  async ingest(id) {
    if (get().ingestingIds[id]) return null
    set(state => ({ ingestingIds: { ...state.ingestingIds, [id]: true }, uploadError: '', uploadStatus: '正在重新分块、批量向量化并入库，请不要重复点击…' }))
    try {
      const res = await fetchJSON('/api/documents/' + encodeURIComponent(id) + '/ingest', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      })
      set(state => ({
        ingestResults: {
          ...state.ingestResults,
          [id]: { chunks: res.chunk_count || 0, indexed: res.indexed_count || res.chunk_count || 0 },
        },
        uploadStatus: `重新入库完成：${res.indexed_count || res.chunk_count || 0}/${res.chunk_count || 0} 块`,
      }))
      await get().loadLibrary()
      return res
    } catch (error: any) {
      set({ uploadStatus: '', uploadError: error?.message || '重新入库失败。' })
      throw error
    } finally {
      set(state => ({ ingestingIds: { ...state.ingestingIds, [id]: false } }))
    }
  },
  async deleteLibraryDoc(id) {
    await fetchJSON('/api/documents/' + encodeURIComponent(id), { method: 'DELETE' })
    set(state => ({ library: state.library.filter(d => d.id !== id) }))
    if (get().viewer.id === id) get().closeViewer()
  },
  async ingestViewer() {
    const id = get().viewer.id
    if (!id) return
    set(state => ({ viewer: { ...state.viewer, ingesting: true } }))
    try {
      const res = await get().ingest(id)
      set(state => ({ viewer: { ...state.viewer, meta: state.viewer.meta + ` · 已入库 ${res.indexed_count || 0}/${res.chunk_count || 0}` } }))
    } catch (e: any) {
      set(state => ({ viewer: { ...state.viewer, meta: state.viewer.meta + ` · 入库失败：${e.message}` } }))
    } finally {
      set(state => ({ viewer: { ...state.viewer, ingesting: false } }))
    }
  },
  async deleteViewer() {
    const id = get().viewer.id
    if (!id) return
    await get().deleteLibraryDoc(id)
  },
  reset() {
    set({
      uploaded: [], library: [], libraryLoading: false, libraryStatus: '',
      uploading: false, uploadProgress: 0, uploadStatus: '', uploadError: '',
      ingestingIds: {}, ingestResults: {},
    })
  },
}))
