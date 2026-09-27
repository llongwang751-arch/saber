<template>
  <div class="bg-decor"><span class="glow blue"></span><span class="glow red"></span></div>

  <SideBar id="sidebar-navigation" :class="{ 'mobile-open': sidebarOpen }" @keydown.esc="sidebarOpen = false" />

  <div class="main">
    <button type="button" class="mobile-navigation" aria-controls="sidebar-navigation" :aria-expanded="sidebarOpen" @click="sidebarOpen = !sidebarOpen">{{ sidebarOpen ? '关闭文件与会话' : '文件与会话' }}</button>
    <ControlsBar :features="features" @open-skills="skillHubOpen = true" @open-medical="medicalOpen = true" @open-evaluation="evaluationOpen = true" @open-rag-lab="ragLabOpen = true" @open-native-runs="openRuns('chat')" @open-research="openRuns('research')" />
    <MessageList />
    <ToolApprovals />
    <TaskRecovery />
    <ChatInput />
  </div>

  <AuthModal v-if="auth.overlay" />
  <SkillHub v-if="skillHubOpen" @close="skillHubOpen = false" />
  <MedicalDashboard v-if="medicalOpen && features.medical" @close="medicalOpen = false" />
  <EvaluationDashboard v-if="evaluationOpen" :experiments-enabled="features.experiments" @close="evaluationOpen = false" />
  <RagLab v-if="ragLabOpen" @close="ragLabOpen = false" />
  <RunWorkbench v-if="runWorkbenchOpen" :initial-mode="runMode" :research-enabled="features.research" @close="runWorkbenchOpen = false" />
  <DocViewer />
</template>

<script setup>
import { ref, watch, onMounted } from 'vue'
import { fetchJSON, setUnauthorizedHandler } from './api/client'
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
import RunWorkbench from './components/RunWorkbench.vue'

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
const runWorkbenchOpen = ref(false)
const runMode = ref('research')
const features = ref({ research: true, medical: false, farm: false, experiments: false })
const sidebarOpen = ref(false)
function openRuns(mode) { runMode.value = mode; runWorkbenchOpen.value = true }

// 401 → 清 token + 弹登录层 + 中断在飞的对话
setUnauthorizedHandler(() => {
  chat.abortInflight()
  runWorkbenchOpen.value = false
  auth.logout()
})

function initApp() {
  docs.loadLibrary()
  skills.loadInstalled()
  fetchJSON('/api/status').then(status => {
    if (status.features) features.value = { ...features.value, ...status.features }
  }).catch(() => {})
}

watch(() => auth.loggedIn, (v) => {
  if (v) initApp()
  else { sessions.reset(); docs.reset(); skills.reset(); evaluation.reset() }
})

onMounted(() => { if (auth.loggedIn) initApp() })
</script>
