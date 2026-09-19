// stores/skills.js — Skill 广场（已安装 + marketplace）。
import { defineStore } from 'pinia'
import { fetchJSON } from '../api/client'

export const useSkills = defineStore('skills', {
  state: () => ({
    installed: [],
    featured: [],
    github: [],
    hubStatus: '',
    loadingMarket: false,
  }),
  getters: {
    enabledCount: (s) => s.installed.filter(x => x.enabled).length,
  },
  actions: {
    async loadInstalled() {
      try {
        const data = await fetchJSON('/api/skills/installed')
        this.installed = (data && data.skills) || []
      } catch { this.installed = [] }
    },
    async loadMarketplace() {
      this.loadingMarket = true
      try {
        const data = await fetchJSON('/api/skills/marketplace')
        this.featured = data.featured || []
        this.github = data.hub_status === 'ok' ? (data.github || []) : []
        this.hubStatus = data.hub_status || ''
      } catch {
        this.github = []; this.hubStatus = 'error'
      } finally { this.loadingMarket = false }
    },
    async install(skillId) {
      const res = await fetchJSON('/api/skills/install', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ skill_id: skillId }),
      })
      if (res.ok) await this.loadInstalled()
      return res.ok
    },
    async uninstall(skillId) {
      await fetchJSON('/api/skills/uninstall', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ skill_id: skillId }),
      })
      await this.loadInstalled()
    },
    async toggle(skillId, enabled) {
      await fetchJSON('/api/skills/toggle', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ skill_id: skillId, enabled }),
      })
      await this.loadInstalled()
    },
    reset() { this.installed = []; this.featured = []; this.github = []; this.hubStatus = '' },
  },
})
