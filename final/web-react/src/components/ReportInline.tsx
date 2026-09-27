import { reportInlineTokens } from '../utils/research'

// 行内渲染：引用、加粗、斜体、行内代码、安全链接。只产出 React 节点，绝不产出 HTML。
export default function ReportInline(props: {
  text: string
  sources: any[]
  references: any[]
  onCitation: (sourceId: string) => void
}) {
  const tokens = reportInlineTokens(props.text, props.sources, props.references)
  return (
    <>
      {tokens.map((token, index) => {
        if (token.type === 'citation') {
          return (
            <a
              key={index}
              href={`#research-source-${token.sourceId}`}
              className="citation-link"
              aria-label={`查看来源 ${token.sourceId}`}
              onClick={e => { e.preventDefault(); props.onCitation(token.sourceId) }}
            >
              {token.text}
            </a>
          )
        }
        if (token.type === 'link') return <a key={index} href={token.href} target="_blank" rel="noopener noreferrer">{token.text}</a>
        if (token.type === 'strong') return <strong key={index}>{token.text}</strong>
        if (token.type === 'em') return <em key={index}>{token.text}</em>
        if (token.type === 'code') return <code key={index}>{token.text}</code>
        return <span key={index}>{token.text}</span>
      })}
    </>
  )
}
