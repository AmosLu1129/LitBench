// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * 登录闸门（2026-11-15，团队共享第一步）。
 *
 * 行为：
 * - 认证关闭（LITBENCH_AUTH=0）→ 直接渲染工作台，单机开发不受影响；
 * - 认证打开且零用户 → 显示「初始化管理员」（仅首次）；
 * - 认证打开且已有用户 → 登录；登录成功后才挂载工作台。
 *
 * 登录态是服务端 session cookie（HttpOnly），前端不存 token。
 */
import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { getAuthStatus, login, setupAdmin } from './api'
import type { AuthStatus } from './types'

interface Props {
  children: ReactNode
}

function describe(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

export default function AuthGate({ children }: Props) {
  const [status, setStatus] = useState<AuthStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [username, setUsername] = useState('admin')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')

  const refresh = async () => {
    setLoading(true)
    setError(null)
    try {
      setStatus(await getAuthStatus())
    } catch (err) {
      setError(describe(err))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void refresh()
  }, [])

  const submit = async () => {
    if (!status) return
    setError(null)
    if (!username.trim() || !password) {
      setError('请填用户名和密码')
      return
    }
    if (!status.initialized) {
      if (password.length < 8) {
        setError('密码至少 8 位')
        return
      }
      if (password !== confirm) {
        setError('两次输入的密码不一致')
        return
      }
    }
    setBusy(true)
    try {
      const next = status.initialized
        ? await login(username.trim(), password)
        : await setupAdmin({ username: username.trim(), password, displayName: displayName.trim() })
      setStatus(next)
      setPassword('')
      setConfirm('')
    } catch (err) {
      setError(describe(err))
    } finally {
      setBusy(false)
    }
  }

  if (loading) {
    return (
      <div className="auth-screen">
        <div className="auth-loading">正在检查登录状态…</div>
      </div>
    )
  }
  if (error && !status) {
    return (
      <div className="auth-screen">
        <div className="auth-card">
          <h2>连不上服务端</h2>
          <p className="auth-hint">{error}</p>
          <button className="primary" onClick={() => void refresh()}>
            重试
          </button>
        </div>
      </div>
    )
  }
  if (!status || !status.auth_enabled || status.user) {
    return <>{children}</>
  }

  const setupMode = !status.initialized
  return (
    <div className="auth-screen">
      <div className="auth-card" role="form" aria-label={setupMode ? '初始化管理员' : '登录'}>
        <div className="auth-brand">
          <img src="/brand/litbench-emblem-90.png" alt="" aria-hidden="true" />
          <div>
            <strong>LitBench</strong>
            <span>文献证据工作台</span>
          </div>
        </div>
        <h2>{setupMode ? '首次使用：创建管理员' : '登录'}</h2>
        <p className="auth-hint">
          {setupMode
            ? '这是这台服务器的第一个账号；创建后可在「成员」里给团队建号。'
            : '用管理员或团队成员账号登录。'}
        </p>

        <label className="auth-field">
          <span>用户名</span>
          <input
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
            autoFocus
          />
        </label>
        {setupMode && (
          <label className="auth-field">
            <span>显示名（可选）</span>
            <input
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              placeholder="例如：王老师"
            />
          </label>
        )}
        <label className="auth-field">
          <span>密码</span>
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete={setupMode ? 'new-password' : 'current-password'}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !setupMode) void submit()
            }}
          />
        </label>
        {setupMode && (
          <label className="auth-field">
            <span>确认密码</span>
            <input
              type="password"
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
              autoComplete="new-password"
              onKeyDown={(event) => {
                if (event.key === 'Enter') void submit()
              }}
            />
          </label>
        )}

        {error && <div className="auth-error">{error}</div>}
        <button className="primary auth-submit" disabled={busy} onClick={() => void submit()}>
          {busy ? '请稍候…' : setupMode ? '创建并进入' : '登录'}
        </button>
        <p className="auth-foot">自部署 · 账号只保存在这台服务器上</p>
      </div>
    </div>
  )
}
