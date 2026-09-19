<template>
  <div class="raglab-backdrop" @click.self="$emit('close')">
    <section class="raglab-shell" role="dialog" aria-modal="true" aria-label="RAG 流程实验台">
      <header class="raglab-header">
        <div class="raglab-title-wrap">
          <span class="raglab-logo" aria-hidden="true">
            <svg viewBox="0 0 24 24"><path d="M5 6.5 12 3l7 3.5v5c0 4.5-2.8 7.7-7 9.5-4.2-1.8-7-5-7-9.5v-5Z"/><path d="m8.5 12 2.1 2.1 4.9-5"/></svg>
          </span>
          <div>
            <p>检索增强流程实验</p>
            <h2>知识检索实验台（RAG）</h2>
            <span>用项目真实分块、向量化（Embedding）与生成能力，把黑盒流程拆成五个可观察步骤。</span>
          </div>
        </div>
        <button class="raglab-close" type="button" aria-label="关闭 RAG 流程实验台" @click="$emit('close')">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>
        </button>
      </header>

      <div class="raglab-content">
        <aside class="raglab-input-panel" aria-label="实验输入">
          <div class="panel-intro">
            <span class="section-index">实验输入</span>
            <div><h3>准备实验数据</h3><p>内容只在本次请求内处理，不会写入正式知识库。</p></div>
          </div>

          <label class="field-label" for="raglab-document-source">文档来源</label>
          <div class="source-row">
            <select id="raglab-document-source" v-model="selectedDocumentId" :disabled="loadingDocument" @change="loadSelectedDocument">
              <option value="">手动粘贴 / 演示样例</option>
              <option v-for="doc in docs.library" :key="doc.id" :value="doc.id">{{ doc.title || '未命名文档' }}</option>
            </select>
            <button class="icon-button" type="button" :disabled="docs.libraryLoading" aria-label="刷新本地文档列表" @click="docs.loadLibrary()">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 9a7 7 0 0 1 11.7-2L20 9M4 15l2.2 2a7 7 0 0 0 11.7-2"/></svg>
            </button>
          </div>
          <p v-if="sourceNotice" class="source-notice">{{ sourceNotice }}</p>

          <div class="field-head"><label class="field-label" for="raglab-document">实验文档</label><span>{{ documentText.length.toLocaleString() }} / 40,000 字</span></div>
          <textarea id="raglab-document" v-model="documentText" maxlength="40000" rows="10" placeholder="粘贴一段包含事实信息的文档，例如项目预算、制度说明或产品文档。"></textarea>

          <label class="field-label" for="raglab-query">用户查询</label>
          <textarea id="raglab-query" v-model="queryText" maxlength="1000" rows="3" placeholder="输入一个只能根据上方文档回答的问题。"></textarea>

          <div class="input-options">
            <label for="raglab-topk">召回数量</label>
            <select id="raglab-topk" v-model.number="topK">
              <option v-for="value in [3, 5, 8, 10]" :key="value" :value="value">前 {{ value }} 条</option>
            </select>
            <button class="sample-button" type="button" @click="loadExample">载入演示样例</button>
          </div>

          <button class="run-button" type="button" :disabled="running || !canRun" @click="runPipeline">
            <svg v-if="!running" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 7 8 5-8 5V7Z"/></svg>
            <span v-else class="button-spinner" aria-hidden="true"></span>
            {{ running ? '正在运行完整流程…' : '运行完整 RAG 流程' }}
          </button>
          <p v-if="error" class="raglab-error" role="alert"><strong>运行失败</strong>{{ error }}</p>
        </aside>

        <main class="raglab-workspace">
          <div v-if="!result && !running" class="raglab-empty">
            <div class="empty-diagram" aria-hidden="true">
              <span v-for="index in 5" :key="index">{{ index }}</span>
            </div>
            <h3>从一段文档开始观察 RAG</h3>
            <p>载入样例或选择本地文档，运行后可逐步查看父子分块、查询向量、余弦召回、增强提示词和最终答案。</p>
          </div>

          <div v-else-if="running" class="raglab-loading" aria-live="polite">
            <div class="loading-orbit"><span></span><span></span><span></span></div>
            <h3>正在执行真实流水线</h3>
            <p>长文档需要批量向量化，请稍候。页面不会把实验内容写入知识库。</p>
            <div class="loading-steps"><span v-for="name in stageNames" :key="name">{{ name }}</span></div>
          </div>

          <template v-else-if="result">
            <div class="run-overview">
              <div><span>运行编号</span><strong>{{ result.run_id }}</strong></div>
              <div><span>总耗时</span><strong>{{ formatMs(result.timings?.total_ms) }}</strong></div>
              <div><span>向量模式</span><strong :class="result.query.embedding_mode">{{ result.query.embedding_label }}</strong></div>
              <div><span>候选规模</span><strong>{{ result.retrieval.candidate_count }} 块</strong></div>
            </div>

            <nav class="pipeline-nav" aria-label="RAG 流程步骤">
              <button v-for="stage in result.stages" :key="stage.id" type="button" :class="{ active: selectedStage === stage.id }" @click="selectStage(stage.id)">
                <span class="stage-number">{{ stage.index }}</span>
                <span class="stage-copy"><b>{{ stage.name }}</b><small>{{ stageSummary(stage.summary) }}</small></span>
                <span class="stage-time">{{ formatMs(stage.duration_ms) }}</span>
              </button>
            </nav>

            <div v-if="result.warnings?.length" class="warning-strip" role="status">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 2.8 19h18.4L12 3Z"/><path d="M12 9v4M12 16h.01"/></svg>
              <div><strong>本次运行发生降级或截断</strong><span v-for="warning in result.warnings" :key="warning">{{ warningText(warning) }}</span></div>
            </div>

            <section v-if="selectedStage === 'split'" class="stage-panel split-panel">
              <div class="stage-heading"><div><span>步骤 01</span><h3>文档如何切成可检索单元</h3><p>先切父块保证语境，再切子块提高检索精度；每个子块只指向一个父块。</p></div></div>
              <div class="metric-grid">
                <article><span>文档字符</span><strong>{{ formatNumber(result.document.char_count) }}</strong></article>
                <article><span>父块</span><strong>{{ result.document.parent_count }}</strong></article>
                <article><span>子块</span><strong>{{ result.document.child_count }}</strong></article>
                <article><span>子块大小 / 重叠</span><strong>{{ result.document.child_chunk_size }} / {{ result.document.child_overlap }}</strong></article>
              </div>
              <div class="split-browser">
                <div class="parent-list" aria-label="父块列表">
                  <button v-for="parent in result.parents" :key="parent.id" v-memo="[parent.id, activeParentId]" type="button" :class="{ active: activeParentId === parent.id }" @click="activeParentId = parent.id">
                    <span>P{{ String(parent.id).padStart(2, '0') }}</span><b>{{ parent.child_ids.length }} 个子块</b><small>{{ parent.char_count }} 字</small>
                  </button>
                </div>
                <div class="chunk-map">
                  <div class="parent-preview"><span>当前父块 P{{ String(activeParentId).padStart(2, '0') }}</span><p>{{ activeParent?.content }}</p></div>
                  <div class="child-grid">
                    <article v-for="chunk in activeChildren" :key="chunk.id" v-memo="[chunk.id]">
                      <header><span>C{{ String(chunk.id).padStart(2, '0') }}</span><small>→ P{{ String(chunk.parent_id).padStart(2, '0') }}</small></header>
                      <p>{{ chunk.content }}</p><footer>{{ chunk.char_count }} 字</footer>
                    </article>
                  </div>
                </div>
              </div>
            </section>

            <section v-else-if="selectedStage === 'query'" class="stage-panel query-panel">
              <div class="stage-heading"><div><span>步骤 02</span><h3>把用户查询转换成向量</h3><p>查询和所有子块必须使用同一个向量化模型，才能在同一向量空间计算距离。</p></div></div>
              <div class="query-card"><span>原始查询</span><blockquote>{{ result.query.text }}</blockquote></div>
              <div class="vector-summary">
                <article><span>向量化模型提供方</span><strong>{{ embeddingLabel(result.query.embedding_label) }}</strong><small>{{ result.query.embedding_mode === 'remote_embedding' ? '真实项目配置' : '离线降级模式' }}</small></article>
                <article><span>向量维度</span><strong>{{ result.query.vector_dimension }}</strong><small>查询与子块维度一致</small></article>
                <article><span>L2 范数</span><strong>{{ result.query.vector_norm }}</strong><small>用于余弦相似度归一化</small></article>
              </div>
              <div class="vector-preview"><header><span>向量前 {{ result.query.vector_preview.length }} 维预览</span><small>完整向量不传到页面，避免无意义的大响应</small></header><div><span v-for="(value, index) in result.query.vector_preview" :key="index" :style="vectorBar(value)"><b>v{{ index + 1 }}</b><em>{{ value }}</em></span></div></div>
            </section>

            <section v-else-if="selectedStage === 'retrieve'" class="stage-panel retrieve-panel">
              <div class="stage-heading"><div><span>步骤 03</span><h3>用余弦相似度召回子块</h3><p>分数越高表示方向越接近。命中的是子块，下一步会按父块编号补回完整父块。</p></div><span class="formula">cos(q, d) = q·d / ‖q‖‖d‖</span></div>
              <div class="retrieval-list">
                <article v-for="item in result.retrieval.results" :key="item.id" v-memo="[item.id, item.score]">
                  <div class="rank-badge">{{ item.rank }}</div>
                  <div class="retrieval-copy"><header><span>C{{ String(item.id).padStart(2, '0') }} → P{{ String(item.parent_id).padStart(2, '0') }}</span><b>{{ scorePercent(item.score) }}</b></header><p>{{ item.content }}</p><small>子块 {{ item.char_count }} 字 · 父块 {{ item.parent_char_count }} 字 · 向量 {{ item.vector_dimension }} 维</small></div>
                  <div class="score-meter" :style="{ '--score': `${Math.max(0, item.score) * 100}%` }"><span></span></div>
                </article>
              </div>
            </section>

            <section v-else-if="selectedStage === 'augment'" class="stage-panel prompt-panel">
              <div class="stage-heading"><div><span>步骤 04</span><h3>小块召回，父块补全，再增强提示词</h3><p>同一父块只放一次，并受上下文预算约束，最后和问题一起交给生成模型。</p></div></div>
              <div class="prompt-metrics"><span>父块 {{ result.prompt.parent_count }}</span><span>上下文 {{ formatNumber(result.prompt.context_char_count) }} 字</span><span>约 {{ formatNumber(result.prompt.estimated_tokens) }} 个词元</span></div>
              <div class="prompt-grid">
                <article><header><span>系统指令</span><small>约束回答边界</small></header><pre>{{ result.prompt.system }}</pre></article>
                <article><header><span>用户问题 + 检索上下文</span><small>检索增强后的真实输入</small></header><pre>{{ result.prompt.user }}</pre></article>
              </div>
            </section>

            <section v-else class="stage-panel answer-panel">
              <div class="stage-heading"><div><span>步骤 05</span><h3>基于增强上下文生成答案</h3><p>答案应能回指上一步证据；若上下文不足，模型应拒绝编造。</p></div><span class="generation-badge" :class="result.generation_mode">{{ generationLabel }}</span></div>
              <article class="answer-card"><header><span>AGI-saber</span><small>{{ formatMs(result.timings.generation_ms) }}</small></header><div class="answer-text" v-html="answerHtml"></div></article>
              <div class="evidence-foot"><strong>生成依据</strong><span v-for="item in result.retrieval.results.slice(0, 3)" :key="item.id">P{{ String(item.parent_id).padStart(2, '0') }} / {{ scorePercent(item.score) }}</span></div>
            </section>
          </template>
        </main>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { fetchJSON } from '../api/client'
import { useDocs } from '../stores/docs'
import { renderMarkdown } from '../utils/markdown'

defineEmits(['close'])

const docs = useDocs()
const selectedDocumentId = ref('')
const documentText = ref('')
const queryText = ref('')
const topK = ref(5)
const running = ref(false)
const loadingDocument = ref(false)
const sourceNotice = ref('')
const error = ref('')
const result = ref(null)
const selectedStage = ref('split')
const activeParentId = ref(0)
const stageNames = ['文档切分', '查询向量化', '向量召回', 'Prompt 增强', '答案生成']

const canRun = computed(() => documentText.value.trim().length > 0 && queryText.value.trim().length > 0)
const activeParent = computed(() => result.value?.parents?.find(item => item.id === activeParentId.value) || result.value?.parents?.[0])
const activeChildren = computed(() => {
  if (!result.value || !activeParent.value) return []
  const ids = new Set(activeParent.value.child_ids || [])
  return result.value.chunks.filter(item => ids.has(item.id))
})
const answerHtml = computed(() => renderMarkdown(result.value?.answer || ''))
const generationLabel = computed(() => ({
  configured_llm: '模型生成完成', context_only: '仅上下文模式', generation_failed: '生成失败',
})[result.value?.generation_mode] || '已完成')

function loadExample() {
  selectedDocumentId.value = ''
  sourceNotice.value = '已载入内置演示样例，可直接运行。'
  documentText.value = `# 星槎-47 智慧园区一期方案

星槎-47 项目用于升级园区能耗监控与设备巡检能力。项目分两期建设，其中一期预算为 320 万元，计划在 2026 年 9 月启动，建设周期 6 个月。

一期范围包括 1,200 个传感器接入、能耗分析看板、设备异常告警和移动巡检。项目负责人为周启明，验收指标包括设备在线率不低于 99.5%、异常告警到达时间小于 30 秒。

## 二期计划

二期预算暂未确定，计划根据一期验收结果决定是否引入数字孪生模块。文档没有提供二期的具体金额。`
  queryText.value = '星槎-47 项目一期预算是多少，什么时候启动？'
}

async function loadSelectedDocument() {
  if (!selectedDocumentId.value) return
  loadingDocument.value = true
  error.value = ''
  sourceNotice.value = '正在读取本地文档…'
  try {
    const payload = await fetchJSON(`/api/documents/${encodeURIComponent(selectedDocumentId.value)}?offset=0&limit=40000`)
    documentText.value = payload.version?.content_md || ''
    const page = payload.content_page || {}
    sourceNotice.value = page.has_more
      ? `文档共 ${Number(page.total_chars || 0).toLocaleString()} 字，实验台载入前 40,000 字。`
      : `已载入 ${documentText.value.length.toLocaleString()} 字，不会重复入库。`
  } catch (cause) {
    error.value = cause?.message || '读取文档失败'
    sourceNotice.value = ''
  } finally {
    loadingDocument.value = false
  }
}

async function runPipeline() {
  if (!canRun.value || running.value) return
  running.value = true
  error.value = ''
  result.value = null
  selectedStage.value = 'split'
  try {
    result.value = await fetchJSON('/api/rag/lab/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ document: documentText.value, query: queryText.value, top_k: topK.value }),
    })
    activeParentId.value = result.value.parents?.[0]?.id || 0
  } catch (cause) {
    error.value = cause?.message || 'RAG 流程运行失败'
  } finally {
    running.value = false
  }
}

function selectStage(id) { selectedStage.value = id }
function formatMs(value) { return `${Number(value || 0).toFixed(Number(value || 0) < 10 ? 2 : 0)} ms` }
function formatNumber(value) { return Number(value || 0).toLocaleString() }
function scorePercent(value) { return `${(Math.max(-1, Math.min(1, Number(value || 0))) * 100).toFixed(1)}%` }
function stageSummary(value) { return String(value || '').replace(/召回 Top (\d+)/, '召回前 $1 条') }
function embeddingLabel(value) { return String(value || '').replace('项目 Embedding API', '项目向量化接口') }
function warningText(value) {
  return String(value || '')
    .replaceAll('Embedding', '向量化服务')
    .replaceAll('Prompt', '提示词')
    .replaceAll('Agent', '智能体')
}
function vectorBar(value) {
  const magnitude = Math.min(100, Math.max(8, Math.abs(Number(value || 0)) * 220))
  return { '--magnitude': `${magnitude}%`, '--direction': Number(value || 0) >= 0 ? '#2563eb' : '#e11d48' }
}
function onKeydown(event) { if (event.key === 'Escape') document.querySelector('.raglab-close')?.click() }

onMounted(() => {
  if (!docs.library.length) docs.loadLibrary()
  loadExample()
  document.addEventListener('keydown', onKeydown)
})
onBeforeUnmount(() => document.removeEventListener('keydown', onKeydown))
</script>

<style scoped>
.raglab-backdrop{position:fixed;inset:0;z-index:96;background:rgba(15,23,42,.5);backdrop-filter:blur(9px);display:grid;place-items:center;padding:18px;color:#172033}.raglab-shell{width:min(1560px,100%);height:min(940px,calc(100dvh - 36px));background:#f5f7fb;border:1px solid rgba(148,163,184,.45);border-radius:18px;box-shadow:0 28px 90px rgba(15,23,42,.28);display:flex;flex-direction:column;overflow:hidden}.raglab-header{height:88px;display:flex;align-items:center;justify-content:space-between;gap:20px;padding:16px 22px;background:#fff;border-bottom:1px solid #e3e8f0}.raglab-title-wrap{display:flex;align-items:center;gap:14px;min-width:0}.raglab-logo{width:48px;height:48px;border-radius:14px;display:grid;place-items:center;background:linear-gradient(145deg,#e9f2ff,#fff0f4);border:1px solid #cfe0fa;color:#2563eb;box-shadow:0 8px 24px rgba(37,99,235,.12)}.raglab-logo svg{width:27px;height:27px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}.raglab-title-wrap p{margin:0 0 3px;color:#e11d48;font:750 10px/1.2 ui-monospace,SFMono-Regular,Consolas,monospace;letter-spacing:.12em}.raglab-title-wrap h2{margin:0;font-size:22px;letter-spacing:-.02em}.raglab-title-wrap div>span{display:block;margin-top:4px;color:#64748b;font-size:12px}.raglab-close{width:44px;height:44px;border:1px solid #dbe2ec;border-radius:11px;background:#fff;color:#64748b;cursor:pointer;display:grid;place-items:center;transition:.2s}.raglab-close:hover{color:#e11d48;border-color:#fda4af;background:#fff1f4}.raglab-close:focus-visible,.pipeline-nav button:focus-visible,.run-button:focus-visible,.parent-list button:focus-visible,.icon-button:focus-visible,.sample-button:focus-visible,select:focus-visible,textarea:focus-visible{outline:3px solid rgba(37,99,235,.25);outline-offset:2px}.raglab-close svg{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round}.raglab-content{min-height:0;flex:1;display:grid;grid-template-columns:340px minmax(0,1fr)}.raglab-input-panel{overflow-y:auto;background:#fff;border-right:1px solid #e3e8f0;padding:20px;display:flex;flex-direction:column;gap:10px}.panel-intro{display:flex;gap:10px;align-items:flex-start;margin-bottom:5px}.section-index{font:750 9px/1 ui-monospace,SFMono-Regular,Consolas,monospace;letter-spacing:.1em;color:#2563eb;background:#eaf2ff;padding:6px 7px;border-radius:6px}.panel-intro h3{font-size:16px;margin:0}.panel-intro p{font-size:11px;color:#738095;line-height:1.5;margin:4px 0 0}.field-label{font-size:11px;font-weight:750;color:#38445b;margin-top:4px}.field-head{display:flex;align-items:center;justify-content:space-between}.field-head span{font-size:10px;color:#94a3b8}.source-row{display:flex;gap:8px}.source-row select,.input-options select{min-width:0;flex:1;height:42px;border:1px solid #dce3ed;border-radius:9px;background:#f9fbfe;padding:0 11px;color:#263149;font:500 12px/1.2 inherit}.icon-button{width:44px;height:42px;display:grid;place-items:center;border:1px solid #dce3ed;border-radius:9px;background:#fff;color:#2563eb;cursor:pointer}.icon-button svg,.run-button svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}.icon-button:disabled{opacity:.5;cursor:wait}.source-notice{font-size:10px;line-height:1.45;color:#2563eb;background:#eff6ff;border-radius:7px;padding:7px 9px}.raglab-input-panel textarea{width:100%;min-height:0;resize:vertical;border:1px solid #dce3ed;border-radius:10px;background:#f9fbfe;padding:11px 12px;color:#172033;font:500 12px/1.65 inherit;transition:.2s}.raglab-input-panel textarea:focus{border-color:#60a5fa;background:#fff}.input-options{display:grid;grid-template-columns:auto 88px 1fr;gap:8px;align-items:center;margin-top:3px}.input-options label{font-size:11px;color:#64748b}.sample-button{height:42px;border:1px solid #dce3ed;border-radius:9px;background:#fff;color:#475569;font-weight:650;cursor:pointer}.sample-button:hover{border-color:#60a5fa;color:#2563eb}.run-button{height:46px;margin-top:5px;border:0;border-radius:11px;background:linear-gradient(135deg,#e93466,#3478ee);color:#fff;font-weight:750;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:8px;box-shadow:0 9px 22px rgba(37,99,235,.2);transition:transform .18s,box-shadow .18s}.run-button:hover:not(:disabled){transform:translateY(-1px);box-shadow:0 12px 26px rgba(37,99,235,.28)}.run-button:disabled{opacity:.55;cursor:not-allowed;box-shadow:none}.button-spinner{width:17px;height:17px;border:2px solid rgba(255,255,255,.45);border-top-color:#fff;border-radius:50%;animation:spin .8s linear infinite}.raglab-error{display:flex;flex-direction:column;gap:2px;padding:9px 10px;border:1px solid #fecdd3;background:#fff1f3;border-radius:8px;color:#be123c;font-size:11px;line-height:1.45}.raglab-workspace{min-width:0;min-height:0;overflow-y:auto;padding:18px 20px 24px}.raglab-empty,.raglab-loading{height:100%;min-height:480px;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center}.empty-diagram{display:flex;align-items:center;margin-bottom:24px}.empty-diagram span{width:46px;height:46px;border-radius:14px;border:1px solid #cfe0fa;background:#fff;box-shadow:0 7px 18px rgba(37,99,235,.08);display:grid;place-items:center;color:#2563eb;font:750 12px/1 ui-monospace,monospace}.empty-diagram span+span{margin-left:25px;position:relative}.empty-diagram span+span:before{content:"";position:absolute;right:45px;width:26px;height:1px;background:#b9c9e4}.raglab-empty h3,.raglab-loading h3{font-size:19px;margin:0}.raglab-empty p,.raglab-loading p{max-width:520px;margin:8px 0 0;color:#718096;font-size:13px;line-height:1.7}.loading-orbit{width:70px;height:70px;position:relative;margin-bottom:22px}.loading-orbit span{position:absolute;width:14px;height:14px;border-radius:50%;background:#2563eb;top:28px;left:28px;animation:orbit 1.4s ease-in-out infinite}.loading-orbit span:nth-child(2){background:#e11d48;animation-delay:.15s}.loading-orbit span:nth-child(3){background:#22c55e;animation-delay:.3s}.loading-steps{display:flex;gap:8px;margin-top:22px;flex-wrap:wrap;justify-content:center}.loading-steps span{padding:6px 9px;border:1px solid #dbe5f3;border-radius:7px;background:#fff;color:#64748b;font-size:10px}.run-overview{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-bottom:12px}.run-overview div{display:flex;flex-direction:column;gap:4px;padding:10px 12px;background:#fff;border:1px solid #e0e6ef;border-radius:9px;min-width:0}.run-overview span{font-size:9px;text-transform:uppercase;letter-spacing:.07em;color:#94a3b8}.run-overview strong{font-size:11px;color:#334155;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.run-overview strong.local_hash_fallback{color:#b45309}.run-overview strong.remote_embedding{color:#047857}.pipeline-nav{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));background:#fff;border:1px solid #dfe5ee;border-radius:12px;overflow:hidden;margin-bottom:12px}.pipeline-nav button{position:relative;min-width:0;min-height:72px;border:0;border-right:1px solid #e6ebf2;background:#fff;padding:10px 10px;cursor:pointer;text-align:left;display:grid;grid-template-columns:30px minmax(0,1fr);grid-template-rows:auto auto;gap:0 8px;align-items:center;transition:.2s}.pipeline-nav button:last-child{border-right:0}.pipeline-nav button:hover{background:#f7faff}.pipeline-nav button.active{background:linear-gradient(135deg,#eef5ff,#fff5f7);box-shadow:inset 0 -3px #2563eb}.stage-number{grid-row:1/3;width:29px;height:29px;border-radius:9px;display:grid;place-items:center;background:#edf3fc;color:#2563eb;font:750 11px/1 ui-monospace,monospace}.pipeline-nav button.active .stage-number{background:#2563eb;color:#fff}.stage-copy{min-width:0;display:flex;flex-direction:column}.stage-copy b{font-size:12px;color:#263149}.stage-copy small{font-size:9px;color:#8793a6;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:3px}.stage-time{font:600 9px/1 ui-monospace,monospace;color:#94a3b8}.warning-strip{display:flex;gap:10px;align-items:flex-start;margin-bottom:12px;padding:10px 12px;background:#fffbeb;border:1px solid #fde68a;border-radius:9px;color:#92400e}.warning-strip svg{width:18px;height:18px;flex:none;fill:none;stroke:#d97706;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}.warning-strip div{display:flex;flex-direction:column;gap:2px}.warning-strip strong{font-size:11px}.warning-strip span{font-size:10px;line-height:1.45}.stage-panel{background:#fff;border:1px solid #dfe5ee;border-radius:13px;padding:18px;box-shadow:0 8px 26px rgba(30,41,59,.05)}.stage-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;margin-bottom:15px}.stage-heading>div>span{font:750 9px/1 ui-monospace,monospace;letter-spacing:.1em;color:#e11d48}.stage-heading h3{font-size:17px;margin:5px 0 0}.stage-heading p{font-size:11px;line-height:1.55;color:#718096;margin:4px 0 0}.metric-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-bottom:13px}.metric-grid article{padding:11px 12px;background:#f7f9fc;border:1px solid #e5eaf1;border-radius:9px;display:flex;flex-direction:column;gap:4px}.metric-grid span,.vector-summary span{font-size:9px;color:#8b97aa}.metric-grid strong{font-size:17px}.split-browser{height:430px;display:grid;grid-template-columns:145px minmax(0,1fr);border:1px solid #e1e7ef;border-radius:10px;overflow:hidden}.parent-list{overflow-y:auto;padding:7px;background:#f5f7fb;border-right:1px solid #e1e7ef}.parent-list button{width:100%;min-height:58px;margin-bottom:5px;border:1px solid transparent;border-radius:8px;background:transparent;padding:8px;text-align:left;cursor:pointer;display:grid;grid-template-columns:1fr auto;gap:3px}.parent-list button:hover{background:#fff}.parent-list button.active{background:#fff;border-color:#bfdbfe;box-shadow:0 5px 14px rgba(37,99,235,.08)}.parent-list span{grid-column:1/3;font:750 10px/1 ui-monospace,monospace;color:#2563eb}.parent-list b{font-size:10px;color:#334155}.parent-list small{font-size:9px;color:#94a3b8}.chunk-map{min-width:0;overflow-y:auto;padding:12px}.parent-preview{padding:11px 12px;background:#f7faff;border:1px solid #dbeafe;border-radius:9px;margin-bottom:10px}.parent-preview span{font:750 9px/1 ui-monospace,monospace;color:#2563eb}.parent-preview p{font-size:11px;line-height:1.6;color:#475569;margin-top:6px;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}.child-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.child-grid article{border:1px solid #e1e7ef;border-radius:9px;padding:10px;min-width:0}.child-grid header{display:flex;justify-content:space-between}.child-grid header span{font:750 10px/1 ui-monospace,monospace;color:#e11d48}.child-grid small,.child-grid footer{font-size:9px;color:#94a3b8}.child-grid p{font-size:10px;line-height:1.55;color:#475569;margin:7px 0;overflow-wrap:anywhere}.child-grid footer{text-align:right}.query-card{padding:14px;background:linear-gradient(135deg,#f1f6ff,#fff6f8);border:1px solid #dce7f8;border-radius:10px;margin-bottom:10px}.query-card>span{font-size:9px;font-weight:750;color:#64748b}.query-card blockquote{font-size:16px;font-weight:650;line-height:1.55;margin:7px 0 0;color:#1e293b}.vector-summary{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:10px}.vector-summary article{padding:12px;border:1px solid #e2e8f0;border-radius:9px;display:flex;flex-direction:column;gap:4px}.vector-summary strong{font-size:13px;color:#1e293b}.vector-summary small{font-size:9px;color:#94a3b8}.vector-preview{border:1px solid #e1e7ef;border-radius:10px;padding:13px}.vector-preview header{display:flex;justify-content:space-between;margin-bottom:12px}.vector-preview header span{font-size:11px;font-weight:700}.vector-preview header small{font-size:9px;color:#94a3b8}.vector-preview>div{display:grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:8px;height:160px;align-items:end}.vector-preview>div>span{height:100%;position:relative;display:flex;align-items:flex-end;justify-content:center;background:#f7f9fc;border-radius:7px;overflow:hidden}.vector-preview>div>span:before{content:"";position:absolute;bottom:25px;width:55%;height:var(--magnitude);max-height:110px;background:var(--direction);opacity:.72;border-radius:5px 5px 2px 2px}.vector-preview b{position:absolute;bottom:8px;font:700 8px/1 ui-monospace,monospace;color:#64748b}.vector-preview em{position:absolute;top:7px;font:600 8px/1 ui-monospace,monospace;color:#475569;font-style:normal}.formula{padding:7px 10px;background:#f1f5f9;border-radius:7px;font:600 10px/1.2 ui-monospace,monospace;color:#475569}.retrieval-list{display:flex;flex-direction:column;gap:8px}.retrieval-list article{display:grid;grid-template-columns:38px minmax(0,1fr) 88px;gap:10px;align-items:center;padding:11px;border:1px solid #e1e7ef;border-radius:10px}.rank-badge{width:32px;height:32px;border-radius:9px;display:grid;place-items:center;background:#edf4ff;color:#2563eb;font:750 11px/1 ui-monospace,monospace}.retrieval-copy{min-width:0}.retrieval-copy header{display:flex;justify-content:space-between;gap:10px}.retrieval-copy header span{font:700 9px/1 ui-monospace,monospace;color:#64748b}.retrieval-copy header b{font:750 11px/1 ui-monospace,monospace;color:#047857}.retrieval-copy p{font-size:10px;line-height:1.5;color:#334155;margin:5px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.retrieval-copy small{font-size:9px;color:#94a3b8}.score-meter{height:7px;border-radius:7px;background:#eef2f7;overflow:hidden}.score-meter span{display:block;width:var(--score);height:100%;background:linear-gradient(90deg,#60a5fa,#2563eb);border-radius:inherit}.prompt-metrics{display:flex;gap:7px;margin-bottom:10px}.prompt-metrics span{padding:5px 8px;border-radius:6px;background:#eff6ff;color:#1d4ed8;font-size:9px;font-weight:650}.prompt-grid{display:grid;grid-template-columns:minmax(0,.7fr) minmax(0,1.3fr);gap:9px;height:440px}.prompt-grid article{min-width:0;display:flex;flex-direction:column;border:1px solid #dfe5ee;border-radius:10px;overflow:hidden}.prompt-grid header,.answer-card header{display:flex;align-items:center;justify-content:space-between;padding:9px 11px;background:#f6f8fb;border-bottom:1px solid #e4e9f0}.prompt-grid header span,.answer-card header span{font:750 9px/1 ui-monospace,monospace;letter-spacing:.07em;color:#2563eb}.prompt-grid header small,.answer-card header small{font-size:9px;color:#94a3b8}.prompt-grid pre{margin:0;flex:1;overflow:auto;padding:12px;white-space:pre-wrap;overflow-wrap:anywhere;background:#182238;color:#dbeafe;font:500 10px/1.65 ui-monospace,SFMono-Regular,Consolas,monospace}.generation-badge{padding:7px 10px;border-radius:7px;font-size:10px;font-weight:700;background:#dcfce7;color:#047857}.generation-badge.context_only{background:#fef3c7;color:#92400e}.generation-badge.generation_failed{background:#ffe4e6;color:#be123c}.answer-card{border:1px solid #dfe5ee;border-radius:11px;overflow:hidden}.answer-card .answer-text{padding:17px;font-size:13px;line-height:1.75;min-height:220px;color:#1e293b}.evidence-foot{display:flex;align-items:center;gap:7px;margin-top:10px}.evidence-foot strong{font-size:10px;color:#64748b}.evidence-foot span{padding:5px 7px;border-radius:6px;background:#eff6ff;color:#2563eb;font:650 9px/1 ui-monospace,monospace}@keyframes spin{to{transform:rotate(360deg)}}@keyframes orbit{0%,100%{transform:translate(0,-26px)}33%{transform:translate(23px,15px)}66%{transform:translate(-23px,15px)}}@media(max-width:1050px){.raglab-content{grid-template-columns:300px minmax(0,1fr)}.run-overview{grid-template-columns:repeat(2,1fr)}.pipeline-nav{grid-template-columns:repeat(5,140px);overflow-x:auto}.child-grid{grid-template-columns:1fr}.prompt-grid{grid-template-columns:1fr;height:auto}.prompt-grid article{min-height:250px}}@media(max-width:760px){.raglab-backdrop{padding:0}.raglab-shell{width:100%;height:100dvh;border-radius:0}.raglab-header{height:auto;min-height:82px;padding:13px 15px}.raglab-title-wrap div>span{display:none}.raglab-content{display:block;overflow-y:auto}.raglab-input-panel{border-right:0;border-bottom:1px solid #e3e8f0;overflow:visible}.raglab-workspace{overflow:visible}.pipeline-nav{grid-template-columns:repeat(5,132px)}.metric-grid,.vector-summary{grid-template-columns:repeat(2,1fr)}.split-browser{height:auto;grid-template-columns:1fr}.parent-list{display:flex;overflow-x:auto;border-right:0;border-bottom:1px solid #e1e7ef}.parent-list button{min-width:120px}.vector-preview>div{grid-template-columns:repeat(4,1fr);height:280px}.retrieval-list article{grid-template-columns:38px minmax(0,1fr)}.score-meter{grid-column:2}.run-overview{grid-template-columns:1fr 1fr}}@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}}
/* Web accessibility overrides: interactive controls keep a 44px minimum target. */
.source-row select,.input-options select,.icon-button,.sample-button{min-height:44px}
</style>
