import { useEffect, useMemo, useState } from 'react'
import { fetchJSON } from '../api/client'
import { useDocs } from '../stores/docs'
import { renderMarkdown } from '../utils/markdown'
import type { CSSProperties } from 'react'

const STAGE_NAMES = ['文档切分', '查询向量化', '向量召回', 'Prompt 增强', '答案生成']

function formatMs(value: any) { return `${Number(value || 0).toFixed(Number(value || 0) < 10 ? 2 : 0)} ms` }
function formatNumber(value: any) { return Number(value || 0).toLocaleString() }
function scorePercent(value: any) { return `${(Math.max(-1, Math.min(1, Number(value || 0))) * 100).toFixed(1)}%` }
function stageSummary(value: any) { return String(value || '').replace(/召回 Top (\d+)/, '召回前 $1 条') }
function embeddingLabel(value: any) { return String(value || '').replace('项目 Embedding API', '项目向量化接口') }
function warningText(value: any) {
  return String(value || '')
    .replaceAll('Embedding', '向量化服务')
    .replaceAll('Prompt', '提示词')
    .replaceAll('Agent', '智能体')
}
function vectorBar(value: any): CSSProperties {
  const magnitude = Math.min(100, Math.max(8, Math.abs(Number(value || 0)) * 220))
  return { '--magnitude': `${magnitude}%`, '--direction': Number(value || 0) >= 0 ? '#2563eb' : '#e11d48' } as CSSProperties
}

// 知识检索实验台：用真实分块/向量化/生成能力把 RAG 黑盒流程拆成五个可观察步骤。
export default function RagLab({ onClose }: { onClose: () => void }) {
  const docs = useDocs()
  const library = useDocs(s => s.library)
  const libraryLoading = useDocs(s => s.libraryLoading)
  const [selectedDocumentId, setSelectedDocumentId] = useState('')
  const [documentText, setDocumentText] = useState('')
  const [queryText, setQueryText] = useState('')
  const [topK, setTopK] = useState(5)
  const [running, setRunning] = useState(false)
  const [loadingDocument, setLoadingDocument] = useState(false)
  const [sourceNotice, setSourceNotice] = useState('')
  const [error, setError] = useState('')
  const [result, setResult] = useState<any>(null)
  const [selectedStage, setSelectedStage] = useState('split')
  const [activeParentId, setActiveParentId] = useState(0)

  const canRun = documentText.trim().length > 0 && queryText.trim().length > 0
  const activeParent = result?.parents?.find((item: any) => item.id === activeParentId) || result?.parents?.[0]
  const activeChildren = useMemo(() => {
    if (!result || !activeParent) return []
    const ids = new Set(activeParent.child_ids || [])
    return result.chunks.filter((item: any) => ids.has(item.id))
  }, [result, activeParent])
  const answerHtml = useMemo(() => renderMarkdown(result?.answer || ''), [result?.answer])
  const generationLabel = ({
    configured_llm: '模型生成完成', context_only: '仅上下文模式', generation_failed: '生成失败',
  } as Record<string, string>)[result?.generation_mode] || '已完成'

  function loadExample() {
    setSelectedDocumentId('')
    setSourceNotice('已载入内置演示样例，可直接运行。')
    setDocumentText(`# 星槎-47 智慧园区一期方案

星槎-47 项目用于升级园区能耗监控与设备巡检能力。项目分两期建设，其中一期预算为 320 万元，计划在 2026 年 9 月启动，建设周期 6 个月。

一期范围包括 1,200 个传感器接入、能耗分析看板、设备异常告警和移动巡检。项目负责人为周启明，验收指标包括设备在线率不低于 99.5%、异常告警到达时间小于 30 秒。

## 二期计划

二期预算暂未确定，计划根据一期验收结果决定是否引入数字孪生模块。文档没有提供二期的具体金额。`)
    setQueryText('星槎-47 项目一期预算是多少，什么时候启动？')
  }

  async function loadSelectedDocument() {
    if (!selectedDocumentId) return
    setLoadingDocument(true)
    setError('')
    setSourceNotice('正在读取本地文档…')
    try {
      const payload = await fetchJSON(`/api/documents/${encodeURIComponent(selectedDocumentId)}?offset=0&limit=40000`)
      setDocumentText(payload.version?.content_md || '')
      const page = payload.content_page || {}
      setSourceNotice(page.has_more
        ? `文档共 ${Number(page.total_chars || 0).toLocaleString()} 字，实验台载入前 40,000 字。`
        : `已载入 ${documentText.length.toLocaleString()} 字，不会重复入库。`)
    } catch (cause: any) {
      setError(cause?.message || '读取文档失败')
      setSourceNotice('')
    } finally {
      setLoadingDocument(false)
    }
  }

  async function runPipeline() {
    if (!canRun || running) return
    setRunning(true)
    setError('')
    setResult(null)
    setSelectedStage('split')
    try {
      const data = await fetchJSON('/api/rag/lab/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ document: documentText, query: queryText, top_k: topK }),
      })
      setResult(data)
      setActiveParentId(data.parents?.[0]?.id || 0)
    } catch (cause: any) {
      setError(cause?.message || 'RAG 流程运行失败')
    } finally {
      setRunning(false)
    }
  }

  useEffect(() => {
    if (!useDocs.getState().library.length) useDocs.getState().loadLibrary()
    loadExample()
    function onKeydown(event: KeyboardEvent) { if (event.key === 'Escape') (document.querySelector('.raglab-close') as HTMLElement | null)?.click() }
    document.addEventListener('keydown', onKeydown)
    return () => document.removeEventListener('keydown', onKeydown)
  }, [])

  return (
    <div className="raglab-backdrop" onClick={e => { if (e.target === e.currentTarget) onClose() }}>
      <section className="raglab-shell" role="dialog" aria-modal="true" aria-label="RAG 流程实验台">
        <header className="raglab-header">
          <div className="raglab-title-wrap">
            <span className="raglab-logo" aria-hidden="true">
              <svg viewBox="0 0 24 24"><path d="M5 6.5 12 3l7 3.5v5c0 4.5-2.8 7.7-7 9.5-4.2-1.8-7-5-7-9.5v-5Z"/><path d="m8.5 12 2.1 2.1 4.9-5"/></svg>
            </span>
            <div>
              <p>检索增强流程实验</p>
              <h2>知识检索实验台（RAG）</h2>
              <span>用项目真实分块、向量化（Embedding）与生成能力，把黑盒流程拆成五个可观察步骤。</span>
            </div>
          </div>
          <button className="raglab-close" type="button" aria-label="关闭 RAG 流程实验台" onClick={onClose}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>
          </button>
        </header>

        <div className="raglab-content">
          <aside className="raglab-input-panel" aria-label="实验输入">
            <div className="panel-intro">
              <span className="section-index">实验输入</span>
              <div><h3>准备实验数据</h3><p>内容只在本次请求内处理，不会写入正式知识库。</p></div>
            </div>

            <label className="field-label" htmlFor="raglab-document-source">文档来源</label>
            <div className="source-row">
              <select
                id="raglab-document-source"
                value={selectedDocumentId}
                disabled={loadingDocument}
                onChange={e => { setSelectedDocumentId(e.target.value); setTimeout(() => loadSelectedDocument(), 0) }}
              >
                <option value="">手动粘贴 / 演示样例</option>
                {library.map((doc: any) => <option key={doc.id} value={doc.id}>{doc.title || '未命名文档'}</option>)}
              </select>
              <button
                className="icon-button"
                type="button"
                disabled={libraryLoading}
                aria-label="刷新本地文档列表"
                onClick={() => docs.loadLibrary()}
              >
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 9a7 7 0 0 1 11.7-2L20 9M4 15l2.2 2a7 7 0 0 0 11.7-2"/></svg>
              </button>
            </div>
            {sourceNotice && <p className="source-notice">{sourceNotice}</p>}

            <div className="field-head">
              <label className="field-label" htmlFor="raglab-document">实验文档</label>
              <span>{documentText.length.toLocaleString()} / 40,000 字</span>
            </div>
            <textarea
              id="raglab-document"
              value={documentText}
              maxLength={40000}
              rows={10}
              placeholder="粘贴一段包含事实信息的文档，例如项目预算、制度说明或产品文档。"
              onChange={e => setDocumentText(e.target.value)}
            />

            <label className="field-label" htmlFor="raglab-query">用户查询</label>
            <textarea
              id="raglab-query"
              value={queryText}
              maxLength={1000}
              rows={3}
              placeholder="输入一个只能根据上方文档回答的问题。"
              onChange={e => setQueryText(e.target.value)}
            />

            <div className="input-options">
              <label htmlFor="raglab-topk">召回数量</label>
              <select id="raglab-topk" value={topK} onChange={e => setTopK(Number(e.target.value))}>
                {[3, 5, 8, 10].map(value => <option key={value} value={value}>前 {value} 条</option>)}
              </select>
              <button className="sample-button" type="button" onClick={loadExample}>载入演示样例</button>
            </div>

            <button className="run-button" type="button" disabled={running || !canRun} onClick={runPipeline}>
              {!running
                ? <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 7 8 5-8 5V7Z"/></svg>
                : <span className="button-spinner" aria-hidden="true"></span>}
              {running ? '正在运行完整流程…' : '运行完整 RAG 流程'}
            </button>
            {error && <p className="raglab-error" role="alert"><strong>运行失败</strong>{error}</p>}
          </aside>

          <main className="raglab-workspace">
            {!result && !running && (
              <div className="raglab-empty">
                <div className="empty-diagram" aria-hidden="true">
                  {[1, 2, 3, 4, 5].map(index => <span key={index}>{index}</span>)}
                </div>
                <h3>从一段文档开始观察 RAG</h3>
                <p>载入样例或选择本地文档，运行后可逐步查看父子分块、查询向量、余弦召回、增强提示词和最终答案。</p>
              </div>
            )}

            {running && (
              <div className="raglab-loading" aria-live="polite">
                <div className="loading-orbit"><span></span><span></span><span></span></div>
                <h3>正在执行真实流水线</h3>
                <p>长文档需要批量向量化，请稍候。页面不会把实验内容写入知识库。</p>
                <div className="loading-steps">{STAGE_NAMES.map(name => <span key={name}>{name}</span>)}</div>
              </div>
            )}

            {result && (
              <>
                <div className="run-overview">
                  <div><span>运行编号</span><strong>{result.run_id}</strong></div>
                  <div><span>总耗时</span><strong>{formatMs(result.timings?.total_ms)}</strong></div>
                  <div><span>向量模式</span><strong className={result.query.embedding_mode}>{result.query.embedding_label}</strong></div>
                  <div><span>候选规模</span><strong>{result.retrieval.candidate_count} 块</strong></div>
                </div>

                <nav className="pipeline-nav" aria-label="RAG 流程步骤">
                  {result.stages.map((stage: any) => (
                    <button key={stage.id} type="button" className={selectedStage === stage.id ? 'active' : ''} onClick={() => setSelectedStage(stage.id)}>
                      <span className="stage-number">{stage.index}</span>
                      <span className="stage-copy"><b>{stage.name}</b><small>{stageSummary(stage.summary)}</small></span>
                      <span className="stage-time">{formatMs(stage.duration_ms)}</span>
                    </button>
                  ))}
                </nav>

                {result.warnings?.length > 0 && (
                  <div className="warning-strip" role="status">
                    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 2.8 19h18.4L12 3Z"/><path d="M12 9v4M12 16h.01"/></svg>
                    <div><strong>本次运行发生降级或截断</strong>{result.warnings.map((warning: any) => <span key={warning}>{warningText(warning)}</span>)}</div>
                  </div>
                )}

                {selectedStage === 'split' && (
                  <section className="stage-panel split-panel">
                    <div className="stage-heading"><div><span>步骤 01</span><h3>文档如何切成可检索单元</h3><p>先切父块保证语境，再切子块提高检索精度；每个子块只指向一个父块。</p></div></div>
                    <div className="metric-grid">
                      <article><span>文档字符</span><strong>{formatNumber(result.document.char_count)}</strong></article>
                      <article><span>父块</span><strong>{result.document.parent_count}</strong></article>
                      <article><span>子块</span><strong>{result.document.child_count}</strong></article>
                      <article><span>子块大小 / 重叠</span><strong>{result.document.child_chunk_size} / {result.document.child_overlap}</strong></article>
                    </div>
                    <div className="split-browser">
                      <div className="parent-list" aria-label="父块列表">
                        {result.parents.map((parent: any) => (
                          <button key={parent.id} type="button" className={activeParentId === parent.id ? 'active' : ''} onClick={() => setActiveParentId(parent.id)}>
                            <span>P{String(parent.id).padStart(2, '0')}</span><b>{parent.child_ids.length} 个子块</b><small>{parent.char_count} 字</small>
                          </button>
                        ))}
                      </div>
                      <div className="chunk-map">
                        <div className="parent-preview">
                          <span>当前父块 P{String(activeParent?.id ?? 0).padStart(2, '0')}</span>
                          <p>{activeParent?.content}</p>
                        </div>
                        <div className="child-grid">
                          {activeChildren.map((chunk: any) => (
                            <article key={chunk.id}>
                              <header><span>C{String(chunk.id).padStart(2, '0')}</span><small>→ P{String(chunk.parent_id).padStart(2, '0')}</small></header>
                              <p>{chunk.content}</p><footer>{chunk.char_count} 字</footer>
                            </article>
                          ))}
                        </div>
                      </div>
                    </div>
                  </section>
                )}

                {selectedStage === 'query' && (
                  <section className="stage-panel query-panel">
                    <div className="stage-heading"><div><span>步骤 02</span><h3>把用户查询转换成向量</h3><p>查询和所有子块必须使用同一个向量化模型，才能在同一向量空间计算距离。</p></div></div>
                    <div className="query-card"><span>原始查询</span><blockquote>{result.query.text}</blockquote></div>
                    <div className="vector-summary">
                      <article><span>向量化模型提供方</span><strong>{embeddingLabel(result.query.embedding_label)}</strong><small>{result.query.embedding_mode === 'remote_embedding' ? '真实项目配置' : '离线降级模式'}</small></article>
                      <article><span>向量维度</span><strong>{result.query.vector_dimension}</strong><small>查询与子块维度一致</small></article>
                      <article><span>L2 范数</span><strong>{result.query.vector_norm}</strong><small>用于余弦相似度归一化</small></article>
                    </div>
                    <div className="vector-preview">
                      <header><span>向量前 {result.query.vector_preview.length} 维预览</span><small>完整向量不传到页面，避免无意义的大响应</small></header>
                      <div>
                        {result.query.vector_preview.map((value: number, index: number) => (
                          <span key={index} style={vectorBar(value)}><b>v{index + 1}</b><em>{value}</em></span>
                        ))}
                      </div>
                    </div>
                  </section>
                )}

                {selectedStage === 'retrieve' && (
                  <section className="stage-panel retrieve-panel">
                    <div className="stage-heading">
                      <div><span>步骤 03</span><h3>用余弦相似度召回子块</h3><p>分数越高表示方向越接近。命中的是子块，下一步会按父块编号补回完整父块。</p></div>
                      <span className="formula">cos(q, d) = q·d / ‖q‖‖d‖</span>
                    </div>
                    <div className="retrieval-list">
                      {result.retrieval.results.map((item: any) => (
                        <article key={item.id}>
                          <div className="rank-badge">{item.rank}</div>
                          <div className="retrieval-copy">
                            <header><span>C{String(item.id).padStart(2, '0')} → P{String(item.parent_id).padStart(2, '0')}</span><b>{scorePercent(item.score)}</b></header>
                            <p>{item.content}</p>
                            <small>子块 {item.char_count} 字 · 父块 {item.parent_char_count} 字 · 向量 {item.vector_dimension} 维</small>
                          </div>
                          <div className="score-meter" style={{ '--score': `${Math.max(0, item.score) * 100}%` } as CSSProperties}><span></span></div>
                        </article>
                      ))}
                    </div>
                  </section>
                )}

                {selectedStage === 'augment' && (
                  <section className="stage-panel prompt-panel">
                    <div className="stage-heading"><div><span>步骤 04</span><h3>小块召回，父块补全，再增强提示词</h3><p>同一父块只放一次，并受上下文预算约束，最后和问题一起交给生成模型。</p></div></div>
                    <div className="prompt-metrics">
                      <span>父块 {result.prompt.parent_count}</span>
                      <span>上下文 {formatNumber(result.prompt.context_char_count)} 字</span>
                      <span>约 {formatNumber(result.prompt.estimated_tokens)} 个词元</span>
                    </div>
                    <div className="prompt-grid">
                      <article><header><span>系统指令</span><small>约束回答边界</small></header><pre>{result.prompt.system}</pre></article>
                      <article><header><span>用户问题 + 检索上下文</span><small>检索增强后的真实输入</small></header><pre>{result.prompt.user}</pre></article>
                    </div>
                  </section>
                )}

                {selectedStage === 'answer' && (
                  <section className="stage-panel answer-panel">
                    <div className="stage-heading">
                      <div><span>步骤 05</span><h3>基于增强上下文生成答案</h3><p>答案应能回指上一步证据；若上下文不足，模型应拒绝编造。</p></div>
                      <span className={`generation-badge ${result.generation_mode}`}>{generationLabel}</span>
                    </div>
                    <article className="answer-card">
                      <header><span>AGI-saber</span><small>{formatMs(result.timings.generation_ms)}</small></header>
                      <div className="answer-text" dangerouslySetInnerHTML={{ __html: answerHtml }} />
                    </article>
                    <div className="evidence-foot">
                      <strong>生成依据</strong>
                      {result.retrieval.results.slice(0, 3).map((item: any) => (
                        <span key={item.id}>P{String(item.parent_id).padStart(2, '0')} / {scorePercent(item.score)}</span>
                      ))}
                    </div>
                  </section>
                )}
              </>
            )}
          </main>
        </div>
      </section>
    </div>
  )
}
