// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * 顶栏用户菜单（2026-11-15，团队共享第一步）。
 *
 * - 显示当前账号与角色；退出登录；
 * - 管理员可打开「成员管理」：建号、停用/启用、重置密码。
 *
 * 认证关闭时（单机模式）整个组件不渲染，工作台保持原样。
 */
import { useEffect, useState } from 'react'
import { createUser, getAuthStatus, listUsers, logout, patchUser } from './api'
import type { AuthUser } from './types'

function describe(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

export default function UserMenu() {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [open, setOpen] = useState(false)
  const [membersOpen, setMembersOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    void getAuthStatus()
      .then((status) => setUser(status.user))
      .catch(() => setUser(null))
  }, [])

  if (!user) return null
  const name = user.display_name || user.username

  const doLogout = async () => {
    try {
      await logout()
    } catch (err) {
      setError(describe(err))
      return
    }
    // 退出后强制回到「工作台入口」：防止欢迎页还没看过时 reload 又落回欢迎页
    window.location.replace('/?welcome=0')
  }

  return (
    <div className="user-menu">
      <button
        className="user-chip"
        title={`${name}（@${user.username}）· ${user.role === 'admin' ? '管理员' : '成员'}`}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="user-avatar" aria-hidden="true">
          {name.slice(0, 1)}
        </span>
        <span className="user-name">{name}</span>
        <span className={`user-role ${user.role}`}>{user.role === 'admin' ? '管理员' : '成员'}</span>
      </button>

      {open && (
        <>
          <div className="user-menu-mask" onClick={() => setOpen(false)} />
          <div className="user-dropdown" role="menu">
            <div className="user-dropdown-meta">
              <b>{name}</b>
              <span>@{user.username}</span>
            </div>
            {user.role === 'admin' && (
              <button
                role="menuitem"
                onClick={() => {
                  setOpen(false)
                  setMembersOpen(true)
                }}
              >
                成员管理
              </button>
            )}
            <button role="menuitem" onClick={() => void doLogout()}>
              退出登录
            </button>
          </div>
        </>
      )}

      {error && <span className="user-menu-error">{error}</span>}

      {membersOpen && <MembersPanel onClose={() => setMembersOpen(false)} currentUserId={user.id} />}
    </div>
  )
}

function MembersPanel({ onClose, currentUserId }: { onClose: () => void; currentUserId: string }) {
  const [rows, setRows] = useState<AuthUser[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [username, setUsername] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<'member' | 'admin'>('member')
  const [creating, setCreating] = useState(false)

  const load = async () => {
    setError(null)
    try {
      setRows(await listUsers())
    } catch (err) {
      setError(describe(err))
    }
  }

  useEffect(() => {
    void load()
  }, [])

  const add = async () => {
    if (!username.trim() || password.length < 8) {
      setError('用户名不能为空，密码至少 8 位')
      return
    }
    setCreating(true)
    setError(null)
    try {
      await createUser({ username: username.trim(), password, displayName: displayName.trim(), role })
      setUsername('')
      setDisplayName('')
      setPassword('')
      await load()
    } catch (err) {
      setError(describe(err))
    } finally {
      setCreating(false)
    }
  }

  const toggle = async (row: AuthUser) => {
    setBusyId(row.id)
    setError(null)
    try {
      await patchUser(row.id, { isActive: !row.is_active })
      await load()
    } catch (err) {
      setError(describe(err))
    } finally {
      setBusyId(null)
    }
  }

  const resetPassword = async (row: AuthUser) => {
    const next = window.prompt(`给 ${row.display_name || row.username} 设置新密码（至少 8 位）：`)
    if (!next) return
    if (next.length < 8) {
      setError('密码至少 8 位')
      return
    }
    setBusyId(row.id)
    setError(null)
    try {
      await patchUser(row.id, { password: next })
      window.alert('密码已重置')
    } catch (err) {
      setError(describe(err))
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="proposal-backdrop" role="presentation" onPointerDown={onClose}>
      <div
        className="member-panel"
        role="dialog"
        aria-modal="true"
        aria-label="成员管理"
        onPointerDown={(event) => event.stopPropagation()}
      >
        <header className="member-head">
          <strong>成员管理</strong>
          <span className="muted">账号只在这台服务器上；停用后旧登录立即失效</span>
          <span className="spacer" />
          <button className="close" onClick={onClose} aria-label="关闭">
            ✕
          </button>
        </header>

        {error && <div className="banner error member-error">{error}</div>}

        <section className="member-add">
          <h3>新增成员</h3>
          <div className="member-add-row">
            <input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              placeholder="用户名（登录用）"
            />
            <input
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              placeholder="显示名（可选）"
            />
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="密码（至少 8 位）"
            />
            <select value={role} onChange={(event) => setRole(event.target.value as 'member' | 'admin')}>
              <option value="member">成员</option>
              <option value="admin">管理员</option>
            </select>
            <button className="primary small" disabled={creating} onClick={() => void add()}>
              {creating ? '创建中…' : '创建'}
            </button>
          </div>
        </section>

        <section className="member-list">
          <h3>已有成员（{rows?.length ?? '…'}）</h3>
          {rows === null && <p className="sum-muted">载入中…</p>}
          <ul>
            {(rows ?? []).map((row) => (
              <li key={row.id} className={row.is_active ? '' : 'inactive'}>
                <span className="member-avatar">{(row.display_name || row.username).slice(0, 1)}</span>
                <span className="member-name">
                  <b>{row.display_name || row.username}</b>
                  <em>@{row.username}</em>
                </span>
                <span className={`user-role ${row.role}`}>
                  {row.role === 'admin' ? '管理员' : '成员'}
                </span>
                {!row.is_active && <span className="member-off">已停用</span>}
                {row.id === currentUserId && <span className="member-self">当前账号</span>}
                <span className="spacer" />
                <button
                  className="ghost small"
                  disabled={busyId === row.id || row.id === currentUserId}
                  onClick={() => void toggle(row)}
                  title={row.id === currentUserId ? '不能停用自己' : undefined}
                >
                  {row.is_active ? '停用' : '启用'}
                </button>
                <button
                  className="ghost small"
                  disabled={busyId === row.id}
                  onClick={() => void resetPassword(row)}
                >
                  重置密码
                </button>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  )
}
