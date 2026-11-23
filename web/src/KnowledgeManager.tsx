// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * 知识库操作台（2026-11-12 工作台改版）。
 *
 * 侧栏只负责「看」；真正的整理动作集中在这里：
 * - 自由建组 / 建子组 / 改名 / 删组（删组不删文献，子组上提）；
 * - 给文献分组（一棵树上勾选任意分组，多对多）；
 * - 备注（只属于用户的一句话，不进检索/AI prompt）；
 * - 收藏（★ 标记，对应侧栏「收藏夹」虚拟视图）。
 *
 * 数据来源都是现有 collection / paper 接口，没有新增写语义；只是第一次有了集中操作的地方。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ReactElement } from 'react'
import {
  addPaperToCollection,
  createCollection,
  deleteCollection,
  fetchCollections,
  listPapers,
  patchPaper,
  removePaperFromCollection,
  updateCollection,
} from './api'
import type { Collection, Paper } from './types'

interface Props {
  onOpenPaper: (paper: Paper) => void
  onToast: (message: string) => void
  /** 分组/收藏/备注变化后，通知外层刷新侧栏与文献列表 */
  onChanged: () => void
  onClose: () => void
}

const SELECT_ALL = '__all__'
const SELECT_FAV = '__favorites__'

function collectionTotals(collections: Collection[]): Map<string, number> {
  const byParent = new Map<string | null, Collection[]>()
  for (const col of collections) {
    const list = byParent.get(col.parent_id) ?? []
    list.push(col)
    byParent.set(col.parent_id, list)
  }
  // 后序累加：子集合的文献也算进父集合
  const totals = new Map<string, number>()
  const walk = (id: string, seen: Set<string>): number => {
    if (seen.has(id)) return 0
    seen.add(id)
    const direct = collections.find((c) => c.id === id)?.paper_count ?? 0
    let sum = direct
    for (const child of byParent.get(id) ?? []) sum += walk(child.id, seen)
    totals.set(id, sum)
    return sum
  }
  for (const col of collections) if (col.parent_id === null) walk(col.id, new Set())
  return totals
}

function childrenOf(collections: Collection[], parentId: string | null): Collection[] {
  return collections
    .filter((c) => c.parent_id === parentId)
    .sort((a, b) => a.sort_order - b.sort_order || a.name.localeCompare(b.name))
}

export default function KnowledgeManager({ onOpenPaper, onToast, onChanged, onClose }: Props) {
  const [collections, setCollections] = useState<Collection[]>([])
  const [papers, setPapers] = useState<Paper[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<string>(SELECT_ALL)
  const [query, setQuery] = useState('')
  const [editingNote, setEditingNote] = useState<string | null>(null)
  const [noteDraft, setNoteDraft] = useState('')
  const [busyId, setBusyId] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [cols, rows] = await Promise.all([fetchCollections(), listPapers()])
      setCollections(cols)
      setPapers(rows)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const totals = useMemo(() => collectionTotals(collections), [collections])
  const favoriteCount = useMemo(() => papers.filter((p) => p.favorite).length, [papers])

  const descendantIds = useCallback(
    (id: string): Set<string> => {
      const out = new Set<string>([id])
      let frontier = [id]
      while (frontier.length > 0) {
        const next: string[] = []
        for (const parent of frontier) {
          for (const child of collections.filter((c) => c.parent_id === parent)) {
            if (!out.has(child.id)) {
              out.add(child.id)
              next.push(child.id)
            }
          }
        }
        frontier = next
      }
      return out
    },
    [collections],
  )

  const visiblePapers = useMemo(() => {
    const keyword = query.trim().toLowerCase()
    return papers.filter((paper) => {
      if (selected === SELECT_FAV && !paper.favorite) return false
      if (selected !== SELECT_ALL && selected !== SELECT_FAV) {
        const ids = descendantIds(selected)
        if (!(paper.collection_ids ?? []).some((id) => ids.has(id))) return false
      }
      if (!keyword) return true
      return (
        paper.title.toLowerCase().includes(keyword) ||
        (paper.note ?? '').toLowerCase().includes(keyword) ||
        (paper.venue ?? '').toLowerCase().includes(keyword)
      )
    })
  }, [papers, query, selected, descendantIds])

  const selectedCollection =
    selected !== SELECT_ALL && selected !== SELECT_FAV
      ? collections.find((c) => c.id === selected) ?? null
      : null

  const refresh = async () => {
    await load()
    onChanged()
  }

  const createGroup = async () => {
    const name = window.prompt(
      selectedCollection ? `在「${selectedCollection.name}」下新建子组名称` : '新建分组名称',
    )
    if (!name?.trim()) return
    try {
      await createCollection(name.trim(), selectedCollection?.id ?? null)
      await refresh()
      onToast(`已新建分组「${name.trim()}」`)
    } catch (err) {
      onToast(err instanceof Error ? `新建失败：${err.message}` : '新建失败')
    }
  }

  const renameGroup = async () => {
    if (!selectedCollection) return
    const name = window.prompt('新的分组名称', selectedCollection.name)
    if (!name?.trim() || name.trim() === selectedCollection.name) return
    try {
      await updateCollection(selectedCollection.id, { name: name.trim() })
      await refresh()
      onToast('已改名')
    } catch (err) {
      onToast(err instanceof Error ? `改名失败：${err.message}` : '改名失败')
    }
  }

  const removeGroup = async () => {
    if (!selectedCollection) return
    if (
      !window.confirm(
        `删除分组「${selectedCollection.name}」？只删分组，文献不会丢；子分组会上提一层。`,
      )
    ) {
      return
    }
    try {
      await deleteCollection(selectedCollection.id)
      setSelected(SELECT_ALL)
      await refresh()
      onToast('已删除分组（文献保留）')
    } catch (err) {
      onToast(err instanceof Error ? `删除失败：${err.message}` : '删除失败')
    }
  }

  const toggleFavorite = async (paper: Paper) => {
    if (busyId) return
    setBusyId(paper.id)
    try {
      const updated = await patchPaper(paper.id, { favorite: !paper.favorite })
      setPapers((prev) => prev.map((p) => (p.id === paper.id ? updated : p)))
      onChanged()
      onToast(updated.favorite ? '已加入收藏夹' : '已移出收藏夹')
    } catch (err) {
      onToast(err instanceof Error ? `失败：${err.message}` : '失败')
    } finally {
      setBusyId(null)
    }
  }

  const saveNote = async (paper: Paper) => {
    if (busyId) return
    setBusyId(paper.id)
    try {
      const updated = await patchPaper(paper.id, { note: noteDraft })
      setPapers((prev) => prev.map((p) => (p.id === paper.id ? updated : p)))
      setEditingNote(null)
      onChanged()
      onToast('备注已保存')
    } catch (err) {
      onToast(err instanceof Error ? `保存失败：${err.message}` : '保存失败')
    } finally {
      setBusyId(null)
    }
  }

  const toggleGroup = async (paper: Paper, collectionId: string) => {
    if (busyId) return
    setBusyId(paper.id)
    try {
      const inGroup = (paper.collection_ids ?? []).includes(collectionId)
      if (inGroup) await removePaperFromCollection(collectionId, paper.id)
      else await addPaperToCollection(collectionId, paper.id)
      const rows = await listPapers()
      setPapers(rows)
      await refresh()
      onToast(inGroup ? '已移出分组' : '已加入分组')
    } catch (err) {
      onToast(err instanceof Error ? `操作失败：${err.message}` : '操作失败')
    } finally {
      setBusyId(null)
    }
  }

  const renderTree = (parentId: string | null, depth: number): ReactElement[] =>
    childrenOf(collections, parentId).flatMap((col) => {
      const row = (
        <li key={col.id}>
          <button
            className={`km-group ${selected === col.id ? 'on' : ''}`}
            style={{ paddingLeft: `${10 + depth * 14}px` }}
            onClick={() => setSelected(col.id)}
            title={col.source === 'zotero' ? 'Zotero 镜像分组（只读，重新导入会同步）' : '手动分组'}
          >
            <span className="km-group-name">{col.name}</span>
            <span className="km-group-count">{totals.get(col.id) ?? 0}</span>
            {col.source === 'zotero' && <span className="km-group-src">Z</span>}
          </button>
        </li>
      )
      return [row, ...renderTree(col.id, depth + 1)]
    })

  return (
    <div className="km">
      <header className="km-head">
        <div>
          <h2>知识库操作台</h2>
          <p className="km-sub">
            分组是自由的多对多关系；备注只属于你（不进检索/AI）；星标文献进「收藏夹」。
          </p>
        </div>
        <button className="ghost" onClick={onClose}>
          返回工作台
        </button>
      </header>

      {loading && <p className="km-muted">载入知识库…</p>}
      {error && (
        <div className="banner error">
          载入失败：{error}
          <button className="link" onClick={() => void load()}>
            重试
          </button>
        </div>
      )}

      {!loading && !error && (
        <div className="km-body">
          <aside className="km-groups">
            <div className="km-groups-head">
              <span>分组</span>
              <button className="link" onClick={() => void createGroup()} title="新建分组">
                ＋
              </button>
            </div>
            <ul>
              <li>
                <button
                  className={`km-group ${selected === SELECT_ALL ? 'on' : ''}`}
                  onClick={() => setSelected(SELECT_ALL)}
                >
                  <span className="km-group-name">全部文献</span>
                  <span className="km-group-count">{papers.length}</span>
                </button>
              </li>
              <li>
                <button
                  className={`km-group fav ${selected === SELECT_FAV ? 'on' : ''}`}
                  onClick={() => setSelected(SELECT_FAV)}
                  title="收藏夹（虚拟分组）"
                >
                  <span className="km-group-name">★ 收藏夹</span>
                  <span className="km-group-count">{favoriteCount}</span>
                </button>
              </li>
              {renderTree(null, 0)}
            </ul>
            {selectedCollection && selectedCollection.source !== 'zotero' && (
              <div className="km-group-actions">
                <button className="ghost small" onClick={() => void renameGroup()}>
                  改名
                </button>
                <button className="ghost small danger" onClick={() => void removeGroup()}>
                  删除分组
                </button>
              </div>
            )}
            {selectedCollection?.source === 'zotero' && (
              <p className="km-muted small">Zotero 镜像分组只读；要改请在 Zotero 里改后重新导入。</p>
            )}
            <p className="km-muted small">选中一个分组后点「＋」会在它下面建子组。</p>
          </aside>

          <section className="km-papers">
            <div className="km-toolbar">
              <input
                className="km-search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="搜索标题 / 备注 / 期刊…"
                spellCheck={false}
              />
              <span className="km-count">{visiblePapers.length} 篇</span>
              <button
                className={`chip ${selected === SELECT_FAV ? 'on' : ''}`}
                onClick={() => setSelected(selected === SELECT_FAV ? SELECT_ALL : SELECT_FAV)}
                title="只看收藏"
              >
                ★ 收藏
              </button>
            </div>

            {visiblePapers.length === 0 ? (
              <div className="empty km-empty">
                <h3>这里还没有文献</h3>
                <p>
                  {selected === SELECT_FAV
                    ? '在文献行点 ☆ 收藏，或先在侧栏把文献整理进来。'
                    : '换一个分组，或把文献加入当前分组。'}
                </p>
              </div>
            ) : (
              <ul className="km-list">
                {visiblePapers.map((paper) => {
                  const inGroups = paper.collection_ids ?? []
                  const editing = editingNote === paper.id
                  return (
                    <li className="km-paper" key={paper.id}>
                      <div className="km-paper-head">
                        <button
                          className={`km-star ${paper.favorite ? 'on' : ''}`}
                          disabled={busyId === paper.id}
                          onClick={() => void toggleFavorite(paper)}
                          title={paper.favorite ? '移出收藏夹' : '加入收藏夹'}
                          aria-label={paper.favorite ? '移出收藏夹' : '加入收藏夹'}
                        >
                          {paper.favorite ? '★' : '☆'}
                        </button>
                        <div className="km-paper-title-wrap">
                          <div className="km-paper-title">{paper.title}</div>
                          <div className="km-paper-meta">
                            {[paper.year, paper.venue].filter(Boolean).join(' · ') || '未著录'}
                            {' · '}
                            {paper.figure_count} 图
                            {paper.has_version === false && ' · 仅元数据'}
                            {paper.metadata_source === 'zotero' && ' · Zotero'}
                          </div>
                        </div>
                        <button className="ghost small" onClick={() => onOpenPaper(paper)}>
                          打开
                        </button>
                      </div>

                      {paper.tags_json && paper.tags_json.length > 0 && (
                        <div className="km-tags">
                          {paper.tags_json.slice(0, 8).map((tag) => (
                            <span className="tag" key={tag}>
                              {tag}
                            </span>
                          ))}
                        </div>
                      )}

                      <div className="km-groups-pick">
                        <span className="km-pick-label">分组</span>
                        {collections.length === 0 && <span className="km-muted">还没有分组</span>}
                        {collections.map((col) => (
                          <button
                            key={col.id}
                            className={`km-chip ${inGroups.includes(col.id) ? 'on' : ''}`}
                            disabled={busyId === paper.id}
                            onClick={() => void toggleGroup(paper, col.id)}
                            title={inGroups.includes(col.id) ? '点击移出' : '点击加入'}
                          >
                            {col.name}
                          </button>
                        ))}
                      </div>

                      <div className="km-note">
                        <span className="km-pick-label">备注</span>
                        {editing ? (
                          <>
                            <textarea
                              className="km-note-input"
                              value={noteDraft}
                              onChange={(e) => setNoteDraft(e.target.value)}
                              rows={2}
                              maxLength={20000}
                              placeholder="比如：重点看第 3 节实验设置；方法与第 2 篇对照……"
                              autoFocus
                            />
                            <div className="km-note-actions">
                              <button
                                className="primary small"
                                disabled={busyId === paper.id}
                                onClick={() => void saveNote(paper)}
                              >
                                保存
                              </button>
                              <button className="ghost small" onClick={() => setEditingNote(null)}>
                                取消
                              </button>
                            </div>
                          </>
                        ) : (
                          <>
                            <button
                              className="km-note-view"
                              onClick={() => {
                                setEditingNote(paper.id)
                                setNoteDraft(paper.note ?? '')
                              }}
                              title="点击编辑备注"
                            >
                              {paper.note?.trim() ? paper.note : '＋ 添加备注'}
                            </button>
                          </>
                        )}
                      </div>
                    </li>
                  )
                })}
              </ul>
            )}
          </section>
        </div>
      )}
    </div>
  )
}
