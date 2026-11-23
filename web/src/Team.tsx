// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * 团队共享（M7 第二步，2026-11-15）的三个界面部件：
 *
 * - `WorkspaceSwitcher`：顶栏「个人库 / 工作组」切换；
 * - `WorkgroupPanel`：建组、看成员、按用户名加人、移除/退出/解散；
 * - `ShareMenu`：文献卡片/详情上的「共享到工作组」勾选菜单。
 *
 * 认证关闭（单机模式）时 App 不渲染切换器，这三个部件也不会出现。
 */
import { useEffect, useState } from 'react'
import {
  addWorkgroupMember,
  createWorkgroup,
  deleteWorkgroup,
  getWorkgroup,
  removeWorkgroupMember,
  sharePaper,
  unsharePaper,
  updateWorkgroup,
} from './api'
import type {
  AuthUser,
  Paper,
  Workgroup,
  WorkgroupDetail,
} from './types'

function describe(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

// ---------------------------------------------------------------------------
// 顶栏：个人库 / 工作组切换
// ---------------------------------------------------------------------------
export function WorkspaceSwitcher({
  current,
  workgroups,
  onSelect,
  onManage,
}: {
  current: string
  workgroups: Workgroup[]
  onSelect: (workspace: string) => void
  onManage: (workgroupId: string | null) => void
}) {
  const [open, setOpen] = useState(false)
  const group = workgroups.find((item) => item.id === current) ?? null

  return (
    <div className="ws-switcher">
      <button
        className={`ws-btn ${group ? 'team' : ''}`}
        onClick={() => setOpen((value) => !value)}
        title="切换个人库 / 工作组（团队库只显示成员主动共享的文献）"
      >
        <span className="ws-icon" aria-hidden="true">
          {group ? '组' : '私'}
        </span>
        <span className="ws-label">{group ? group.name : '个人库'}</span>
        <span className="ws-caret" aria-hidden="true">
          ▾
        </span>
      </button>

      {open && (
        <>
          <div className="ws-mask" onClick={() => setOpen(false)} />
          <div className="ws-menu" role="menu">
            <div className="ws-menu-head">数据范围</div>
            <button
              role="menuitem"
              className={`ws-item ${group ? '' : 'on'}`}
              onClick={() => {
                setOpen(false)
                onSelect('personal')
              }}
            >
              <span className="ws-dot">私</span>
              <span className="ws-item-text">
                <b>个人库</b>
                <em>只有我自己看得到</em>
              </span>
              {!group && <span className="ws-check">✓</span>}
            </button>
            {workgroups.map((item) => (
              <button
                key={item.id}
                role="menuitem"
                className={`ws-item ${group?.id === item.id ? 'on' : ''}`}
                onClick={() => {
                  setOpen(false)
                  onSelect(item.id)
                }}
              >
                <span className="ws-dot team">组</span>
                <span className="ws-item-text">
                  <b>{item.name}</b>
                  <em>
                    {item.member_count} 人 · {item.paper_count} 篇共享
                  </em>
                </span>
                {group?.id === item.id && <span className="ws-check">✓</span>}
              </button>
            ))}
            {workgroups.length === 0 && (
              <div className="ws-empty">还没有加入工作组；新建一个，把文献共享给组员。</div>
            )}
            <div className="ws-sep" />
            <button
              role="menuitem"
              onClick={() => {
                setOpen(false)
                onManage(null)
              }}
            >
              ＋ 新建工作组
            </button>
            <button
              role="menuitem"
              onClick={() => {
                setOpen(false)
                onManage(group?.id ?? workgroups[0]?.id ?? null)
              }}
            >
              管理工作组…
            </button>
          </div>
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 工作组管理浮层
// ---------------------------------------------------------------------------
export function WorkgroupPanel({
  open,
  initialGroupId,
  currentUser,
  workgroups,
  onClose,
  onChanged,
  onCurrentRemoved,
  onToast,
}: {
  open: boolean
  initialGroupId: string | null
  currentUser: AuthUser
  workgroups: Workgroup[]
  onClose: () => void
  onChanged: () => void
  /** 当前工作区对应的组被解散/退出时，App 会切回个人库 */
  onCurrentRemoved: (workgroupId: string) => void
  onToast: (message: string) => void
}) {
  const [selected, setSelected] = useState<string | null>(initialGroupId)
  const [detail, setDetail] = useState<WorkgroupDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [newName, setNewName] = useState('')
  const [newDesc, setNewDesc] = useState('')
  const [addName, setAddName] = useState('')

  useEffect(() => {
    if (!open) return
    setSelected(initialGroupId ?? workgroups[0]?.id ?? null)
    setError(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  useEffect(() => {
    if (!open || !selected) {
      setDetail(null)
      return
    }
    let alive = true
    void getWorkgroup(selected)
      .then((data) => {
        if (alive) setDetail(data)
      })
      .catch((err) => {
        if (alive) setError(describe(err))
      })
    return () => {
      alive = false
    }
  }, [open, selected])

  if (!open) return null

  const isAdmin = currentUser.role === 'admin'
  const myRole = detail?.role ?? workgroups.find((item) => item.id === selected)?.role ?? 'member'
  const canManage = isAdmin || myRole === 'owner'

  const refreshDetail = async () => {
    if (!selected) return
    setDetail(await getWorkgroup(selected))
  }

  const create = async () => {
    if (!newName.trim()) {
      setError('工作组名称不能为空')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const created = await createWorkgroup(newName.trim(), newDesc.trim())
      setNewName('')
      setNewDesc('')
      setSelected(created.id)
      onChanged()
      onToast(`工作组「${created.name}」已创建`)
    } catch (err) {
      setError(describe(err))
    } finally {
      setBusy(false)
    }
  }

  const addMember = async () => {
    if (!selected || !addName.trim()) return
    setBusy(true)
    setError(null)
    try {
      const member = await addWorkgroupMember(selected, addName.trim())
      setAddName('')
      await refreshDetail()
      onChanged()
      onToast(`已把 ${member.display_name || member.username} 加入工作组`)
    } catch (err) {
      setError(describe(err))
    } finally {
      setBusy(false)
    }
  }

  const removeMember = async (userId: string, name: string) => {
    if (!selected) return
    if (!window.confirm(`把 ${name} 移出这个工作组？`)) return
    setBusy(true)
    setError(null)
    try {
      await removeWorkgroupMember(selected, userId)
      await refreshDetail()
      onChanged()
      onToast(`已移除 ${name}`)
      if (userId === currentUser.id) {
        onCurrentRemoved(selected)
        setSelected(workgroups.find((item) => item.id !== selected)?.id ?? null)
      }
    } catch (err) {
      setError(describe(err))
    } finally {
      setBusy(false)
    }
  }

  const rename = async () => {
    if (!selected || !detail) return
    const next = window.prompt('工作组新名称：', detail.name)
    if (!next || !next.trim() || next.trim() === detail.name) return
    setBusy(true)
    setError(null)
    try {
      setDetail(await updateWorkgroup(selected, { name: next.trim() }))
      onChanged()
    } catch (err) {
      setError(describe(err))
    } finally {
      setBusy(false)
    }
  }

  const dissolve = async () => {
    if (!selected || !detail) return
    if (!window.confirm(`解散工作组「${detail.name}」？\n成员与共享关系会删除，文献本身不动。`)) return
    setBusy(true)
    setError(null)
    try {
      await deleteWorkgroup(selected)
      onCurrentRemoved(selected)
      onChanged()
      onToast('工作组已解散')
      setSelected(workgroups.find((item) => item.id !== selected)?.id ?? null)
    } catch (err) {
      setError(describe(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="proposal-backdrop" role="presentation" onPointerDown={onClose}>
      <div
        className="wg-panel"
        role="dialog"
        aria-modal="true"
        aria-label="工作组管理"
        onPointerDown={(event) => event.stopPropagation()}
      >
        <header className="member-head">
          <strong>工作组</strong>
          <span className="muted">团队库只显示成员主动共享的文献；图/表/摘要随文献可见</span>
          <span className="spacer" />
          <button className="close" onClick={onClose} aria-label="关闭">
            ✕
          </button>
        </header>

        {error && <div className="banner error member-error">{error}</div>}

        <div className="wg-body">
          <aside className="wg-side">
            <h3>我的工作组（{workgroups.length}）</h3>
            <ul className="wg-groups">
              {workgroups.map((item) => (
                <li key={item.id}>
                  <button
                    className={`wg-group ${selected === item.id ? 'on' : ''}`}
                    onClick={() => setSelected(item.id)}
                  >
                    <b>{item.name}</b>
                    <em>
                      {item.member_count} 人 · {item.paper_count} 篇共享
                    </em>
                  </button>
                </li>
              ))}
              {workgroups.length === 0 && <li className="muted">还没有工作组</li>}
            </ul>

            <div className="wg-create">
              <h3>新建工作组</h3>
              <input
                value={newName}
                onChange={(event) => setNewName(event.target.value)}
                placeholder="工作组名称（如：传热课题组）"
                maxLength={120}
              />
              <input
                value={newDesc}
                onChange={(event) => setNewDesc(event.target.value)}
                placeholder="说明（可选）"
                maxLength={200}
              />
              <button className="primary small" disabled={busy} onClick={() => void create()}>
                创建
              </button>
            </div>
          </aside>

          <section className="wg-main">
            {!selected || !detail ? (
              <div className="wg-placeholder">
                <h3>选一个工作组</h3>
                <p>左边是已有的组；也可以先建一个，再把文献共享进来。</p>
              </div>
            ) : (
              <>
                <div className="wg-title">
                  <div>
                    <h3>{detail.name}</h3>
                    <p className="muted">
                      {detail.description || '没有说明'} · 创建者 {detail.created_by_name || '—'}
                    </p>
                  </div>
                  <span className="wg-counts">
                    {detail.member_count} 位成员 · {detail.paper_count} 篇共享
                  </span>
                  <span className="spacer" />
                  {canManage && (
                    <button className="ghost small" disabled={busy} onClick={() => void rename()}>
                      改名
                    </button>
                  )}
                </div>

                <h4>成员</h4>
                <ul className="wg-members">
                  {detail.members.map((member) => (
                    <li key={member.user_id}>
                      <span className="member-avatar">
                        {(member.display_name || member.username).slice(0, 1)}
                      </span>
                      <span className="member-name">
                        <b>{member.display_name || member.username}</b>
                        <em>@{member.username}</em>
                      </span>
                      <span className={`user-role ${member.role === 'owner' ? 'admin' : 'member'}`}>
                        {member.role === 'owner' ? 'owner' : '成员'}
                      </span>
                      {member.user_id === currentUser.id && (
                        <span className="member-self">当前账号</span>
                      )}
                      <span className="spacer" />
                      {(canManage || member.user_id === currentUser.id) && (
                        <button
                          className="ghost small"
                          disabled={busy}
                          onClick={() =>
                            void removeMember(
                              member.user_id,
                              member.display_name || member.username,
                            )
                          }
                        >
                          {member.user_id === currentUser.id ? '退出' : '移除'}
                        </button>
                      )}
                    </li>
                  ))}
                </ul>

                {canManage && (
                  <div className="wg-add">
                    <input
                      value={addName}
                      onChange={(event) => setAddName(event.target.value)}
                      placeholder="按用户名加人（对方需已有账号）"
                      onKeyDown={(event) => {
                        if (event.key === 'Enter') void addMember()
                      }}
                    />
                    <button className="primary small" disabled={busy} onClick={() => void addMember()}>
                      加入工作组
                    </button>
                  </div>
                )}

                <div className="wg-actions">
                  {canManage && (
                    <button className="ghost small danger" disabled={busy} onClick={() => void dissolve()}>
                      解散工作组
                    </button>
                  )}
                  <p className="muted">
                    共享是文献级：组员能读共享文献的图/表/摘要，不能修改或再共享；
                    每个人的笔记、收藏、阅读状态各用各的。
                  </p>
                </div>
              </>
            )}
          </section>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// 文献共享菜单
// ---------------------------------------------------------------------------
export function ShareMenu({
  paper,
  workgroups,
  onChanged,
  label = false,
}: {
  paper: Paper
  workgroups: Workgroup[]
  onChanged: () => void
  /** true = 详情页按钮带文字；false = 卡片角标 */
  label?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const shared = new Set(paper.shared_workgroup_ids ?? [])

  if (paper.can_edit === false || workgroups.length === 0) return null

  const toggle = async (workgroupId: string) => {
    setBusy(workgroupId)
    setError(null)
    try {
      if (shared.has(workgroupId)) await unsharePaper(paper.id, workgroupId)
      else await sharePaper(paper.id, workgroupId)
      onChanged()
    } catch (err) {
      setError(describe(err))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className={`share-wrap ${label ? 'labeled' : ''}`}>
      <button
        className={`papercard-share ${shared.size > 0 ? 'on' : ''} ${label ? 'with-label' : ''}`}
        onClick={(event) => {
          event.stopPropagation()
          setOpen((value) => !value)
        }}
        title={
          shared.size > 0
            ? `已共享到 ${shared.size} 个工作组`
            : '共享到工作组（组员可以读，不能改）'
        }
      >
        <span aria-hidden="true">⤴</span>
        {label ? <span>共享</span> : shared.size > 0 ? <span>{shared.size}</span> : null}
      </button>

      {open && (
        <>
          <div
            className="share-mask"
            onClick={(event) => {
              event.stopPropagation()
              setOpen(false)
            }}
          />
          <div className="share-pop" onClick={(event) => event.stopPropagation()}>
            <div className="share-head">共享到工作组</div>
            {workgroups.map((group) => (
              <button
                key={group.id}
                className={`share-row ${shared.has(group.id) ? 'on' : ''}`}
                disabled={busy === group.id}
                onClick={() => void toggle(group.id)}
              >
                <span className="share-check">{shared.has(group.id) ? '✓' : ''}</span>
                <span className="share-name">{group.name}</span>
                <em>{group.member_count} 人</em>
              </button>
            ))}
            {error && <div className="share-error">{error}</div>}
            <div className="share-foot">组员可读；取消勾选后立即看不到</div>
          </div>
        </>
      )}
    </div>
  )
}
