import { useChat, modeHint } from '../stores/chat'
import { useSkills, selectEnabledCount } from '../stores/skills'

export default function ControlsBar(props: {
  features: { research: boolean }
  onOpenSkills: () => void
  onOpenEvaluation: () => void
  onOpenRagLab: () => void
  onOpenNativeRuns: () => void
  onOpenResearch: () => void
}) {
  const ragOn = useChat(s => s.ragOn)
  const toggleRag = useChat(s => s.toggleRag)
  const enabledCount = useSkills(selectEnabledCount)
  const hint = modeHint(ragOn)

  return (
    <div className="controls-bar">
      <div className={`rag-toggle-wrap${ragOn ? ' on' : ''}`} onClick={() => toggleRag()}>
        <div className="toggle-pill"></div>
        <span className="toggle-label">知识库</span>
      </div>

      <div className="tools-btn-wrap">
        <button className={`tools-btn${enabledCount > 0 ? ' active' : ''}`} type="button" onClick={props.onOpenSkills}>
          <svg className="control-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M8.5 4H5v3.5a2.5 2.5 0 1 0 0 5V16h3.5a2.5 2.5 0 1 0 5 0H17v-3.5a2.5 2.5 0 1 0 0-5V4h-3.5a2.5 2.5 0 1 0-5 0Z"/></svg>
          <span>技能广场</span>
          {enabledCount > 0 && <span className="tool-count">{enabledCount}</span>}
        </button>
      </div>

      {props.features.research && (
        <div className="tools-btn-wrap">
          <button className="tools-btn native-run-entry" type="button" onClick={props.onOpenResearch}>
            <svg className="control-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="10" cy="10" r="6"/><path d="m15 15 5 5M7 10h6M10 7v6"/></svg>
            <span>研究工作台</span>
          </button>
        </div>
      )}

      <div className="tools-btn-wrap">
        <button className="tools-btn eval-entry" type="button" onClick={props.onOpenEvaluation}>
          <span className="eval-mark">QA</span>
          <span>智能体评测</span>
        </button>
      </div>

      <div className="tools-btn-wrap">
        <button className="tools-btn native-run-entry" type="button" onClick={props.onOpenNativeRuns}>
          <span className="eval-mark">RUN</span><span>运行记录</span>
        </button>
      </div>

      <div className="tools-btn-wrap">
        <button className="tools-btn raglab-entry" type="button" onClick={props.onOpenRagLab}>
          <svg className="control-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h5M15 6h5M9 6a3 3 0 1 0 6 0 3 3 0 0 0-6 0ZM4 18h5M15 18h5M9 18a3 3 0 1 0 6 0 3 3 0 0 0-6 0ZM12 9v6"/></svg>
          <span>知识检索实验台</span>
        </button>
      </div>

      <div className={`controls-right ${hint.cls}`}>{hint.text}</div>
    </div>
  )
}
