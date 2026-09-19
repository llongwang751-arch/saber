<template>
  <div class="bg-decor"><span class="glow blue"></span><span class="glow red"></span></div>

  <SideBar />

  <div class="main">
    <ControlsBar @open-skills="skillHubOpen = true" @open-medical="medicalOpen = true" @open-evaluation="evaluationOpen = true" @open-rag-lab="ragLabOpen = true" />
    <MessageList />
    <ToolApprovals />
    <TaskRecovery />
    <ChatInput />
  </div>

  <AuthModal v-if="auth.overlay" />
  <SkillHub v-if="skillHubOpen" @close="skillHubOpen = false" />
  <MedicalDashboard v-if="medicalOpen" @close="medicalOpen = false" />
  <EvaluationDashboard v-if="evaluationOpen" @close="evaluationOpen = false" />
  <RagLab v-if="ragLabOpen" @close="ragLabOpen = false" />
  <DocViewer />
</template>

<script setup>
import { ref, watch, onMounted } from 'vue'
import { setUnauthorizedHandler } from './api/client'
import { useAuth } from './stores/auth'
import { useDocs } from './stores/docs'
import { useSkills } from './stores/skills'
import { useSessions } from './stores/sessions'
import { useChat } from './stores/chat'
import { useEvaluation } from './stores/evaluation'
import SideBar from './components/SideBar.vue'
import ControlsBar from './components/ControlsBar.vue'
import MessageList from './components/MessageList.vue'
import ChatInput from './components/ChatInput.vue'
import ToolApprovals from './components/ToolApprovals.vue'
import TaskRecovery from './components/TaskRecovery.vue'
import AuthModal from './components/AuthModal.vue'
import SkillHub from './components/SkillHub.vue'
import DocViewer from './components/DocViewer.vue'
import EvaluationDashboard from './components/EvaluationDashboard.vue'
import MedicalDashboard from './components/MedicalDashboard.vue'
import RagLab from './components/RagLab.vue'

const auth = useAuth()
const docs = useDocs()
const skills = useSkills()
const sessions = useSessions()
const chat = useChat()
const evaluation = useEvaluation()
const skillHubOpen = ref(false)
const medicalOpen = ref(false)
const evaluationOpen = ref(false)
const ragLabOpen = ref(false)

// 401 → 清 token + 弹登录层 + 中断在飞的对话
setUnauthorizedHandler(() => {
  chat.abortInflight()
  auth.logout()
})

function initApp() {
  docs.loadLibrary()
  skills.loadInstalled()
}

watch(() => auth.loggedIn, (v) => {
  if (v) initApp()
  else { sessions.reset(); docs.reset(); skills.reset(); evaluation.reset() }
})

onMounted(() => { if (auth.loggedIn) initApp() })
</script>
