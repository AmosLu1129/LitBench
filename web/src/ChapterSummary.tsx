// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * 章节级摘要（M2a）：左栏章节树 + 右栏摘要详情。
 *
 * 设计取舍：
 * - 摘要只认后端已落库的数据；这里不发任何「直接问模型」的请求，生成走 generate 接口
 *   （mock provider 也不花钱，但流程和真实模型一致：生成 → 证据校验 → 落库）。
 * - 证据块点击优先打开原文并高亮 bbox（复用 PdfViewer 的 figureId=null 模式）；
 *   没有 bbox 就退化成 toast 提示页码，不改后端。
 * - block_count=0 的是纯父节点：只显示「分组」，不允许生成。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import PdfViewer from './PdfViewer'
import { formatTime, splitKeyPoints } from './format'
import {
  fetchPaperSections,
  generateSummaries,
  getSummary,
  listSummaryRevisions,
  publishSummary,
  restoreSummaryRevision,
  updateSummary,
} from './api'
import type {
  GenerateSectionResult,
  GenerateSummaryResponse,
  Paper,
  PaperSections,
  SectionItem,
  SummaryEvidence,
  SummaryRevision,
} from './types'
import type { SummaryDetail } from './types'

interface Props {
  paper: Paper
  onToast: (message: string) => void
}

/** 原文查看器需要的最小参数：页码 + PDF 点坐标框。 */
type EvidenceView = { page: number; bbox: number[]; title: string; label: string }

/** 章节列表右侧的小状态标签。人工编辑优先于 draft/published。 */
function summaryTag(section: SectionItem): { cls: string; text: string; title: string } {
  if (section.block_count === 0) {
    return { cls: 'group', text: '分组', title: '纯父节点：只做分组，不生成摘要' }
  }
  const brief = section.summary
  if (!brief) return { cls: 'none', text: '无摘要', title: '本节还没有摘要' }
  if (brief.source === 'human_edited') {
    return {
      cls: 'human',
      text: '人工',
      title: brief.human_locked ? '人工编辑（已锁定；自动生成会跳过，重新生成可覆盖）' : '人工编辑',
    }
  }
  if (brief.status === 'draft') {
    return { cls: 'draft', text: '草稿', title: '草稿：发布后才算定稿' }
  }
  if (brief.status === 'published') {
    return { cls: 'published', text: '已发布', title: '已发布' }
  }
  return { cls: 'none', text: brief.status, title: brief.status }
}

function short(text: string, limit: number): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > limit ? `${flat.slice(0, limit)}…` : flat
}

export default function ChapterSummary({ paper, onToast }: Props) {
  const [sections, setSections] = useState<PaperSections | null>(null)
  const [sectionsLoading, setSectionsLoading] = useState(true)
  const [sectionsError, setSectionsError] = useState<string | null>(null)
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [detail, setDetail] = useState<SummaryDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)
  // 生成：整篇 / 单节共用一套 busy + 失败清单
  const [generating, setGenerating] = useState(false)
  const [genFailures, setGenFailures] = useState<GenerateSectionResult[]>([])
  // 人工编辑
  const [editing, setEditing] = useState(false)
  const [draftContent, setDraftContent] = useState('')
  const [draftPoints, setDraftPoints] = useState('')
  const [saving, setSaving] = useState(false)
  const [publishing, setPublishing] = useState(false)
  // 历史版本
  const [revisions, setRevisions] = useState<SummaryRevision[] | null>(null)
  const [revisionsLoading, setRevisionsLoading] = useState(false)
  const [restoring, setRestoring] = useState<string | null>(null)
  // 证据跳转
  const [evidenceView, setEvidenceView] = useState<EvidenceView | null>(null)

  const loadSections = useCallback(
    async (keepKey?: string | null) => {
      setSectionsLoading(true)
      setSectionsError(null)
      try {
        const data = await fetchPaperSections(paper.id)
        setSections(data)
        setSelectedKey((prev) => {
          const want = keepKey ?? prev
          if (want && data.sections.some((s) => s.key === want)) return want
          // 默认挑第一个已有摘要、且能生成摘要的章节；没有就挑第一个有正文的
          const withSummary = data.sections.find((s) => s.block_count > 0 && s.summary)
          const first = withSummary ?? data.sections.find((s) => s.block_count > 0)
          return first?.key ?? data.sections[0]?.key ?? null
        })
      } catch (err) {
        setSections(null)
        setSectionsError(err instanceof Error ? err.message : String(err))
      } finally {
        setSectionsLoading(false)
      }
    },
    [paper.id],
  )

  useEffect(() => {
    void loadSections(null)
  }, [loadSections])

  const selected = useMemo(
    () => sections?.sections.find((s) => s.key === selectedKey) ?? null,
    [sections, selectedKey],
  )
  const selectedSummaryId = selected?.summary?.summary_id ?? null

  // 选中章节 → 拉摘要详情。没摘要的章节不请求，直接显示空态。
  useEffect(() => {
    let cancelled = false
    setDetail(null)
    setDetailError(null)
    setEditing(false)
    setRevisions(null)
    if (!selectedKey || !selectedSummaryId) return
    setDetailLoading(true)
    void (async () => {
      try {
        const data = await getSummary(selectedSummaryId)
        if (!cancelled) setDetail(data)
      } catch (err) {
        if (!cancelled) setDetailError(err instanceof Error ? err.message : String(err))
      } finally {
        if (!cancelled) setDetailLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [selectedKey, selectedSummaryId])

  const collectFailures = (res: GenerateSummaryResponse) => {
    const failed = res.results.filter((r) => r.status === 'failed')
    setGenFailures(failed)
    return failed
  }

  const statsText = (res: GenerateSummaryResponse): string => {
    const parts = [`新建 ${res.created}`]
    if (res.updated) parts.push(`更新 ${res.updated}`)
    parts.push(`已有 ${res.skipped_existing}`)
    if (res.skipped_locked) parts.push(`锁定 ${res.skipped_locked}`)
    if (res.skipped_empty) parts.push(`无正文 ${res.skipped_empty}`)
    if (res.failed) parts.push(`失败 ${res.failed}`)
    if (res.tokens_in_total || res.tokens_out_total) {
      parts.push(`token ${res.tokens_in_total}/${res.tokens_out_total}`)
    }
    if (res.cost_total > 0) parts.push(`花费 $${res.cost_total.toFixed(4)}`)
    return parts.join(' · ')
  }

  const refreshDetail = useCallback(async () => {
    // 重新生成后章节树会刷新，但右侧详情的 summary_id 不变 → 依赖不会变，这里手动再拉一次，
    // 否则「重新生成」后正文还是旧的。
    if (!detail) return
    try {
      setDetail(await getSummary(detail.summary_id))
    } catch {
      /* 详情刷新失败不打断：章节树已经是新的，用户可重新点选 */
    }
  }, [detail])

  const generateAll = async () => {
    if (generating || sectionsLoading) return
    setGenerating(true)
    setGenFailures([])
    try {
      const res = await generateSummaries(paper.id, {})
      collectFailures(res)
      onToast(`生成完成：${statsText(res)}`)
      await loadSections(selectedKey)
      await refreshDetail()
    } catch (err) {
      onToast(err instanceof Error ? `生成失败：${err.message}` : '生成失败')
    } finally {
      setGenerating(false)
    }
  }

  const generateOne = async (section: SectionItem, force = false) => {
    if (generating || section.block_count === 0) return
    if (force && !window.confirm(`重新生成「${section.title}」？会覆盖当前摘要（包括人工编辑过的）；旧内容会先留一条历史版本，可在「历史版本」里回滚。`)) {
      return
    }
    setGenerating(true)
    setGenFailures([])
    try {
      const res = await generateSummaries(paper.id, {
        sectionKeys: [section.key],
        force,
      })
      collectFailures(res)
      const row = res.results.find((r) => r.section_key === section.key)
      if (!row) {
        onToast('后端没有返回本节结果，请重试')
      } else if (row.status === 'failed') {
        onToast(`生成失败：${row.error ?? '未知错误'}`)
      } else if (row.status === 'skipped_locked') {
        onToast('本节已被人工编辑锁定，未覆盖')
      } else if (row.status === 'skipped_empty') {
        onToast('本节没有正文块，跳过')
      } else if (row.status === 'skipped_existing') {
        onToast('本节已有摘要；要覆盖请用「重新生成」')
      } else if (row.status === 'updated') {
        onToast('已更新本节摘要')
      } else {
        onToast('已生成本节摘要')
      }
      await loadSections(section.key)
      await refreshDetail()
    } catch (err) {
      onToast(err instanceof Error ? `生成失败：${err.message}` : '生成失败')
    } finally {
      setGenerating(false)
    }
  }

  const startEdit = () => {
    if (!detail) return
    setDraftContent(detail.content)
    setDraftPoints(detail.key_points.join('\n'))
    setEditing(true)
  }

  const saveEdit = async () => {
    if (!detail || saving) return
    if (!draftContent.trim()) {
      onToast('摘要正文不能为空')
      return
    }
    const { points, truncated } = splitKeyPoints(draftPoints)
    setSaving(true)
    const summaryId = detail.summary_id
    try {
      await updateSummary(summaryId, {
        content: draftContent.trim(),
        key_points: points,
      })
    } catch (err) {
      // 只有 PATCH 本身失败才是「保存失败」
      onToast(err instanceof Error ? `保存失败：${err.message}` : '保存失败')
      setSaving(false)
      return
    }

    // PATCH 已经落库：从这里往后的失败都只能说「已保存，刷新失败」，不能误报保存失败
    const clipped = truncated > 0 ? `（要点最多 8 条，超出的 ${truncated} 条已截断）` : ''
    setEditing(false)
    setRevisions(null)
    onToast(`已保存${clipped}；人工编辑已锁定，自动生成会跳过（重新生成可覆盖，旧内容留历史）`)
    try {
      // PATCH 返回 SummaryOut（没有 evidence），再拉一次详情把证据带上
      setDetail(await getSummary(summaryId))
    } catch (err) {
      onToast(
        `已保存${clipped}，但刷新详情失败${err instanceof Error ? `（${err.message}）` : ''}；重新点选本节即可看到最新内容`,
      )
    }
    await loadSections(selectedKey)
    setSaving(false)
  }

  const publish = async () => {
    if (!detail || publishing) return
    setPublishing(true)
    try {
      const updated = await publishSummary(detail.summary_id)
      setDetail((prev) => (prev ? { ...prev, ...updated } : prev))
      onToast('已发布')
      await loadSections(selectedKey)
    } catch (err) {
      onToast(err instanceof Error ? `发布失败：${err.message}` : '发布失败')
    } finally {
      setPublishing(false)
    }
  }

  const toggleRevisions = async () => {
    if (!detail || revisionsLoading) return
    if (revisions) {
      setRevisions(null)
      return
    }
    setRevisionsLoading(true)
    try {
      setRevisions(await listSummaryRevisions(detail.summary_id))
    } catch (err) {
      onToast(err instanceof Error ? `读取历史失败：${err.message}` : '读取历史失败')
    } finally {
      setRevisionsLoading(false)
    }
  }

  const restore = async (rev: SummaryRevision) => {
    if (!detail || restoring) return
    if (!window.confirm(`回滚到 ${formatTime(rev.created_at)} 的版本？当前内容会先留一条历史。`)) {
      return
    }
    setRestoring(rev.revision_id)
    try {
      await restoreSummaryRevision(detail.summary_id, rev.revision_id)
      setDetail(await getSummary(detail.summary_id))
      setRevisions(null)
      onToast('已回滚到该版本')
      await loadSections(selectedKey)
    } catch (err) {
      onToast(err instanceof Error ? `回滚失败：${err.message}` : '回滚失败')
    } finally {
      setRestoring(null)
    }
  }

  const openEvidence = (ev: SummaryEvidence) => {
    if (Array.isArray(ev.bbox) && ev.bbox.length === 4) {
      setEvidenceView({
        page: ev.page,
        bbox: ev.bbox,
        title: `${paper.title} · 证据 p.${ev.page}`,
        label: `p.${ev.page}`,
      })
    } else {
      onToast(`证据在 p.${ev.page}：${short(ev.text, 80)}`)
    }
  }

  return (
    <div className="sumwork">
      <aside className="sumlist">
        <div className="sumlist-head">
          <span className="sumlist-title">
            章节 <span className="muted">{sections?.sections.length ?? '…'}</span>
          </span>
          <button
            className="primary small"
            disabled={generating || sectionsLoading || !!sectionsError}
            onClick={() => void generateAll()}
            title="给所有有正文的章节生成摘要；已有人工编辑的会跳过"
          >
            {generating ? '生成中…' : '生成全部章节'}
          </button>
        </div>

        {sectionsLoading && <p className="sum-muted">载入章节…</p>}
        {sectionsError && (
          <div className="sum-error">
            <p>{sectionsError}</p>
            <button className="ghost" onClick={() => void loadSections(selectedKey)}>
              重试
            </button>
          </div>
        )}

        {sections && (
          <ul className="sumtree">
            {sections.sections.map((section) => {
              const tag = summaryTag(section)
              return (
                <li key={section.key}>
                  <button
                    className={`sumitem ${selectedKey === section.key ? 'active' : ''} ${
                      section.block_count === 0 ? 'parent' : ''
                    }`}
                    style={{ paddingLeft: `${8 + Math.max(0, section.level - 1) * 12}px` }}
                    onClick={() => setSelectedKey(section.key)}
                    title={section.title}
                  >
                    <span className="sumitem-main">
                      <span className="sumitem-title">{section.title || '（无标题）'}</span>
                      <span className="sumitem-meta">
                        p{section.page_start}-{section.page_end} · {section.figure_count} 图
                      </span>
                    </span>
                    <span className={`sumtag ${tag.cls}`} title={tag.title}>
                      {tag.text}
                    </span>
                  </button>
                </li>
              )
            })}
          </ul>
        )}
      </aside>

      <section className="sumdetail">
        {genFailures.length > 0 && (
          <div className="banner error sum-failures">
            <strong>有 {genFailures.length} 个章节生成失败：</strong>
            <ul>
              {genFailures.map((row) => (
                <li key={row.section_key}>
                  {row.section_title}：{row.error ?? '未知错误'}
                </li>
              ))}
            </ul>
          </div>
        )}

        {!selected && !sectionsLoading && !sectionsError && (
          <div className="empty">
            <h3>选一个章节</h3>
            <p>左侧点章节看摘要；标「分组」的父节点只做层级分组，不生成摘要。</p>
          </div>
        )}

        {selected && !selected.summary && !detailLoading && (
          <div className="sum-empty">
            <h2>{selected.title || '（无标题）'}</h2>
            <p className="sum-meta">
              p{selected.page_start}-{selected.page_end} · {selected.figure_count} 图 ·{' '}
              {selected.block_count} 个正文块
            </p>
            {selected.block_count === 0 ? (
              <p className="tip">
                这是分组标题（纯父节点），没有自己的正文，不能生成摘要；请选它下面的章节。
              </p>
            ) : (
              <>
                <p className="sum-muted">本节还没有摘要。</p>
                <button
                  className="primary"
                  disabled={generating}
                  onClick={() => void generateOne(selected)}
                >
                  {generating ? '生成中…' : '生成本节摘要'}
                </button>
              </>
            )}
          </div>
        )}

        {detailLoading && <p className="sum-muted">载入摘要…</p>}
        {detailError && <div className="banner error">摘要加载失败：{detailError}</div>}

        {selected && selected.summary && detail && !detailLoading && (
          <article className="sum-body">
            <header className="sum-head">
              <h2>{detail.section_title}</h2>
              <div className="sum-tags">
                <span className={`sumtag ${detail.status === 'draft' ? 'draft' : 'published'}`}>
                  {detail.status === 'draft' ? '草稿' : '已发布'}
                </span>
                <span className={`sumtag ${detail.source === 'human_edited' ? 'human' : 'agent'}`}>
                  {detail.source === 'human_edited' ? '人工编辑' : 'AI 生成'}
                </span>
                {detail.auto_accepted && <span className="sumtag auto">自动接受</span>}
                {detail.confidence != null && (
                  <span
                    className="sumtag conf"
                    title="模型自评置信度（0–1）：达到自动接受阈值的才会直接发布"
                  >
                    置信度 {detail.confidence.toFixed(2)}
                  </span>
                )}
                {detail.human_locked && (
                  <span className="sumtag human" title="人工编辑后锁定：「生成全部章节」会跳过">
                    已锁定
                  </span>
                )}
                <span className="sum-model" title="生成模型与提示词版本">
                  {detail.provider} · {detail.model} · prompt {detail.prompt_version}
                </span>
              </div>
            </header>

            {editing ? (
              <div className="sum-edit">
                <label htmlFor="sum-content">摘要正文</label>
                <textarea
                  id="sum-content"
                  value={draftContent}
                  onChange={(e) => setDraftContent(e.target.value)}
                  rows={9}
                  maxLength={8000}
                  spellCheck={false}
                />
                <label htmlFor="sum-points">要点（每行一条）</label>
                <textarea
                  id="sum-points"
                  value={draftPoints}
                  onChange={(e) => setDraftPoints(e.target.value)}
                  rows={4}
                  spellCheck={false}
                  placeholder="每条一行，可留空"
                />
                <span className="muted">
                  {splitKeyPoints(draftPoints).total} / 8 条（超出部分保存时截断）
                </span>
                <div className="sum-actions">
                  <button className="primary" disabled={saving} onClick={() => void saveEdit()}>
                    {saving ? '保存中…' : '保存'}
                  </button>
                  <button className="ghost" disabled={saving} onClick={() => setEditing(false)}>
                    取消
                  </button>
                </div>
              </div>
            ) : (
              <>
                <p className="sum-content">{detail.content}</p>

                {detail.key_points.length > 0 && (
                  <ul className="sum-points">
                    {detail.key_points.map((point, index) => (
                      <li key={`${index}-${point.slice(0, 12)}`}>{point}</li>
                    ))}
                  </ul>
                )}

                <h3 className="sum-sub">证据（{detail.evidence.length}）</h3>
                {detail.evidence.length === 0 ? (
                  <p className="sum-muted">这条摘要没有绑定证据块（不应发生；请重新生成并检查后端校验）。</p>
                ) : (
                  <ul className="sum-evidence">
                    {detail.evidence.map((ev) => (
                      <li key={ev.block_id}>
                        <button
                          className="sum-ev"
                          onClick={() => openEvidence(ev)}
                          title="打开原文并高亮这段证据"
                        >
                          <span className="sum-ev-page">p.{ev.page}</span>
                          <span className="sum-ev-text">{short(ev.text, 120)}</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}

                <div className="sum-actions">
                  <button className="ghost" onClick={startEdit}>
                    编辑
                  </button>
                  {detail.status === 'draft' && (
                    <button className="primary" disabled={publishing} onClick={() => void publish()}>
                      {publishing ? '发布中…' : '发布'}
                    </button>
                  )}
                  <button
                    className="ghost"
                    disabled={generating}
                    onClick={() => void generateOne(selected, true)}
                    title="会覆盖当前摘要（含人工编辑过的）；旧内容先留历史版本，可回滚"
                  >
                    {generating ? '生成中…' : '重新生成'}
                  </button>
                  <button className="ghost" disabled={revisionsLoading} onClick={() => void toggleRevisions()}>
                    {revisionsLoading ? '载入历史…' : revisions ? '收起历史版本' : '历史版本'}
                  </button>
                </div>

                {detail.human_locked && (
                  <p className="tip">人工编辑已锁定：「生成全部章节」会跳过这一节；点「重新生成」仍可显式覆盖（旧内容会先留一条历史版本）。</p>
                )}

                {revisions && (
                  <div className="sum-revs">
                    <h3 className="sum-sub">历史版本（{revisions.length}）</h3>
                    {revisions.length === 0 ? (
                      <p className="sum-muted">还没有历史版本（首次编辑/发布后才会出现）。</p>
                    ) : (
                      <ul>
                        {revisions.map((rev) => (
                          <li className="sum-rev" key={rev.revision_id}>
                            <div className="sum-rev-head">
                              <span className="sum-rev-reason">{rev.reason || rev.status}</span>
                              <span className="sum-muted">{formatTime(rev.created_at)}</span>
                              <button
                                className="link"
                                disabled={!!restoring}
                                onClick={() => void restore(rev)}
                              >
                                {restoring === rev.revision_id ? '回滚中…' : '回滚到此版本'}
                              </button>
                            </div>
                            <p className="sum-rev-text">{short(rev.content, 80)}</p>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                )}
              </>
            )}
          </article>
        )}
      </section>

      {evidenceView && (
        <PdfViewer
          figureId={null}
          paperId={paper.id}
          page={evidenceView.page}
          bbox={evidenceView.bbox}
          title={evidenceView.title}
          label={evidenceView.label}
          onClose={() => setEvidenceView(null)}
        />
      )}
    </div>
  )
}
