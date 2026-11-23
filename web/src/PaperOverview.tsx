// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * 整篇速览（AI 一键总结文献）。
 *
 * 与「章节摘要」的区别：这里只调一次模型，材料是作者摘要 + 章节大纲 + 引言/结论证据块，
 * 产出 5–8 句综述 + 要点 + 证据；仍走同一套 summary 状态机（编辑锁定 / 发布 / 历史回滚）。
 */
import { useCallback, useEffect, useState } from 'react'
import PdfViewer from './PdfViewer'
import { formatTime, splitKeyPoints } from './format'
import {
  generatePaperSummary,
  getPaperSummary,
  listSummaryRevisions,
  publishSummary,
  restoreSummaryRevision,
  updateSummary,
} from './api'
import type { Paper, SummaryDetail, SummaryEvidence, SummaryRevision } from './types'

interface Props {
  paper: Paper
  onToast: (message: string) => void
}

type EvidenceView = { page: number; bbox: number[]; title: string; label: string }

function short(text: string, limit: number): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > limit ? `${flat.slice(0, limit)}…` : flat
}

export default function PaperOverview({ paper, onToast }: Props) {
  const [summary, setSummary] = useState<SummaryDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [generating, setGenerating] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draftContent, setDraftContent] = useState('')
  const [draftPoints, setDraftPoints] = useState('')
  const [saving, setSaving] = useState(false)
  const [publishing, setPublishing] = useState(false)
  const [revisions, setRevisions] = useState<SummaryRevision[] | null>(null)
  const [revisionsLoading, setRevisionsLoading] = useState(false)
  const [restoring, setRestoring] = useState<string | null>(null)
  const [evidenceView, setEvidenceView] = useState<EvidenceView | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setLoadError(null)
    try {
      setSummary(await getPaperSummary(paper.id))
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [paper.id])

  useEffect(() => {
    void load()
  }, [load])

  const generate = async (force = false) => {
    if (generating) return
    if (
      force &&
      summary &&
      !window.confirm('重新生成整篇速览？会覆盖 AI 版本；人工编辑过的会先留历史，确认覆盖。')
    ) {
      return
    }
    setGenerating(true)
    try {
      const stats = await generatePaperSummary(paper.id, { force })
      onToast(
        `${stats.status === 'created' ? '速览已生成' : '速览已更新'} · ${stats.provider}/${stats.model}` +
          ` · ${stats.tokens_in}/${stats.tokens_out} tokens · ${stats.latency_ms}ms`,
      )
      await load()
      setEditing(false)
      setRevisions(null)
    } catch (err) {
      onToast(err instanceof Error ? `生成失败：${err.message}` : '生成失败')
    } finally {
      setGenerating(false)
    }
  }

  const startEdit = () => {
    if (!summary) return
    setDraftContent(summary.content)
    setDraftPoints(summary.key_points.join('\n'))
    setEditing(true)
  }

  const saveEdit = async () => {
    if (!summary || saving) return
    if (!draftContent.trim()) {
      onToast('速览正文不能为空')
      return
    }
    const { points, truncated } = splitKeyPoints(draftPoints)
    setSaving(true)
    try {
      await updateSummary(summary.summary_id, {
        content: draftContent.trim(),
        key_points: points,
      })
    } catch (err) {
      // 只有 PATCH 本身失败才是「保存失败」
      onToast(err instanceof Error ? `保存失败：${err.message}` : '保存失败')
      setSaving(false)
      return
    }

    // PATCH 已落库：后续刷新失败只能提示「已保存，刷新失败」
    const clipped = truncated > 0 ? `（要点最多 8 条，超出的 ${truncated} 条已截断）` : ''
    setEditing(false)
    setRevisions(null)
    onToast(`已保存${clipped}；人工编辑已锁定，重新生成需要先确认覆盖`)
    try {
      setSummary(await getPaperSummary(paper.id))
    } catch (err) {
      onToast(
        `已保存${clipped}，但刷新详情失败${err instanceof Error ? `（${err.message}）` : ''}；刷新页面或重新进入 AI 速览即可看到最新内容`,
      )
    }
    setSaving(false)
  }

  const publish = async () => {
    if (!summary || publishing) return
    setPublishing(true)
    try {
      await publishSummary(summary.summary_id)
      setSummary(await getPaperSummary(paper.id))
      onToast('已发布')
    } catch (err) {
      onToast(err instanceof Error ? `发布失败：${err.message}` : '发布失败')
    } finally {
      setPublishing(false)
    }
  }

  const toggleRevisions = async () => {
    if (!summary || revisionsLoading) return
    if (revisions) {
      setRevisions(null)
      return
    }
    setRevisionsLoading(true)
    try {
      setRevisions(await listSummaryRevisions(summary.summary_id))
    } catch (err) {
      onToast(err instanceof Error ? `读取历史失败：${err.message}` : '读取历史失败')
    } finally {
      setRevisionsLoading(false)
    }
  }

  const restore = async (rev: SummaryRevision) => {
    if (!summary || restoring) return
    if (!window.confirm(`回滚到 ${formatTime(rev.created_at)} 的版本？当前内容会先留一条历史。`)) return
    setRestoring(rev.revision_id)
    try {
      await restoreSummaryRevision(summary.summary_id, rev.revision_id)
      setSummary(await getPaperSummary(paper.id))
      setRevisions(null)
      onToast('已回滚到该版本')
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

  if (loading) return <p className="sum-muted overview-loading">载入整篇速览…</p>
  if (loadError) {
    return (
      <div className="banner error">
        整篇速览加载失败：{loadError}
        <button className="link" onClick={() => void load()}>
          重试
        </button>
      </div>
    )
  }

  if (!summary) {
    return (
      <div className="overview-empty">
        <div className="overview-hero">
          <span className="overview-badge">AI 速览</span>
          <h2>一键总结这篇文献</h2>
          <p>
            模型会通读作者摘要、章节大纲和引言/结论附近的正文，给出 5–8 句中文综述、
            3–5 条要点（问题 / 方法 / 结果 / 局限）以及可回溯的证据块。整篇只调用一次模型。
          </p>
          <button className="primary" disabled={generating} onClick={() => void generate()}>
            {generating ? 'AI 正在通读全文…' : 'AI 一键总结'}
          </button>
          {generating && <p className="sum-muted">第一次通常 30–90 秒；请保持页面打开。</p>}
        </div>
      </div>
    )
  }

  return (
    <article className="sum-body overview-body">
      <header className="sum-head">
        <h2>整篇速览</h2>
        <div className="sum-tags">
          <span className={`sumtag ${summary.status === 'draft' ? 'draft' : 'published'}`}>
            {summary.status === 'draft' ? '草稿' : '已发布'}
          </span>
          <span className={`sumtag ${summary.source === 'human_edited' ? 'human' : 'agent'}`}>
            {summary.source === 'human_edited' ? '人工编辑' : 'AI 生成'}
          </span>
          {summary.auto_accepted && <span className="sumtag auto">自动接受</span>}
          {summary.confidence != null && (
            <span
              className="sumtag conf"
              title="模型自评置信度（0–1）：达到自动接受阈值的才会直接发布"
            >
              置信度 {summary.confidence.toFixed(2)}
            </span>
          )}
          {summary.human_locked && <span className="sumtag human">已锁定</span>}
          <span className="sum-model" title="生成模型与提示词版本">
            {summary.provider} · {summary.model} · prompt {summary.prompt_version} ·
            {' '}{summary.tokens_in}/{summary.tokens_out} tokens
          </span>
        </div>
      </header>

      {editing ? (
        <div className="sum-edit">
          <label htmlFor="overview-content">速览正文</label>
          <textarea
            id="overview-content"
            value={draftContent}
            onChange={(e) => setDraftContent(e.target.value)}
            rows={8}
            maxLength={8000}
            spellCheck={false}
          />
          <label htmlFor="overview-points">要点（每行一条）</label>
          <textarea
            id="overview-points"
            value={draftPoints}
            onChange={(e) => setDraftPoints(e.target.value)}
            rows={4}
            spellCheck={false}
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
          <p className="sum-content overview-text">{summary.content}</p>

          {summary.key_points.length > 0 && (
            <ul className="sum-points">
              {summary.key_points.map((point, index) => (
                <li key={`${index}-${point.slice(0, 12)}`}>{point}</li>
              ))}
            </ul>
          )}

          <h3 className="sum-sub">证据（{summary.evidence.length}）</h3>
          {summary.evidence.length === 0 ? (
            <p className="sum-muted">这条速览没有绑定证据块（不应发生；请重新生成）。</p>
          ) : (
            <ul className="sum-evidence">
              {summary.evidence.map((ev) => (
                <li key={ev.block_id}>
                  <button
                    className="sum-ev"
                    onClick={() => void openEvidence(ev)}
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
            {summary.status === 'draft' && (
              <button className="primary" disabled={publishing} onClick={() => void publish()}>
                {publishing ? '发布中…' : '发布'}
              </button>
            )}
            <button className="ghost" disabled={generating} onClick={() => void generate(true)}>
              {generating ? '生成中…' : '重新生成'}
            </button>
            <button
              className="ghost"
              disabled={revisionsLoading}
              onClick={() => void toggleRevisions()}
            >
              {revisionsLoading ? '载入历史…' : revisions ? '收起历史版本' : '历史版本'}
            </button>
          </div>

          {revisions && (
            <div className="sum-revs">
              <h3 className="sum-sub">历史版本（{revisions.length}）</h3>
              {revisions.length === 0 ? (
                <p className="sum-muted">还没有历史版本。</p>
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
    </article>
  )
}
