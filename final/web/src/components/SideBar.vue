<template>
  <aside class="sidebar">
    <div class="sidebar-logo">
      <div class="logo-mark">◈</div>
      <div>
        <div class="logo-name">AGI-saber</div>
        <div class="logo-sub">智能协作 · 一站直达</div>
      </div>
    </div>

    <!-- 个人文件 -->
    <div class="sec-label">个人文件</div>
    <div class="blackhole">
      <label for="personalFileInput" class="upload-zone" :class="{ drag: dragging, busy: docs.uploading }"
           @dragover.prevent="dragging = true"
           @dragleave="dragging = false"
           @drop.prevent="onDrop">
        <div class="uz-icon">{{ docs.uploading ? '↻' : '⬆' }}</div>
        <div class="uz-text">{{ docs.uploading ? '正在处理…' : '上传个人文件' }}</div>
        <div class="uz-sub">{{ docs.uploading ? `${docs.uploadProgress}%` : '点击或拖拽到此处' }}</div>
      </label>
      <input id="personalFileInput" class="file-input-accessible" type="file"
             accept=".txt,.md,.pdf,text/plain,text/markdown,application/pdf" multiple
             :disabled="docs.uploading" @change="onPick" />
      <div v-if="docs.uploading" class="upload-progress" aria-live="polite">
        <div class="upload-progress-track"><span :style="{ width: `${docs.uploadProgress}%` }"></span></div>
        <span>{{ docs.uploadProgress }}%</span>
      </div>
      <div v-if="docs.uploadStatus" class="upload-message">{{ docs.uploadStatus }}</div>
      <div v-if="docs.uploadError" class="upload-error" role="alert">{{ docs.uploadError }}</div>
      <div class="doc-list">
        <div v-if="!docs.uploaded.length" style="font-size:11px;color:var(--text3);padding:6px 0">暂无文档</div>
        <div v-for="d in docs.uploaded.slice(0, 6)" :key="d.name" class="doc-item">
          <span class="doc-icon">{{ d.needsOCR ? '!' : '✓' }}</span>
          <span class="doc-name" :title="docTitle(d)">{{ d.name }}</span>
          <span class="doc-chunks">{{ d.needsOCR ? '需 OCR' : `${d.chunks || 0} 块${d.indexed ? '/' + d.indexed : ''}` }}</span>
          <span v-if="d.docHash" class="doc-del" title="删除" @click="removeDoc(d)">×</span>
        </div>
      </div>
    </div>

    <!-- 本地文档库 -->
    <div class="sec-label sec-label-row">
      <span>本地文档库</span>
      <button class="mini-refresh" type="button" :disabled="docs.libraryLoading" @click="docs.loadLibrary()">
        {{ docs.libraryLoading ? '刷新中…' : '刷新' }}
      </button>
    </div>
    <div v-if="docs.libraryStatus" class="library-status" aria-live="polite">{{ docs.libraryStatus }}</div>
    <div class="blackhole library-panel">
      <div class="doc-list">
        <div v-if="!docs.library.length" class="doc-empty">暂无本地文档。上传或保存的文档会出现在这里。</div>
        <div v-for="d in docs.library.slice(0, 8)" :key="d.id" class="doc-item" role="button" @click="docs.openViewer(d.id)">
          <span class="doc-icon">§</span>
          <span class="doc-name" :title="d.title || ''">{{ d.title || '未命名文档' }}</span>
          <span class="doc-chunks">{{ docs.ingestResults[d.id] ? `${docs.ingestResults[d.id].indexed}/${docs.ingestResults[d.id].chunks} 块` : `${d.rag_chunk_count || 0} 块 · v${d.latest_version || 0}` }}</span>
          <span class="doc-ingest" :class="{ spinning: docs.ingestingIds[d.id] }"
                :title="docs.ingestingIds[d.id] ? '正在入库' : '重新入库 RAG'"
                @click.stop="docs.ingest(d.id)">{{ docs.ingestingIds[d.id] ? '…' : '↻' }}</span>
          <span class="doc-del" title="删除本地文档" @click.stop="removeLibraryDoc(d)">×</span>
        </div>
      </div>
    </div>

    <!-- 近期对话 -->
    <div class="sec-label">近期对话</div>
    <div class="recent">
      <div v-if="!sessions.sessions.length" class="sess-empty">暂无对话记录</div>
      <div v-for="s in sessions.sessions" :key="s.id"
           class="session-item" :class="{ active: s.id === sessions.currentId }"
           @click="switchTo(s.id)">
        <span class="sess-dot">▹</span>
        <div class="sess-info">
          <div class="sess-title">{{ s.title }}</div>
          <div class="sess-meta">{{ fmtDate(s.ts) }}</div>
        </div>
        <div class="sess-del" title="删除" @click.stop="del(s.id)">✕</div>
      </div>
    </div>

    <button class="new-chat-btn" @click="newChat">＋ 新建对话</button>
    <p v-if="sessionError" role="alert">{{ sessionError }}</p>

    <div v-if="auth.loggedIn" class="user-bar" style="display:flex">
      <span class="uname">{{ auth.username }}</span>
      <button class="logout-btn" type="button" @click="logout">登出</button>
    </div>
  </aside>
</template>

<script setup>
import { ref } from 'vue'
import { useDocs } from '../stores/docs'
import { useSessions } from '../stores/sessions'
import { useAuth } from '../stores/auth'
import { useChat } from '../stores/chat'

const docs = useDocs()
const sessions = useSessions()
const auth = useAuth()
const chat = useChat()
const dragging = ref(false)
const sessionError = ref('')

async function onPick(e) { for (const f of e.target.files || []) await docs.uploadFile(f); e.target.value = '' }
async function onDrop(e) { dragging.value = false; for (const f of e.dataTransfer.files) await docs.uploadFile(f) }
function docTitle(d) { return `${d.name}${d.pages ? ' · ' + d.pages + ' 页' : ''}${d.textChars ? ' · ' + d.textChars + ' 字' : ''}${d.parser ? ' · ' + d.parser : ''}` }
async function removeDoc(d) { if (confirm('确定删除「' + d.name + '」？')) await docs.deleteDoc(d.docHash) }
async function removeLibraryDoc(d) { if (confirm('确定删除本地文档「' + (d.title || '未命名文档') + '」？')) await docs.deleteLibraryDoc(d.id) }
function fmtDate(ts) { return new Date(ts).toLocaleDateString('zh', { month: 'short', day: 'numeric' }) }
function switchTo(id) { chat.abortInflight(); sessions.switchSession(id) }
function del(id) { sessions.deleteSession(id) }
async function newChat() {
  chat.stop(); sessionError.value = ''
  try { await sessions.newSession() }
  catch (e) { sessionError.value = '创建会话失败：' + e.message }
}
function logout() { chat.abortInflight(); auth.logout() }
</script>
