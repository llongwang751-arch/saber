<template>
  <div class="doc-viewer-backdrop" :class="{ open: docs.viewer.open }" @click.self="docs.closeViewer()">
    <section class="doc-viewer" role="dialog" aria-modal="true">
      <div class="doc-viewer-head">
        <div class="doc-viewer-title-wrap">
          <div class="doc-viewer-kicker">本地文档库</div>
          <div class="doc-viewer-title">{{ docs.viewer.title }}</div>
          <div class="doc-viewer-meta">{{ docs.viewer.meta }}</div>
        </div>
        <div class="doc-viewer-actions">
          <button class="doc-viewer-action" type="button" :disabled="!docs.viewer.id || docs.viewer.ingesting" @click="docs.ingestViewer()">↻ 入库</button>
          <button class="doc-viewer-action" type="button" :disabled="!docs.viewer.id" @click="removeViewer()">删除</button>
          <button class="doc-viewer-close" type="button" aria-label="关闭文档" @click="docs.closeViewer()">×</button>
        </div>
      </div>
      <div class="doc-viewer-pager">
        <button type="button" :disabled="docs.viewer.loading || !docs.viewer.hasPrevious" @click="docs.viewerPrevious()">上一段</button>
        <span>{{ pageLabel() }}</span>
        <button type="button" :disabled="docs.viewer.loading || !docs.viewer.hasMore" @click="docs.viewerNext()">下一段</button>
      </div>
      <div class="doc-viewer-body">
        <pre class="doc-viewer-content">{{ docs.viewer.content }}</pre>
      </div>
    </section>
  </div>
</template>

<script setup>
import { onMounted, onBeforeUnmount } from 'vue'
import { useDocs } from '../stores/docs'

const docs = useDocs()
async function removeViewer() {
  if (confirm('确定删除当前本地文档？')) await docs.deleteViewer()
}
function onEsc(e) { if (e.key === 'Escape') docs.closeViewer() }
function pageLabel() {
  if (docs.viewer.loading) return '正在加载…'
  if (!docs.viewer.totalChars) return '0 字符'
  const start = docs.viewer.offset + 1
  const end = Math.min(docs.viewer.totalChars, docs.viewer.offset + docs.viewer.content.length)
  return `${start.toLocaleString()}–${end.toLocaleString()} / ${docs.viewer.totalChars.toLocaleString()} 字符`
}
onMounted(() => document.addEventListener('keydown', onEsc))
onBeforeUnmount(() => document.removeEventListener('keydown', onEsc))
</script>
