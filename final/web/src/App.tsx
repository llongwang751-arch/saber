import { useCallback, useEffect, useState } from 'react'
import { fetchJSON, setUnauthorizedHandler } from './api/client'
import { useAuth, selectLoggedIn } from './stores/auth'
import { useDocs } from './stores/docs'
import { useSkills } from './stores/skills'
import { useSessions } from './stores/sessions'
import { useChat } from './stores/chat'
import { useEvaluation } from './stores/evaluation'
import SideBar from './components/SideBar'
import ControlsBar from './components/ControlsBar'
import MessageList from './components/MessageList'
import ChatInput from './components/ChatInput'
import ToolApprovals from './components/ToolApprovals'
import TaskRecovery from './components/TaskRecovery'
import AuthModal from './components/AuthModal'
import SkillHub from './components/SkillHub'
import DocViewer from './components/DocViewer'
import EvaluationDashboard from './components/EvaluationDashboard'
import RagLab from './components/RagLab'
import RunWorkbench from './components/RunWorkbench'

type RunMode = 'research' | 'chat'

export default function App() {
  const [skillHubOpen, setSkillHubOpen] = useState(false)
  const [evaluationOpen, setEvaluationOpen] = useState(false)
  const [ragLabOpen, setRagLabOpen] = useState(false)
  const [runWorkbenchOpen, setRunWorkbenchOpen] = useState(false)
  const [runMode, setRunMode] = useState<RunMode>('research')
  const [features, setFeatures] = useState<{ research: boolean }>({ research: true })
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const loggedIn = useAuth(selectLoggedIn)
  const authOverlay = useAuth(s => s.overlay)

  function openRuns(mode: RunMode) { setRunMode(mode); setRunWorkbenchOpen(true) }

  // 401 → 清 token + 弹登录层 + 中断在飞的对话
  useEffect(() => {
    setUnauthorizedHandler(() => {
      useChat.getState().abortInflight()
      setRunWorkbenchOpen(false)
      useAuth.getState().logout()
    })
  }, [])

  const initApp = useCallback(() => {
    useDocs.getState().loadLibrary()
    useSkills.getState().loadInstalled()
    fetchJSON('/api/status').then(status => {
      if (status.features) setFeatures(f => ({ ...f, ...status.features }))
    }).catch(() => {})
  }, [])

  useEffect(() => {
    if (loggedIn) initApp()
    else {
      useSessions.getState().reset()
      useDocs.getState().reset()
      useSkills.getState().reset()
      useEvaluation.getState().reset()
    }
  }, [loggedIn, initApp])

  return (
    <>
      <div className="bg-decor"><span className="glow blue"></span><span className="glow red"></span></div>

      <SideBar
        id="sidebar-navigation"
        className={sidebarOpen ? 'mobile-open' : ''}
        onKeyDown={(e: React.KeyboardEvent) => { if (e.key === 'Escape') setSidebarOpen(false) }}
      />

      <div className="main">
        <button
          type="button"
          className="mobile-navigation"
          aria-controls="sidebar-navigation"
          aria-expanded={sidebarOpen}
          onClick={() => setSidebarOpen(v => !v)}
        >
          {sidebarOpen ? '关闭文件与会话' : '文件与会话'}
        </button>
        <ControlsBar
          features={features}
          onOpenSkills={() => setSkillHubOpen(true)}
          onOpenEvaluation={() => setEvaluationOpen(true)}
          onOpenRagLab={() => setRagLabOpen(true)}
          onOpenNativeRuns={() => openRuns('chat')}
          onOpenResearch={() => openRuns('research')}
        />
        <MessageList />
        <ToolApprovals />
        <TaskRecovery />
        <ChatInput />
      </div>

      {authOverlay && <AuthModal />}
      {skillHubOpen && <SkillHub onClose={() => setSkillHubOpen(false)} />}
      {evaluationOpen && <EvaluationDashboard onClose={() => setEvaluationOpen(false)} />}
      {ragLabOpen && <RagLab onClose={() => setRagLabOpen(false)} />}
      {runWorkbenchOpen && (
        <RunWorkbench
          initialMode={runMode}
          researchEnabled={features.research}
          onClose={() => setRunWorkbenchOpen(false)}
        />
      )}
      <DocViewer />
    </>
  )
}
