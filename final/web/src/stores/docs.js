// stores/docs.js — 个人上传文件 + 本地文档库 + 文档查看器状态。
import { defineStore } from 'pinia'
import { apiFetch, apiURL, fetchJSON, getToken } from '../api/client'

const VIEWER_PAGE_SIZE = 30000

export const useDocs = defineStore('docs', {
  state: () => ({
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
    viewer: {
      open: false, id: null, title: '', meta: '', content: '', ingesting: false,
      loading: false, offset: 0, pageSize: VIEWER_PAGE_SIZE, totalChars: 0,
      hasPrevious: false, hasMore: false,
    },
  }),
  actions: {
    async uploadFile(file) {
      if (!file) return
      if (!/\.(txt|md|pdf)$/i.test(file.name || '')) {
        this.uploadError = '暂不支持该格式，请选择 TXT、Markdown 或 PDF 文件。'
        return
      }
      this.uploading = true
      this.uploadProgress = 0
      this.uploadStatus = `正在上传：${file.name}`
      this.uploadError = ''
      const form = new FormData()
      form.append('file', file)
      try {
        const res = await new Promise((resolve, reject) => {
          const xhr = new XMLHttpRequest()
          xhr.open('POST', apiURL('/api/upload'))
          const token = getToken()
          if (token) xhr.setRequestHeader('Authorization', 'Bearer ' + token)
          xhr.timeout = 10 * 60 * 1000
          xhr.upload.onprogress = event => {
            if (!event.lengthComputable) return
            this.uploadProgress = Math.min(99, Math.round(event.loaded * 100 / event.total))
          }
          xhr.upload.onload = () => {
            this.uploadProgress = 100
            this.uploadStatus = `正在解析、向量化并入库：${file.name}`
          }
          xhr.onerror = () => reject(new Error('网络连接失败，文件没有到达服务器。'))
          xhr.ontimeout = () => reject(new Error('处理超过 10 分钟，请换小文件或检查 Embedding 服务。'))
          xhr.onload = () => {
            let payload = {}
            try { payload = JSON.parse(xhr.responseText || '{}') } catch { payload = {} }
            if (xhr.status >= 200 && xhr.status < 300) resolve(payload)
            else if (xhr.status === 401) reject(new Error('登录已过期，请重新登录后上传。'))
            else reject(new Error(payload.detail || payload.error || `上传失败：HTTP ${xhr.status}`))
          }
          xhr.send(form)
        })
        const doc = {
          name: file.name, chunks: res.chunk_count, indexed: res.indexed_count || 0,
          parser: res.parser || '', pages: res.pages || 0, textChars: res.text_chars || 0,
          needsOCR: !!res.needs_ocr, docHash: res.doc_hash, ts: Date.now(),
        }
        this.uploaded = this.uploaded.filter(d => d.name !== file.name)
        this.uploaded.unshift(doc)
        if (res.needs_ocr) this.uploadStatus = `${file.name} 是扫描件，需要 OCR 后再入库。`
        else this.uploadStatus = `上传完成：${file.name}（${res.indexed_count || res.chunk_count || 0} 块）`
        await this.loadLibrary()
      } catch (error) {
        this.uploadError = error?.message || '上传失败，请稍后重试。'
        this.uploadStatus = ''
      } finally {
        this.uploading = false
      }
    },
    async deleteDoc(docHash) {
      const res = await apiFetch('/api/docs/delete', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ doc_hash: docHash }),
      })
      if (res.ok) this.uploaded = this.uploaded.filter(d => d.docHash !== docHash)
    },
    async loadLibrary() {
      if (this.libraryLoading) return
      this.libraryLoading = true
      try {
        const res = await fetchJSON('/api/documents')
        this.library = res.documents || []
        const time = new Date().toLocaleTimeString('zh-CN', { hour12: false })
        this.libraryStatus = `已刷新 ${time} · ${this.library.length} 个文档`
      } catch (error) {
        this.libraryStatus = `刷新失败：${error?.message || '网络错误'}`
      } finally {
        this.libraryLoading = false
      }
    },
    async openViewer(id) {
      this.viewer = {
        open: true, id, title: '正在读取文档…', meta: '', content: '正在加载预览…',
        ingesting: false, loading: true, offset: 0, pageSize: VIEWER_PAGE_SIZE,
        totalChars: 0, hasPrevious: false, hasMore: false,
      }
      await this.loadViewerPage(id, 0)
    },
    async loadViewerPage(id, offset = 0) {
      if (!id || (this.viewer.loading && this.viewer.id === id && this.viewer.totalChars > 0)) return
      const pageSize = this.viewer.pageSize || VIEWER_PAGE_SIZE
      this.viewer.loading = true
      try {
        const res = await fetchJSON(
          '/api/documents/' + encodeURIComponent(id) +
          `?offset=${Math.max(0, Number(offset) || 0)}&limit=${pageSize}`,
        )
        const doc = res.document || {}, ver = res.version || {}
        const page = res.content_page || {}
        const version = doc.latest_version || ver.version || 0
        this.viewer = {
          open: true, id: doc.id || null, title: doc.title || '本地文档',
          meta: `v${version} · ${doc.doc_type || 'document'} · ${doc.source || 'local'} · 分页预览`,
          content: ver.content_md || '（空文档）', ingesting: this.viewer.ingesting,
          loading: false, offset: page.offset || 0, pageSize,
          totalChars: page.total_chars ?? (ver.content_md || '').length,
          hasPrevious: !!page.has_previous, hasMore: !!page.has_more,
        }
      } catch (e) {
        this.viewer = {
          ...this.viewer, open: true, title: '读取文档失败', content: e.message,
          loading: false, hasPrevious: false, hasMore: false,
        }
      }
    },
    async viewerPrevious() {
      if (!this.viewer.id || this.viewer.loading || !this.viewer.hasPrevious) return
      await this.loadViewerPage(this.viewer.id, Math.max(0, this.viewer.offset - this.viewer.pageSize))
    },
    async viewerNext() {
      if (!this.viewer.id || this.viewer.loading || !this.viewer.hasMore) return
      await this.loadViewerPage(this.viewer.id, this.viewer.offset + this.viewer.pageSize)
    },
    closeViewer() { this.viewer.open = false; this.viewer.id = null },
    async ingest(id) {
      if (this.ingestingIds[id]) return null
      this.ingestingIds = { ...this.ingestingIds, [id]: true }
      this.uploadError = ''
      this.uploadStatus = '正在重新分块、批量向量化并入库，请不要重复点击…'
      try {
        const res = await fetchJSON('/api/documents/' + encodeURIComponent(id) + '/ingest', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
        })
        this.ingestResults = {
          ...this.ingestResults,
          [id]: { chunks: res.chunk_count || 0, indexed: res.indexed_count || res.chunk_count || 0 },
        }
        this.uploadStatus = `重新入库完成：${res.indexed_count || res.chunk_count || 0}/${res.chunk_count || 0} 块`
        await this.loadLibrary()
        return res
      } catch (error) {
        this.uploadStatus = ''
        this.uploadError = error?.message || '重新入库失败。'
        throw error
      } finally {
        this.ingestingIds = { ...this.ingestingIds, [id]: false }
      }
    },
    async deleteLibraryDoc(id) {
      await fetchJSON('/api/documents/' + encodeURIComponent(id), { method: 'DELETE' })
      this.library = this.library.filter(d => d.id !== id)
      if (this.viewer.id === id) this.closeViewer()
    },
    async ingestViewer() {
      if (!this.viewer.id) return
      this.viewer.ingesting = true
      try {
        const res = await this.ingest(this.viewer.id)
        this.viewer.meta += ` · 已入库 ${res.indexed_count || 0}/${res.chunk_count || 0}`
      } catch (e) {
        this.viewer.meta += ` · 入库失败：${e.message}`
      } finally { this.viewer.ingesting = false }
    },
    async deleteViewer() {
      if (!this.viewer.id) return
      await this.deleteLibraryDoc(this.viewer.id)
    },
    reset() {
      this.uploaded = []
      this.library = []
      this.libraryLoading = false
      this.libraryStatus = ''
      this.uploading = false
      this.uploadProgress = 0
      this.uploadStatus = ''
      this.uploadError = ''
      this.ingestingIds = {}
      this.ingestResults = {}
    },
  },
})
