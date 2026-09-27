import { useState } from 'react'
import { useAuth } from '../stores/auth'

export default function AuthModal() {
  const auth = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')

  async function submit() {
    const ok = await auth.submit(username.trim(), password)
    if (ok) setPassword('')
  }

  return (
    <div className="auth-overlay show">
      <div className="auth-card">
        <div className="auth-title">AGI-saber</div>
        <div className="auth-sub">{auth.mode === 'login' ? '登录以使用专属记忆' : '创建一个新账号'}</div>

        <div className="auth-tabs">
          <div className={`auth-tab${auth.mode === 'login' ? ' active' : ''}`} onClick={() => auth.setMode('login')}>登录</div>
          <div className={`auth-tab${auth.mode === 'register' ? ' active' : ''}`} onClick={() => auth.setMode('register')}>注册</div>
        </div>

        <div className="auth-field">
          <label>用户名</label>
          <input
            type="text"
            value={username}
            autoComplete="username"
            placeholder="3-32 字符"
            onChange={e => setUsername(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') submit() }}
          />
        </div>
        <div className="auth-field">
          <label>密码</label>
          <input
            type="password"
            value={password}
            autoComplete="current-password"
            placeholder="至少 8 位"
            onChange={e => setPassword(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') submit() }}
          />
        </div>

        <div className={`auth-error${auth.error ? ' show' : ''}`}>{auth.error}</div>

        <button className="auth-submit" disabled={auth.submitting} onClick={submit}>
          {auth.submitting ? '处理中...' : (auth.mode === 'login' ? '登录' : '注册')}
        </button>
      </div>
    </div>
  )
}
