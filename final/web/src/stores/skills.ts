// stores/skills.ts — Skill 广场（已安装 + marketplace）。Pinia → Zustand 移植。
import { create } from 'zustand'
import { fetchJSON } from '../api/client'

export interface SkillsState {
  installed: any[]
  featured: any[]
  github: any[]
  hubStatus: string
  loadingMarket: boolean
  loadInstalled(): Promise<void>
  loadMarketplace(): Promise<void>
  install(skillId: string): Promise<boolean>
  uninstall(skillId: string): Promise<void>
  toggle(skillId: string, enabled: boolean): Promise<void>
  reset(): void
}

export const useSkills = create<SkillsState>()((set, get) => ({
  installed: [],
  featured: [],
  github: [],
  hubStatus: '',
  loadingMarket: false,
  async loadInstalled() {
    try {
      const data = await fetchJSON('/api/skills/installed')
      set({ installed: (data && data.skills) || [] })
    } catch { set({ installed: [] }) }
  },
  async loadMarketplace() {
    set({ loadingMarket: true })
    try {
      const data = await fetchJSON('/api/skills/marketplace')
      set({
        featured: data.featured || [],
        github: data.hub_status === 'ok' ? (data.github || []) : [],
        hubStatus: data.hub_status || '',
      })
    } catch {
      set({ github: [], hubStatus: 'error' })
    } finally { set({ loadingMarket: false }) }
  },
  async install(skillId) {
    const res = await fetchJSON('/api/skills/install', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ skill_id: skillId }),
    })
    if (res.ok) await get().loadInstalled()
    return !!res.ok
  },
  async uninstall(skillId) {
    await fetchJSON('/api/skills/uninstall', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ skill_id: skillId }),
    })
    await get().loadInstalled()
  },
  async toggle(skillId, enabled) {
    await fetchJSON('/api/skills/toggle', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ skill_id: skillId, enabled }),
    })
    await get().loadInstalled()
  },
  reset() { set({ installed: [], featured: [], github: [], hubStatus: '' }) },
}))

export const selectEnabledCount = (s: SkillsState) => s.installed.filter(x => x.enabled).length
