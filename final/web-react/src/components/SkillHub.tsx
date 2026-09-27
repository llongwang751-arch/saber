import { useEffect, useState } from 'react'
import { useSkills } from '../stores/skills'

// 技能广场：已安装（开关 + 卸载）、官方精选、GitHub 热门。
// Vue 版直接改按钮 DOM；React 版用每个技能的安装状态驱动按钮显示，UX 等价。
type InstallState = 'idle' | 'loading' | 'done'

export default function SkillHub({ onClose }: { onClose: () => void }) {
  const skills = useSkills()
  const [installStates, setInstallStates] = useState<Record<string, InstallState>>({})

  useEffect(() => {
    useSkills.getState().loadInstalled()
    useSkills.getState().loadMarketplace()
  }, [])

  async function install(id: string) {
    setInstallStates(s => ({ ...s, [id]: 'loading' }))
    let ok = false
    try { ok = await skills.install(id) } catch { ok = false }
    if (ok) setInstallStates(s => ({ ...s, [id]: 'done' }))
    else {
      setInstallStates(s => ({ ...s, [id]: 'idle' }))
      alert('安装失败')
    }
  }

  function installButton(id: string) {
    const state = installStates[id] || 'idle'
    return (
      <button
        className="s-btn"
        onClick={() => install(id)}
        disabled={state === 'loading'}
      >
        {state === 'loading' ? '安装中…' : state === 'done' ? '已安装' : '安装'}
      </button>
    )
  }

  return (
    <div className="doc-viewer-backdrop open" onClick={e => { if (e.target === e.currentTarget) onClose() }}>
      <section className="doc-viewer" role="dialog" aria-modal="true">
        <div className="doc-viewer-head">
          <div className="doc-viewer-title-wrap">
            <div className="doc-viewer-kicker">技能广场</div>
            <div className="doc-viewer-title">办公技能 · 安装并开启后主循环自动调度</div>
            <div className="doc-viewer-meta"></div>
          </div>
          <div className="doc-viewer-actions">
            <button className="doc-viewer-close" type="button" aria-label="关闭" onClick={onClose}>×</button>
          </div>
        </div>
        <div className="doc-viewer-body">
          <div className="skill-section-title">已安装（开关控制是否参与主循环）</div>
          <div className="skill-grid">
            {!skills.installed.length && <div className="s-meta">还没有安装任何技能，可从下方广场选择安装。</div>}
            {skills.installed.map(s => (
              <div key={s.id} className="skill-card">
                <div className="s-name">{s.name}</div>
                <div className="s-desc">{s.description || ''}</div>
                <div className="s-foot">
                  <label className="skill-switch">
                    <input
                      type="checkbox"
                      checked={!!s.enabled}
                      onChange={e => skills.toggle(s.id, e.target.checked)}
                    />
                    <span className="track"></span>
                  </label>
                  <button className="s-btn secondary" onClick={() => skills.uninstall(s.id)}>卸载</button>
                </div>
              </div>
            ))}
          </div>

          <div className="picker-divider"></div>
          <div className="skill-section-title">官方精选</div>
          <div className="skill-grid">
            {skills.loadingMarket && <div className="s-meta">加载中…</div>}
            {skills.featured.map(m => (
              <div key={m.id} className="skill-card">
                <div className="s-name">{m.name}</div>
                <div className="s-desc">{m.description || ''}</div>
                <div className="s-foot">
                  <span className="s-meta">官方内置</span>
                  {installButton(m.id)}
                </div>
              </div>
            ))}
          </div>

          <div className="picker-divider"></div>
          <div className="skill-section-title">GitHub 热门（按星标数排序，前 20 项）</div>
          <div className="skill-grid">
            {skills.hubStatus === 'ok' && skills.github.map((m: any) => (
              <div key={m.id} className="skill-card">
                <div className="s-name">{m.name}</div>
                <div className="s-desc">{m.description || ''}</div>
                <div className="s-foot">
                  <span className="s-meta">★ {m.stars || 0} · <a href={m.source_url} target="_blank" rel="noopener noreferrer">GitHub</a></span>
                  {installButton(m.id)}
                </div>
              </div>
            ))}
            {skills.hubStatus === 'loading' && <div className="s-meta">正在获取 GitHub 热门，不影响官方技能使用…</div>}
            {skills.hubStatus === 'disabled' && <div className="s-meta">GitHub 广场已关闭（可在 config.skillhub.enabled 开启）</div>}
            {skills.hubStatus !== 'ok' && skills.hubStatus !== 'loading' && skills.hubStatus !== 'disabled' && (
              <div className="s-meta">GitHub 热门暂不可用（限流或网络问题），稍后重试</div>
            )}
          </div>
        </div>
      </section>
    </div>
  )
}
