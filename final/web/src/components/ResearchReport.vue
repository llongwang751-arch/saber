<template>
  <section class="research-results" aria-label="研究结果与证据">
    <section v-if="report" class="research-report" aria-labelledby="report-heading">
      <header class="section-heading"><div><span class="section-kicker">RESEARCH REPORT</span><h3 id="report-heading">研究报告</h3></div><button type="button" class="research-button secondary" @click="download(report, 'research-report.md')">下载 Markdown</button></header>
      <p v-if="result.status === 'partial'" class="research-notice">部分完成：请结合下方研究限制和来源判断结论。</p>
      <div class="report-body">
        <template v-for="(block, index) in blocks" :key="index">
          <hr v-if="block.type === 'rule'" />
          <pre v-else-if="block.type === 'code'" class="report-code"><code>{{ block.text }}</code></pre>
          <component :is="block.tag" v-else-if="block.type === 'list'"><li v-for="(item, itemIndex) in block.items" :key="itemIndex"><ReportInline :text="item" :sources="sources" :references="result.references" @citation="focusSource" /></li></component>
          <component :is="block.tag || (block.type === 'quote' ? 'blockquote' : 'p')" v-else><ReportInline :text="block.text" :sources="sources" :references="result.references" @citation="focusSource" /></component>
        </template>
      </div>
    </section>
    <section v-if="result.limitations?.length" class="research-limitations"><h3>研究限制</h3><ul><li v-for="(item, index) in result.limitations" :key="index">{{ item }}</li></ul></section>
    <section v-if="sources.length" class="research-sources" aria-labelledby="sources-heading">
      <header class="section-heading"><div><span class="section-kicker">EVIDENCE</span><h3 id="sources-heading">来源与证据 <small>{{ sources.length }}</small></h3></div></header>
      <article v-for="(source, index) in sources" :id="`research-source-${source.source_id || `S${index + 1}`}`" :key="source.source_id || index" class="source-card" tabindex="-1">
        <div class="source-heading"><span class="source-id">{{ source.source_id || `S${index + 1}` }}</span><a v-if="sourceURL(source)" :href="sourceURL(source)" target="_blank" rel="noopener noreferrer">{{ source.title || source.url }}</a><strong v-else>{{ source.title || source.url_or_doc_id || '本地资料' }}</strong></div>
        <p v-if="source.url_or_doc_id || source.url" class="source-location">{{ source.url_or_doc_id || source.url }}</p>
        <blockquote v-for="(entry, evidenceIndex) in evidenceFor(source)" :key="evidenceIndex"><p v-if="entry.claim">{{ entry.claim }}</p><p v-if="entry.quote">“{{ entry.quote }}”</p></blockquote>
        <details v-if="source.content"><summary>查看来源摘录</summary><pre>{{ source.content }}</pre></details>
        <small v-if="source.query">检索：{{ source.query }}</small>
      </article>
    </section>
    <section v-if="artifacts.length" class="research-artifacts"><h3>生成文件</h3><div v-for="(artifact, index) in artifacts" :key="artifact.name || index" class="artifact-row"><span>{{ artifact.name || '研究文件' }}<small>{{ artifact.media_type || 'text/plain' }}</small></span><button v-if="typeof artifact.content === 'string'" type="button" class="research-button secondary" @click="download(artifact.content, artifact.name || 'research-output.txt', artifact.media_type)">下载文件</button><span v-else>文件元数据已记录</span></div></section>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import { reportBlocks, safeSourceURL } from '../utils/research'
import ReportInline from './ReportInline.vue'
const props = defineProps({ result: { type: Object, default: () => ({}) }, sources: { type: Array, default: () => [] }, artifacts: { type: Array, default: () => [] }, report: { type: String, default: '' } })
const sourceURL = source => safeSourceURL(source.url || source.url_or_doc_id)
const evidenceFor = source => (props.result.evidence || props.result.references || []).filter(item => item.source_id === source.source_id)
const blocks = computed(() => reportBlocks(props.report))
function focusSource(id) {
  const target = document.getElementById(`research-source-${id}`)
  target?.scrollIntoView({ behavior: 'auto', block: 'nearest' })
  target?.focus({ preventScroll: true })
}
function download(content, name, mediaType = 'text/markdown') {
  const url = URL.createObjectURL(new Blob([content], { type: `${mediaType || 'text/plain'};charset=utf-8` }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = name.replace(/[\\/:*?"<>|]/g, '_')
  anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
</script>
