import { useMemo } from 'react'
import { reportBlocks, safeSourceURL } from '../utils/research'
import ReportInline from './ReportInline'

// 研究报告 + 来源证据 + 生成文件。引用点击/悬停跳转到对应来源卡片（focusSource）。
export default function ResearchReport(props: {
  result?: any
  sources?: any[]
  artifacts?: any[]
  report?: string
}) {
  const { result = {}, sources = [], artifacts = [], report = '' } = props

  const sourceURL = (source: any) => safeSourceURL(source.url || source.url_or_doc_id)
  const evidenceFor = (source: any) => (result.evidence || result.references || []).filter((item: any) => item.source_id === source.source_id)
  const blocks = useMemo(() => reportBlocks(report), [report])

  function focusSource(id: string) {
    const target = document.getElementById(`research-source-${id}`)
    target?.scrollIntoView({ behavior: 'auto', block: 'nearest' })
    target?.focus({ preventScroll: true })
  }
  function download(content: string, name: string, mediaType = 'text/markdown') {
    const url = URL.createObjectURL(new Blob([content], { type: `${mediaType || 'text/plain'};charset=utf-8` }))
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = name.replace(/[\\/:*?"<>|]/g, '_')
    anchor.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  return (
    <section className="research-results" aria-label="研究结果与证据">
      {report && (
        <section className="research-report" aria-labelledby="report-heading">
          <header className="section-heading">
            <div><span className="section-kicker">RESEARCH REPORT</span><h3 id="report-heading">研究报告</h3></div>
            <button type="button" className="research-button secondary" onClick={() => download(report, 'research-report.md')}>下载 Markdown</button>
          </header>
          {result.status === 'partial' && <p className="research-notice">部分完成：请结合下方研究限制和来源判断结论。</p>}
          <div className="report-body">
            {blocks.map((block, index) => {
              if (block.type === 'rule') return <hr key={index} />
              if (block.type === 'code') return <pre key={index} className="report-code"><code>{block.text}</code></pre>
              if (block.type === 'list') {
                const Tag = block.tag as 'ul' | 'ol'
                return (
                  <Tag key={index}>
                    {block.items.map((item, itemIndex) => (
                      <li key={itemIndex}><ReportInline text={item} sources={sources} references={result.references} onCitation={focusSource} /></li>
                    ))}
                  </Tag>
                )
              }
              const Tag = ((block as any).tag || (block.type === 'quote' ? 'blockquote' : 'p')) as keyof React.JSX.IntrinsicElements
              return (
                <Tag key={index}>
                  <ReportInline text={(block as any).text} sources={sources} references={result.references} onCitation={focusSource} />
                </Tag>
              )
            })}
          </div>
        </section>
      )}
      {result.limitations?.length > 0 && (
        <section className="research-limitations">
          <h3>研究限制</h3>
          <ul>{result.limitations.map((item: string, index: number) => <li key={index}>{item}</li>)}</ul>
        </section>
      )}
      {sources.length > 0 && (
        <section className="research-sources" aria-labelledby="sources-heading">
          <header className="section-heading">
            <div><span className="section-kicker">EVIDENCE</span><h3 id="sources-heading">来源与证据 <small>{sources.length}</small></h3></div>
          </header>
          {sources.map((source: any, index: number) => (
            <article
              id={`research-source-${source.source_id || `S${index + 1}`}`}
              key={source.source_id || index}
              className="source-card"
              tabIndex={-1}
            >
              <div className="source-heading">
                <span className="source-id">{source.source_id || `S${index + 1}`}</span>
                {sourceURL(source)
                  ? <a href={sourceURL(source)} target="_blank" rel="noopener noreferrer">{source.title || source.url}</a>
                  : <strong>{source.title || source.url_or_doc_id || '本地资料'}</strong>}
              </div>
              {(source.url_or_doc_id || source.url) && <p className="source-location">{source.url_or_doc_id || source.url}</p>}
              {evidenceFor(source).map((entry: any, evidenceIndex: number) => (
                <blockquote key={evidenceIndex}>
                  {entry.claim && <p>{entry.claim}</p>}
                  {entry.quote && <p>“{entry.quote}”</p>}
                </blockquote>
              ))}
              {source.content && <details><summary>查看来源摘录</summary><pre>{source.content}</pre></details>}
              {source.query && <small>检索：{source.query}</small>}
            </article>
          ))}
        </section>
      )}
      {artifacts.length > 0 && (
        <section className="research-artifacts">
          <h3>生成文件</h3>
          {artifacts.map((artifact: any, index: number) => (
            <div key={artifact.name || index} className="artifact-row">
              <span>{artifact.name || '研究文件'}<small>{artifact.media_type || 'text/plain'}</small></span>
              {typeof artifact.content === 'string'
                ? <button type="button" className="research-button secondary" onClick={() => download(artifact.content, artifact.name || 'research-output.txt', artifact.media_type)}>下载文件</button>
                : <span>文件元数据已记录</span>}
            </div>
          ))}
        </section>
      )}
    </section>
  )
}
