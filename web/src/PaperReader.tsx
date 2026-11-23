// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * 文献正文速览（2026-11-12 工作台改版）。
 *
 * 目的不是「再做一个 PDF 阅读器」，而是**把解析后的正文按章节摊开**，方便和图表 / AI 速览 /
 * 章节摘要对照：左栏章节大纲，右栏正文段落；点任意段落打开原文对应页并高亮 bbox。
 * 数据来自只读接口 `GET /api/papers/{id}/text`（不落库、不调模型）。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import PdfViewer from './PdfViewer'
import { decideOcrProposal, fetchOcrProposals, fetchPaperText } from './api'
import type { OcrProposal, Paper, PaperText, PaperTextBlock } from './types'

interface Props {
  paper: Paper
}

type ViewTarget = { page: number; bbox: number[]; title: string; label: string }

function short(text: string, limit: number): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > limit ? `${flat.slice(0, limit)}…` : flat
}

export default function PaperReader({ paper }: Props) {
  const [text, setText] = useState<PaperText | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [activeKey, setActiveKey] = useState<string | null>(null)
  const [viewer, setViewer] = useState<ViewTarget | null>(null)
  const [ocrProposals, setOcrProposals] = useState<OcrProposal[]>([])
  const [ocrBusy, setOcrBusy] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await fetchPaperText(paper.id)
      setText(data)
      setActiveKey(data.sections.find((s) => s.blocks.length > 0)?.key ?? data.sections[0]?.key ?? null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [paper.id])

  const loadOcr = useCallback(async () => {
    try {
      setOcrProposals(await fetchOcrProposals(paper.id))
    } catch {
      // 校对提案是增强信息，拉不到不影响正文阅读
      setOcrProposals([])
    }
  }, [paper.id])

  useEffect(() => {
    void load()
    void loadOcr()
  }, [load, loadOcr])

  const decideOcr = async (
    proposal: OcrProposal,
    action: 'accept_ocr' | 'accept_model' | 'reject',
  ) => {
    setOcrBusy(proposal.proposal_id)
    try {
      await decideOcrProposal(proposal.proposal_id, action)
      setOcrProposals((prev) => prev.filter((row) => row.proposal_id !== proposal.proposal_id))
      if (action === 'accept_model') await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setOcrBusy(null)
    }
  }

  const stats = useMemo(() => {
    if (!text) return null
    return {
      sections: text.sections.length,
      blocks: text.block_count,
      pages: text.page_count ?? undefined,
    }
  }, [text])

  const jumpTo = (key: string) => {
    setActiveKey(key)
    const el = document.getElementById(`reader-sec-${key}`)
    el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  const openBlock = (block: PaperTextBlock) => {
    setViewer({
      page: block.page,
      bbox: Array.isArray(block.bbox) && block.bbox.length === 4 ? block.bbox : [],
      title: `${paper.title} · 正文 p.${block.page}`,
      label: `p.${block.page}`,
    })
  }

  if (loading) return <p className="reader-muted">载入正文…</p>
  if (error) {
    return (
      <div className="banner error">
        正文加载失败：{error}
        <button className="link" onClick={() => void load()}>
          重试
        </button>
      </div>
    )
  }
  if (!text || text.sections.length === 0) {
    return (
      <div className="empty">
        <h3>没有可读正文</h3>
        <p>解析结果里没有正文块；可能是扫描页还没跑 OCR，或者这篇文献只有元数据。</p>
      </div>
    )
  }

  return (
    <div className="reader">
      <aside className="reader-outline" aria-label="章节大纲">
        <div className="reader-outline-head">
          <span>章节</span>
          <span className="muted">
            {stats?.sections} 节 · {stats?.blocks} 段
            {stats?.pages ? ` · ${stats.pages} 页` : ''}
          </span>
        </div>
        <ul>
          {text.sections.map((section) => (
            <li key={section.key}>
              <button
                className={`reader-outline-item ${activeKey === section.key ? 'on' : ''}`}
                style={{ paddingLeft: `${8 + Math.max(0, section.level - 1) * 12}px` }}
                onClick={() => jumpTo(section.key)}
                title={`p${section.page_start}–${section.page_end} · ${section.blocks.length} 段`}
              >
                <span className="reader-outline-title">{section.title || '（无标题）'}</span>
                <span className="reader-outline-meta">
                  p{section.page_start}
                  {section.page_end !== section.page_start ? `–${section.page_end}` : ''}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <div className="reader-body">
        {ocrProposals.length > 0 && (
          <section className="ocr-review" aria-label="OCR 校对提案">
            <div className="ocr-review-head">
              <strong>OCR 校对：{ocrProposals.length} 页需要你确认</strong>
              <span>
                轻量 OCR 已先写入正文；下面这些页视觉模型读出了不同内容，请你决定保留哪一份。
              </span>
            </div>
            {ocrProposals.map((proposal) => (
              <article className="ocr-review-item" key={proposal.proposal_id}>
                <div className="ocr-review-title">
                  第 {proposal.page} 页
                  <span className="muted">
                    模型 {proposal.model || proposal.provider || '视觉模型'} ·{' '}
                    {proposal.differences.length} 处差异
                  </span>
                </div>
                <details>
                  <summary>查看 OCR 与大模型读数</summary>
                  <div className="ocr-review-texts">
                    <div>
                      <h4>OCR（当前正文）</h4>
                      <pre>{proposal.ocr_text || '（空）'}</pre>
                    </div>
                    <div>
                      <h4>大模型修订</h4>
                      <pre>{proposal.model_text || '（空）'}</pre>
                    </div>
                  </div>
                  {proposal.differences.length > 0 && (
                    <ul className="ocr-review-diffs">
                      {proposal.differences.map((diff, index) => (
                        <li key={`${proposal.proposal_id}-${index}`}>
                          <code>{diff.ocr ?? ''}</code> → <code>{diff.model ?? ''}</code>
                          {diff.reason ? `（${diff.reason}）` : ''}
                        </li>
                      ))}
                    </ul>
                  )}
                </details>
                <div className="ocr-review-actions">
                  <button
                    className="primary"
                    disabled={ocrBusy === proposal.proposal_id}
                    onClick={() => void decideOcr(proposal, 'accept_model')}
                  >
                    采用大模型修订
                  </button>
                  <button
                    className="ghost"
                    disabled={ocrBusy === proposal.proposal_id}
                    onClick={() => void decideOcr(proposal, 'accept_ocr')}
                  >
                    保留 OCR
                  </button>
                  <button
                    className="ghost"
                    disabled={ocrBusy === proposal.proposal_id}
                    onClick={() => void decideOcr(proposal, 'reject')}
                  >
                    忽略
                  </button>
                </div>
              </article>
            ))}
          </section>
        )}
        {text.sections.map((section) => (
          <section
            className="reader-section"
            id={`reader-sec-${section.key}`}
            key={section.key}
          >
            <header className="reader-sec-head">
              <h3>{section.title || '（无标题）'}</h3>
              <span className="reader-sec-page">
                p{section.page_start}
                {section.page_end !== section.page_start ? `–${section.page_end}` : ''}
              </span>
            </header>
            {section.blocks.length === 0 ? (
              <p className="reader-muted">（本节没有正文块）</p>
            ) : (
              section.blocks.map((block) => (
                <button
                  key={block.block_id}
                  className={`reader-para ${block.type === 'caption' ? 'caption' : ''}`}
                  onClick={() => openBlock(block)}
                  title={`第 ${block.page} 页 · 点击对照原文`}
                >
                  <span className="reader-para-text">{short(block.text, 1200)}</span>
                  <span className="reader-para-page">p.{block.page}</span>
                </button>
              ))
            )}
          </section>
        ))}
        <p className="reader-tip">
          点任意段落 → 打开原文对应页并高亮位置。正文来自解析结果，未做 AI 改写。
        </p>
      </div>

      {viewer && (
        <PdfViewer
          figureId={null}
          paperId={paper.id}
          page={viewer.page}
          bbox={viewer.bbox.length === 4 ? viewer.bbox : null}
          title={viewer.title}
          label={viewer.label}
          onClose={() => setViewer(null)}
        />
      )}
    </div>
  )
}
