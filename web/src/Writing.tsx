// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * M5 写作台：把「读到的东西」变成「能引用的稿子」。
 *
 * 三个页签：
 * 1. 素材：一句论断 + 证据（图/表/整篇速览）→ claim 卡片；可切草稿/已认可、改、删；
 * 2. 对比矩阵：选 2–8 篇文献，实时派生「题目/作者/摘要/关键发现/图表/我的论断」；
 * 3. 导出：LaTeX（含 refs.bib）/ BibTeX / Markdown，外加 \cite{} 引用校验。
 *
 * 设计约束（与后端一致）：
 * - 一条 claim 必须挂真实证据；没有证据的输入框是灰的，不让人白写；
 * - 矩阵/导出/校验都是只读派生，不落库，改完素材回来重新生成即可；
 * - 导出只给骨架，不假装能生成整篇论文。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  applyPaperMetadata,
  checkCitations,
  checkThesis,
  copyText,
  createClaim,
  deleteClaim,
  exportThesis,
  exportReferenceCslJson,
  exportWriting,
  exportWritingDocument,
  fetchDocExportCapabilities,
  fetchMatrix,
  fetchMatrixDimensions,
  fetchReferenceStyles,
  fetchWritingOutline,
  formatReferences,
  getPaperSummary,
  listClaimRevisions,
  listClaims,
  listPapers,
  lookupPaperMetadata,
  paperFigures,
  paperTables,
  patchPaper,
  restoreClaimRevision,
  updateClaim,
} from './api'
import { formatTime } from './format'
import type {
  CitationCheck,
  Claim,
  ClaimRevision,
  ComparisonMatrix,
  DocExportCapabilities,
  MatrixDimension,
  MetadataLookup,
  Paper,
  ReferenceFormat,
  ReferenceStyle,
  ThesisCheck,
  ThesisExport,
  WritingExport,
  WritingOutline,
} from './types'

interface Props {
  open: boolean
  onClose: () => void
  onToast: (message: string) => void
  /** 从表格页签「写进卡片」进来时预选这篇文献（打开后直接能勾表格证据） */
  initialPaperId?: string | null
}

type Tab = 'material' | 'outline' | 'matrix' | 'export' | 'refs' | 'thesis'

const TAB_LABEL: Record<Tab, string> = {
  material: '素材',
  outline: '大纲',
  matrix: '对比矩阵',
  export: '导出',
  refs: 'References',
  thesis: '学位论文',
}

const SECTION_PRESETS = ['引言', '相关工作', '方法', '结果', '讨论', '结论', '正文']

interface EvidenceOption {
  key: string
  type: 'figure' | 'table' | 'summary'
  id: string
  label: string
  detail: string
}

function evidenceKey(type: string, id: string): string {
  return `${type}:${id}`
}

function downloadText(filename: string, content: string): void {
  const blob = new Blob([content], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  // 下载是同步触发的，下一轮事件循环再释放即可
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

function metaSelectedHasConflict(result: MetadataLookup, selected: Set<string>): boolean {
  return result.fields.some((row) => selected.has(row.field) && row.conflict)
}

function downloadBlob(filename: string, blob: Blob): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export default function Writing({ open, onClose, onToast, initialPaperId }: Props) {
  const [tab, setTab] = useState<Tab>('material')
  const [claims, setClaims] = useState<Claim[] | null>(null)
  const [papers, setPapers] = useState<Paper[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // ---- 写作大纲 + 引用/图表交叉检查（M8） ----
  const [outline, setOutline] = useState<WritingOutline | null>(null)
  const [outlineBusy, setOutlineBusy] = useState(false)
  const [outlineDrafts, setOutlineDrafts] = useState(true)
  const [outlineAttempted, setOutlineAttempted] = useState(false)

  // ---- 素材表单 ----
  const [paperId, setPaperId] = useState('')
  const [section, setSection] = useState('引言')
  const [text, setText] = useState('')
  const [tags, setTags] = useState('')
  const [status, setStatus] = useState<'draft' | 'approved'>('draft')
  const [evidenceOptions, setEvidenceOptions] = useState<EvidenceOption[]>([])
  const [evidenceLoading, setEvidenceLoading] = useState(false)
  const [evidenceIds, setEvidenceIds] = useState<Set<string>>(new Set())
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editingText, setEditingText] = useState('')

  // ---- 矩阵 ----
  const [matrixPaperIds, setMatrixPaperIds] = useState<Set<string>>(new Set())
  const [matrix, setMatrix] = useState<ComparisonMatrix | null>(null)
  const [matrixBusy, setMatrixBusy] = useState(false)
  const [matrixQuery, setMatrixQuery] = useState('')
  const [matrixDims, setMatrixDims] = useState<MatrixDimension[] | null>(null)
  const [matrixSelected, setMatrixSelected] = useState<Set<string>>(new Set())
  const [matrixDimsTouched, setMatrixDimsTouched] = useState(false)

  // ---- claim 编辑历史（2026-11-15 小修） ----
  const [historyClaimId, setHistoryClaimId] = useState<string | null>(null)
  const [historyRows, setHistoryRows] = useState<ClaimRevision[] | null>(null)
  const [historyBusy, setHistoryBusy] = useState(false)

  // ---- 导出 / 引用校验 ----
  const [fmt, setFmt] = useState<'latex' | 'bibtex' | 'markdown'>('latex')
  const [exportTitle, setExportTitle] = useState('')
  const [includeDrafts, setIncludeDrafts] = useState(true)
  const [withComments, setWithComments] = useState(false)
  const [exported, setExported] = useState<WritingExport | null>(null)
  const [exportBusy, setExportBusy] = useState(false)
  // Word/PDF 导出（M8）：能力探测 + 二进制下载
  const [docCaps, setDocCaps] = useState<DocExportCapabilities | null>(null)
  const [docBusy, setDocBusy] = useState<'docx' | 'pdf' | null>(null)
  const [citeText, setCiteText] = useState('')
  const [citeResult, setCiteResult] = useState<CitationCheck | null>(null)
  const [citeBusy, setCiteBusy] = useState(false)

  // ---- References（2026-11-13）：按格式预设生成条目 ----
  const [refStyles, setRefStyles] = useState<ReferenceStyle[] | null>(null)
  const [refStyle, setRefStyle] = useState('gbt7714')
  const [refEngine, setRefEngine] = useState<'template' | 'csl'>('template')
  const [cslEngineAvailable, setCslEngineAvailable] = useState(false)
  const [cslNote, setCslNote] = useState('')
  const [cslJsonBusy, setCslJsonBusy] = useState(false)
  const [refPaperIds, setRefPaperIds] = useState<Set<string>>(new Set())
  const [refTouched, setRefTouched] = useState(false)
  const [refNumbered, setRefNumbered] = useState(true)
  const [refTemplate, setRefTemplate] = useState('')
  const [refQuery, setRefQuery] = useState('')
  const [refResult, setRefResult] = useState<ReferenceFormat | null>(null)
  const [refBusy, setRefBusy] = useState(false)
  // 卷期页补全：当前编辑的文献 + 三个输入
  const [biblioPaperId, setBiblioPaperId] = useState('')
  const [biblioVolume, setBiblioVolume] = useState('')
  const [biblioIssue, setBiblioIssue] = useState('')
  const [biblioPages, setBiblioPages] = useState('')
  const [biblioBusy, setBiblioBusy] = useState(false)
  // 在线元数据补全（Crossref / arXiv，M8）：查询是只读的，apply 才写库
  const [metaBusy, setMetaBusy] = useState<'lookup' | 'search' | 'apply' | null>(null)
  const [metaResult, setMetaResult] = useState<MetadataLookup | null>(null)
  const [metaSelected, setMetaSelected] = useState<Set<string>>(new Set())
  const [metaOverwrite, setMetaOverwrite] = useState(false)
  const [metaQuery, setMetaQuery] = useState('')
  const [metaError, setMetaError] = useState('')

  // ---- 学位论文 LaTeX 包（M8，2026-11-15） ----
  const [thesisTitle, setThesisTitle] = useState('')
  const [thesisAuthor, setThesisAuthor] = useState('')
  const [thesisSchool, setThesisSchool] = useState('')
  const [thesisDegree, setThesisDegree] = useState('')
  const [thesisAdvisor, setThesisAdvisor] = useState('')
  const [thesisYear, setThesisYear] = useState('')
  const [thesisChapterOrder, setThesisChapterOrder] = useState('')
  const [thesisIncludeDrafts, setThesisIncludeDrafts] = useState(true)
  // 空集合 = 全部可见（列表里勾选用于收窄范围）
  const [thesisClaimIds, setThesisClaimIds] = useState<Set<string>>(new Set())
  const [thesisPaperIds, setThesisPaperIds] = useState<Set<string>>(new Set())
  const [thesisPaperQuery, setThesisPaperQuery] = useState('')
  const [thesisExport, setThesisExport] = useState<ThesisExport | null>(null)
  const [thesisBusy, setThesisBusy] = useState(false)
  const [thesisCheckText, setThesisCheckText] = useState('')
  const [thesisCheck, setThesisCheck] = useState<ThesisCheck | null>(null)
  const [thesisCheckBusy, setThesisCheckBusy] = useState(false)

  const loadClaims = useCallback(async () => {
    setError(null)
    try {
      setClaims(await listClaims({ limit: 500 }))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [])

  const loadPapers = useCallback(async () => {
    try {
      setPapers(await listPapers(null, { limit: 200 }))
    } catch {
      setPapers([])
    }
  }, [])

  useEffect(() => {
    if (!open) return
    setTab('material')
    setExported(null)
    setCiteResult(null)
    setThesisCheck(null)
    setHistoryClaimId(null)
    setHistoryRows(null)
    setError(null)
    void loadClaims()
    void loadPapers()
  }, [open, loadClaims, loadPapers])

  // 表格页签「写进卡片」：打开写作台后预选文献，证据列表会直接带上表格
  useEffect(() => {
    if (!open || !initialPaperId) return
    setPaperId(initialPaperId)
  }, [open, initialPaperId])

  // References：格式清单只拉一次（同一台机器上不会变）
  useEffect(() => {
    if (!open || refStyles !== null) return
    void (async () => {
      try {
        const payload = await fetchReferenceStyles()
        setRefStyles(payload.styles)
        setCslEngineAvailable(payload.csl_engine_available)
        setCslNote(payload.csl_note)
      } catch {
        setRefStyles([])
      }
    })()
  }, [open, refStyles])

  // Word/PDF 导出能力只探测一次（pandoc/typst 是否装了）
  useEffect(() => {
    if (!open || docCaps !== null) return
    void (async () => {
      try {
        setDocCaps(await fetchDocExportCapabilities())
      } catch {
        setDocCaps({ docx: false, pdf: false, pandoc: false, typst: false, note: '' })
      }
    })()
  }, [open, docCaps])

  // 第一次进 References 页签时默认全选（用户手动改过就不再自动加回）
  useEffect(() => {
    if (!open || tab !== 'refs' || refTouched) return
    if (papers.length === 0 || refPaperIds.size > 0) return
    setRefPaperIds(new Set(papers.map((paper) => paper.id)))
  }, [open, tab, papers, refPaperIds, refTouched])

  // 矩阵维度清单只拉一次；默认勾选标记为 default 的 7 行
  useEffect(() => {
    if (!open || tab !== 'matrix' || matrixDims !== null) return
    void (async () => {
      try {
        const data = await fetchMatrixDimensions()
        setMatrixDims(data.dimensions)
        if (!matrixDimsTouched) {
          setMatrixSelected(
            new Set(data.dimensions.filter((dim) => dim.default).map((dim) => dim.key)),
          )
        }
      } catch {
        setMatrixDims([])
      }
    })()
  }, [open, tab, matrixDims, matrixDimsTouched])

  // 卷期页补全：切换到某篇时把当前值填进输入框
  useEffect(() => {
    const paper = papers.find((row) => row.id === biblioPaperId)
    if (!paper) {
      setBiblioVolume('')
      setBiblioIssue('')
      setBiblioPages('')
      return
    }
    setBiblioVolume(paper.volume ?? '')
    setBiblioIssue(paper.issue ?? '')
    setBiblioPages(paper.pages ?? '')
  }, [biblioPaperId, papers])

  // Esc 关闭 + 锁背景滚动（与其他浮层一致）
  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    document.body.classList.add('overlay-open')
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.classList.remove('overlay-open')
    }
  }, [open, onClose])

  // 选论文后加载可选证据：图 / 表 / 整篇速览
  useEffect(() => {
    if (!open || !paperId) {
      setEvidenceOptions([])
      setEvidenceLoading(false)
      return
    }
    let cancelled = false
    setEvidenceLoading(true)
    setEvidenceIds(new Set())
    void (async () => {
      const options: EvidenceOption[] = []
      try {
        const figures = await paperFigures(paperId)
        for (const figure of figures) {
          options.push({
            key: evidenceKey('figure', figure.figure_id),
            type: 'figure',
            id: figure.figure_id,
            label: figure.label_raw || figure.label_norm || '（未编号）',
            detail: figure.caption || `第 ${figure.page} 页`,
          })
        }
      } catch {
        /* 文献没有图：继续看表/摘要 */
      }
      try {
        const tables = await paperTables(paperId)
        for (const table of tables) {
          options.push({
            key: evidenceKey('table', table.table_id),
            type: 'table',
            id: table.table_id,
            label: table.label_raw || table.label_norm || '（未编号）',
            detail: table.caption || `第 ${table.page} 页`,
          })
        }
      } catch {
        /* 没有表 */
      }
      try {
        const summary = await getPaperSummary(paperId)
        if (summary) {
          options.push({
            key: evidenceKey('summary', summary.summary_id),
            type: 'summary',
            id: summary.summary_id,
            label: '整篇速览',
            detail: summary.content.slice(0, 90),
          })
        }
      } catch {
        /* 没有整篇速览 */
      }
      if (!cancelled) {
        setEvidenceOptions(options)
        setEvidenceLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [open, paperId])

  const paperTitle = useMemo(() => {
    const map = new Map<string, string>()
    for (const paper of papers) map.set(paper.id, paper.title)
    for (const claim of claims ?? []) {
      if (claim.paper_title && !map.has(claim.paper_id)) map.set(claim.paper_id, claim.paper_title)
    }
    return map
  }, [papers, claims])

  const groupedClaims = useMemo(() => {
    const groups = new Map<string, Claim[]>()
    for (const claim of claims ?? []) {
      const key = claim.section || '正文'
      const list = groups.get(key) ?? []
      list.push(claim)
      groups.set(key, list)
    }
    return [...groups.entries()]
  }, [claims])

  const toggleEvidence = (key: string) => {
    setEvidenceIds((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const submitClaim = async () => {
    if (!paperId || !text.trim() || evidenceIds.size === 0) return
    setBusy(true)
    setError(null)
    try {
      const evidence = [...evidenceIds].map((key) => {
        const [type, ...rest] = key.split(':')
        return { type, id: rest.join(':') }
      })
      await createClaim({
        paper_id: paperId,
        text: text.trim(),
        section: section.trim() || '正文',
        evidence,
        tags: tags
          .split(/[,，\s]+/)
          .map((tag) => tag.trim())
          .filter(Boolean),
        status,
      })
      setText('')
      setTags('')
      setEvidenceIds(new Set())
      await loadClaims()
      onToast('已添加一张写作卡片')
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      setError(`保存失败：${message}`)
      onToast(`保存失败：${message}`)
    } finally {
      setBusy(false)
    }
  }

  const saveEdit = async (claim: Claim) => {
    if (!editingText.trim()) return
    try {
      await updateClaim(claim.claim_id, { text: editingText.trim() })
      setEditingId(null)
      await loadClaims()
      onToast('已更新卡片')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  const toggleStatus = async (claim: Claim) => {
    try {
      await updateClaim(claim.claim_id, {
        status: claim.status === 'approved' ? 'draft' : 'approved',
      })
      await loadClaims()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  const removeClaim = async (claim: Claim) => {
    if (!window.confirm('删除这张写作卡片？')) return
    try {
      await deleteClaim(claim.claim_id)
      await loadClaims()
      onToast('已删除卡片')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  const toggleHistory = async (claim: Claim) => {
    if (historyClaimId === claim.claim_id) {
      setHistoryClaimId(null)
      setHistoryRows(null)
      return
    }
    setHistoryClaimId(claim.claim_id)
    setHistoryRows(null)
    setHistoryBusy(true)
    setError(null)
    try {
      setHistoryRows(await listClaimRevisions(claim.claim_id))
    } catch (err) {
      setHistoryRows([])
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setHistoryBusy(false)
    }
  }

  const restoreHistory = async (claim: Claim, revision: ClaimRevision) => {
    if (!window.confirm('恢复到这个历史版本？当前内容会先存一份历史，可再回滚。')) return
    setHistoryBusy(true)
    setError(null)
    try {
      await restoreClaimRevision(claim.claim_id, revision.revision_id)
      await loadClaims()
      setHistoryRows(await listClaimRevisions(claim.claim_id))
      onToast('已恢复到该历史版本')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setHistoryBusy(false)
    }
  }

  const toggleMatrixDim = (key: string) => {
    setMatrixDimsTouched(true)
    setMatrixSelected((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const resetMatrixDims = () => {
    setMatrixDimsTouched(false)
    setMatrixSelected(
      new Set((matrixDims ?? []).filter((dim) => dim.default).map((dim) => dim.key)),
    )
  }

  const selectAllMatrixDims = () => {
    setMatrixDimsTouched(true)
    setMatrixSelected(new Set((matrixDims ?? []).map((dim) => dim.key)))
  }

  const toggleMatrixPaper = (id: string) => {
    setMatrixPaperIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) {
        next.delete(id)
      } else {
        if (next.size >= 8) {
          onToast('对比矩阵最多 8 篇文献')
          return prev
        }
        next.add(id)
      }
      return next
    })
  }

  const runMatrix = async () => {
    if (matrixPaperIds.size === 0) {
      onToast('先勾选至少一篇文献')
      return
    }
    const dimensions = (matrixDims ?? [])
      .filter((dim) => matrixSelected.has(dim.key))
      .map((dim) => dim.key)
    if (dimensions.length === 0) {
      onToast('至少选一个矩阵维度')
      return
    }
    setMatrixBusy(true)
    setError(null)
    try {
      setMatrix(await fetchMatrix([...matrixPaperIds], dimensions))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setMatrixBusy(false)
    }
  }

  const runExport = async () => {
    setExportBusy(true)
    setError(null)
    try {
      const result = await exportWriting({
        format: fmt,
        title: exportTitle.trim(),
        includeDrafts,
        withEvidenceComments: withComments,
      })
      setExported(result)
      onToast(`已生成 ${result.filename}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setExportBusy(false)
    }
  }

  const runOutline = async () => {
    setOutlineBusy(true)
    setError(null)
    try {
      setOutline(await fetchWritingOutline({ includeDrafts: outlineDrafts }))
    } catch (err) {
      setOutline(null)
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setOutlineBusy(false)
    }
  }

  // 第一次切到大纲时自动生成一次；勾选变化或点「重新生成」再算
  useEffect(() => {
    if (!open || tab !== 'outline' || outlineAttempted) return
    setOutlineAttempted(true)
    void runOutline()
  }, [open, tab, outlineAttempted, outlineDrafts])

  const runDocExport = async (format: 'docx' | 'pdf') => {
    setDocBusy(format)
    setError(null)
    try {
      const { blob, filename } = await exportWritingDocument({
        format,
        title: exportTitle.trim(),
        includeDrafts,
      })
      downloadBlob(filename, blob)
      onToast(`已生成 ${filename}（${format === 'docx' ? 'Word' : 'PDF'}）`)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setDocBusy(null)
    }
  }

  const toggleRefPaper = (id: string) => {
    setRefTouched(true)
    setRefPaperIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const selectAllRefPapers = () => {
    setRefTouched(true)
    setRefPaperIds(new Set(papers.map((paper) => paper.id)))
  }

  const clearRefPapers = () => {
    setRefTouched(true)
    setRefPaperIds(new Set())
  }

  const runReferences = async () => {
    if (refPaperIds.size === 0) {
      onToast('先勾选至少一篇文献')
      return
    }
    if (refEngine === 'csl') {
      const styleRow = (refStyles ?? []).find((row) => row.key === refStyle)
      if (!styleRow?.csl_available || !cslEngineAvailable) {
        onToast('这个格式没有可用的 CSL 引擎，先切回手写模板')
        return
      }
    }
    setRefBusy(true)
    setError(null)
    try {
      const result = await formatReferences({
        paperIds: [...refPaperIds],
        style: refStyle,
        numbered: refNumbered,
        template: refStyle === 'custom' ? refTemplate || null : null,
        engine: refEngine,
      })
      setRefResult(result)
      onToast(
        `已生成 ${result.paper_count} 条 references（${result.label}${result.engine === 'csl' ? ' · CSL' : ''}）`,
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setRefBusy(false)
    }
  }

  // CSL JSON 导出：给 pandoc / Zotero 用的通用中间格式（只读派生，不落库）
  const downloadCslJson = async () => {
    if (refPaperIds.size === 0) {
      onToast('先勾选至少一篇文献')
      return
    }
    setCslJsonBusy(true)
    setError(null)
    try {
      const payload = await exportReferenceCslJson([...refPaperIds])
      downloadText('references.csl.json', JSON.stringify(payload.items, null, 2))
      onToast(`已导出 ${payload.count} 条 CSL JSON`)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setCslJsonBusy(false)
    }
  }

  const saveBiblio = async () => {
    if (!biblioPaperId) return
    setBiblioBusy(true)
    setError(null)
    try {
      const updated = await patchPaper(biblioPaperId, {
        volume: biblioVolume.trim(),
        issue: biblioIssue.trim(),
        pages: biblioPages.trim(),
      })
      setPapers((prev) => prev.map((paper) => (paper.id === updated.id ? updated : paper)))
      onToast('卷期页已保存，重新生成即可生效')
      setRefResult(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBiblioBusy(false)
    }
  }

  const resetMeta = () => {
    setMetaResult(null)
    setMetaSelected(new Set())
    setMetaOverwrite(false)
    setMetaError('')
  }

  const runMetadataLookup = async (
    mode: 'auto' | 'search' | 'doi',
    opts: { doi?: string; query?: string } = {},
  ) => {
    if (!biblioPaperId) {
      setMetaError('先选一篇文献')
      return
    }
    setMetaBusy(mode === 'search' ? 'search' : 'lookup')
    setMetaError('')
    try {
      const result = await lookupPaperMetadata(biblioPaperId, {
        mode,
        doi: opts.doi ?? null,
        query: opts.query ?? metaQuery.trim() ?? null,
      })
      setMetaResult(result)
      setMetaSelected(new Set(result.fields.filter((row) => row.empty).map((row) => row.field)))
      setMetaOverwrite(false)
      if (mode === 'search') {
        onToast(result.candidates.length > 0 ? `找到 ${result.candidates.length} 条候选` : '没有候选')
      }
    } catch (err) {
      setMetaError(err instanceof Error ? err.message : String(err))
    } finally {
      setMetaBusy(null)
    }
  }

  const toggleMetaField = (field: string) => {
    setMetaSelected((prev) => {
      const next = new Set(prev)
      if (next.has(field)) next.delete(field)
      else next.add(field)
      return next
    })
  }

  const applyMeta = async () => {
    if (!metaResult || metaSelected.size === 0) return
    if (metaSelectedHasConflict(metaResult, metaSelected) && !metaOverwrite) {
      setMetaError('选中的字段已有值：勾选「覆盖已有值」后再应用')
      return
    }
    const fields: Record<string, unknown> = {}
    for (const row of metaResult.fields) {
      if (metaSelected.has(row.field)) fields[row.field] = row.suggested
    }
    setMetaBusy('apply')
    setMetaError('')
    try {
      const updated = await applyPaperMetadata(biblioPaperId, fields, metaOverwrite)
      setPapers((prev) => prev.map((paper) => (paper.id === updated.id ? updated : paper)))
      onToast(`已补全 ${Object.keys(fields).length} 个字段，重新生成条目即可生效`)
      resetMeta()
      setRefResult(null)
    } catch (err) {
      setMetaError(err instanceof Error ? err.message : String(err))
    } finally {
      setMetaBusy(null)
    }
  }

  const runCitationCheck = async () => {
    if (!citeText.trim()) return
    setCiteBusy(true)
    setCiteResult(null)
    try {
      setCiteResult(await checkCitations(citeText))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setCiteBusy(false)
    }
  }

  const toggleThesisClaim = (id: string) => {
    setThesisClaimIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleThesisPaper = (id: string) => {
    setThesisPaperIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const runThesisExport = async () => {
    setThesisBusy(true)
    setError(null)
    try {
      const chapterOrder = thesisChapterOrder
        .split(/[,，、\n]+/)
        .map((value) => value.trim())
        .filter(Boolean)
      const yearValue = thesisYear.trim()
      const result = await exportThesis({
        title: thesisTitle.trim(),
        author: thesisAuthor.trim(),
        school: thesisSchool.trim(),
        degree: thesisDegree.trim(),
        advisor: thesisAdvisor.trim(),
        year: yearValue && Number.isFinite(Number(yearValue)) ? Number(yearValue) : null,
        paperIds: thesisPaperIds.size > 0 ? [...thesisPaperIds] : undefined,
        claimIds: thesisClaimIds.size > 0 ? [...thesisClaimIds] : undefined,
        chapterOrder: chapterOrder.length > 0 ? chapterOrder : undefined,
        includeDrafts: thesisIncludeDrafts,
      })
      setThesisExport(result)
      setThesisCheckText(result.content)
      setThesisCheck(null)
      onToast(`已生成 ${result.filename}（${result.claim_count} 张卡片 / ${result.paper_count} 篇文献）`)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setThesisBusy(false)
    }
  }

  const runThesisCheck = async () => {
    if (!thesisCheckText.trim()) return
    setThesisCheckBusy(true)
    setThesisCheck(null)
    try {
      setThesisCheck(
        await checkThesis({
          text: thesisCheckText,
          paperIds: thesisPaperIds.size > 0 ? [...thesisPaperIds] : undefined,
        }),
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setThesisCheckBusy(false)
    }
  }

  const filteredRefPapers = useMemo(() => {
    const query = refQuery.trim().toLowerCase()
    if (!query) return papers
    return papers.filter((paper) => paper.title.toLowerCase().includes(query))
  }, [papers, refQuery])

  const activeRefStyle = (refStyles ?? []).find((row) => row.key === refStyle) ?? null
  const cslSelectable = Boolean(activeRefStyle?.csl_available) && cslEngineAvailable

  const filteredMatrixPapers = useMemo(() => {
    const query = matrixQuery.trim().toLowerCase()
    const list = papers.filter((paper) => paper.has_version !== false)
    if (!query) return list
    return list.filter((paper) => paper.title.toLowerCase().includes(query))
  }, [papers, matrixQuery])

  const filteredThesisPapers = useMemo(() => {
    const query = thesisPaperQuery.trim().toLowerCase()
    if (!query) return papers
    return papers.filter((paper) => paper.title.toLowerCase().includes(query))
  }, [papers, thesisPaperQuery])

  if (!open) return null

  const approvedCount = (claims ?? []).filter((claim) => claim.status === 'approved').length

  return (
    <div className="proposal-backdrop" role="presentation" onPointerDown={onClose}>
      <aside
        className="proposal-panel writing-panel"
        role="dialog"
        aria-modal="true"
        aria-label="写作台"
        onPointerDown={(event) => event.stopPropagation()}
      >
        <header className="proposal-head">
          <div>
            <strong>写作台</strong>
            <span className="muted">
              {' '}
              · {claims ? `${claims.length} 张卡片 · ${approvedCount} 张已认可` : '载入中…'}
            </span>
          </div>
          <div className="proposal-head-actions">
            <button className="close" onClick={onClose} aria-label="关闭">
              ✕
            </button>
          </div>
        </header>

        <div className="proposal-tabs" role="tablist" aria-label="写作台视图">
          {(['material', 'outline', 'matrix', 'export', 'refs', 'thesis'] as Tab[]).map((name) => (
            <button
              key={name}
              role="tab"
              aria-selected={tab === name}
              tabIndex={tab === name ? 0 : -1}
              className={`proposal-tab ${tab === name ? 'on' : ''}`}
              onClick={() => setTab(name)}
            >
              {TAB_LABEL[name]}
              {name === 'material' && claims && claims.length > 0 && (
                <span className="count">{claims.length}</span>
              )}
            </button>
          ))}
          <span className="spacer" />
          <button className="ghost small" onClick={() => void loadClaims()} title="刷新素材列表">
            刷新
          </button>
        </div>

        {error && (
          <div className="banner error proposal-error">
            {error}
            <button className="link" onClick={() => setError(null)}>
              知道了
            </button>
          </div>
        )}

        {tab === 'material' && (
          <div className="writing-material">
            <section className="writing-form">
              <h3>新卡片</h3>
              <label className="writing-field">
                <span>文献</span>
                <select
                  value={paperId}
                  onChange={(event) => setPaperId(event.target.value)}
                  disabled={busy}
                >
                  <option value="">选一篇已解析的文献…</option>
                  {papers.map((paper) => (
                    <option key={paper.id} value={paper.id}>
                      {paper.title}
                      {paper.has_version === false ? '（未解析）' : ''}
                    </option>
                  ))}
                </select>
              </label>

              <label className="writing-field">
                <span>章节</span>
                <input
                  list="writing-sections"
                  value={section}
                  onChange={(event) => setSection(event.target.value)}
                  placeholder="引言 / 方法 / 讨论…"
                  disabled={busy}
                />
                <datalist id="writing-sections">
                  {SECTION_PRESETS.map((preset) => (
                    <option key={preset} value={preset} />
                  ))}
                </datalist>
              </label>

              <label className="writing-field">
                <span>论断</span>
                <textarea
                  rows={3}
                  value={text}
                  onChange={(event) => setText(event.target.value)}
                  placeholder="一句能写进论文、且能被下面证据支持的话。例：膜状凝结是工程设计的依据。"
                  disabled={busy}
                />
              </label>

              <div className="writing-field">
                <span>
                  证据 <em className="muted">（必选，导出时自动生成引用）</em>
                </span>
                {!paperId && <p className="sum-muted">先选文献，再挑图 / 表 / 整篇速览。</p>}
                {paperId && evidenceLoading && <p className="sum-muted">载入证据…</p>}
                {paperId && !evidenceLoading && evidenceOptions.length === 0 && (
                  <p className="sum-muted">
                    这篇文献没有可引用的图/表/摘要（可能尚未解析出图）。
                  </p>
                )}
                {evidenceOptions.length > 0 && (
                  <ul className="writing-evidence-list">
                    {evidenceOptions.map((option) => (
                      <li key={option.key}>
                        <label className={evidenceIds.has(option.key) ? 'on' : ''}>
                          <input
                            type="checkbox"
                            checked={evidenceIds.has(option.key)}
                            onChange={() => toggleEvidence(option.key)}
                          />
                          <span className="writing-evidence-label">
                            <b>{option.type === 'summary' ? '速览' : option.type === 'table' ? '表' : '图'}</b>
                            {option.label}
                          </span>
                          <span className="writing-evidence-detail">{option.detail}</span>
                        </label>
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              <div className="writing-row">
                <label className="writing-field">
                  <span>标签</span>
                  <input
                    value={tags}
                    onChange={(event) => setTags(event.target.value)}
                    placeholder="逗号分隔，可选"
                    disabled={busy}
                  />
                </label>
                <label className="writing-field">
                  <span>状态</span>
                  <select
                    value={status}
                    onChange={(event) => setStatus(event.target.value as 'draft' | 'approved')}
                    disabled={busy}
                  >
                    <option value="draft">草稿</option>
                    <option value="approved">已认可</option>
                  </select>
                </label>
              </div>

              <button
                className="primary"
                disabled={busy || !paperId || !text.trim() || evidenceIds.size === 0}
                onClick={() => void submitClaim()}
              >
                {busy ? '保存中…' : '保存卡片'}
              </button>
            </section>

            <section className="writing-list">
              <h3>已收集的卡片</h3>
              {claims === null && <p className="sum-muted">载入中…</p>}
              {claims && claims.length === 0 && (
                <div className="empty writing-empty">
                  <h3>还没有写作卡片</h3>
                  <p>选文献 → 挑证据 → 写一句能进稿子的论断。</p>
                </div>
              )}
              {groupedClaims.map(([group, rows]) => (
                <div className="writing-group" key={group}>
                  <h4>{group}</h4>
                  <ul>
                    {rows.map((claim) => (
                      <li className="writing-card" key={claim.claim_id}>
                        <div className="writing-card-head">
                          <span className={`proposal-status ${claim.status}`}>
                            {claim.status === 'approved' ? '已认可' : '草稿'}
                          </span>
                          <span className="writing-card-paper">
                            {paperTitle.get(claim.paper_id) ?? claim.paper_id.slice(0, 8)}
                          </span>
                          <span className="spacer" />
                          <button
                            className={`link ${historyClaimId === claim.claim_id ? 'active' : ''}`}
                            onClick={() => void toggleHistory(claim)}
                            title="查看/恢复这张卡片的修改历史"
                          >
                            历史
                          </button>
                          <button
                            className="link"
                            onClick={() => {
                              setEditingId(claim.claim_id)
                              setEditingText(claim.text)
                            }}
                          >
                            改
                          </button>
                          <button className="link" onClick={() => void toggleStatus(claim)}>
                            {claim.status === 'approved' ? '转草稿' : '认可'}
                          </button>
                          <button className="link danger" onClick={() => void removeClaim(claim)}>
                            删
                          </button>
                        </div>
                        {editingId === claim.claim_id ? (
                          <div className="writing-edit">
                            <textarea
                              rows={3}
                              value={editingText}
                              onChange={(event) => setEditingText(event.target.value)}
                            />
                            <div className="writing-edit-actions">
                              <button className="primary small" onClick={() => void saveEdit(claim)}>
                                保存
                              </button>
                              <button className="ghost small" onClick={() => setEditingId(null)}>
                                取消
                              </button>
                            </div>
                          </div>
                        ) : (
                          <p className="writing-card-text">{claim.text}</p>
                        )}
                        <div className="writing-card-foot">
                          {claim.evidence_stale && (
                            <span
                              className="writing-chip bad"
                              title="重解析后部分证据失效，请编辑卡片重新选择证据"
                            >
                              证据失效
                            </span>
                          )}
                          {claim.evidence.map((item) => (
                            <span className="writing-chip" key={`${item.type}:${item.id}`}>
                              {item.type === 'figure' ? '图' : item.type === 'table' ? '表' : item.type === 'summary' ? '速览' : '块'}{' '}
                              {item.label || item.id.slice(0, 8)}
                            </span>
                          ))}
                          {claim.cite_key && <span className="writing-cite">{claim.cite_key}</span>}
                          <span className="muted">{formatTime(claim.updated_at)}</span>
                        </div>
                        {historyClaimId === claim.claim_id && (
                          <div className="writing-history">
                            <div className="writing-history-head">
                              <strong>编辑历史</strong>
                              <span className="muted">
                                {historyBusy
                                  ? '载入中…'
                                  : historyRows === null
                                    ? ''
                                    : `${historyRows.length} 条`}
                              </span>
                              <span className="spacer" />
                              <button
                                className="ghost small"
                                onClick={() => {
                                  setHistoryClaimId(null)
                                  setHistoryRows(null)
                                }}
                              >
                                收起
                              </button>
                            </div>
                            {historyRows && historyRows.length === 0 && (
                              <p className="sum-muted">还没有修改记录；每次改动前会自动留一份。</p>
                            )}
                            {historyRows && historyRows.length > 0 && (
                              <ul className="writing-history-list">
                                {historyRows.map((revision) => (
                                  <li key={revision.revision_id}>
                                    <div className="writing-history-meta">
                                      <span>{formatTime(revision.created_at)}</span>
                                      <span
                                        className={`writing-history-reason ${revision.reason}`}
                                      >
                                        {revision.reason === 'restored'
                                          ? '恢复前快照'
                                          : '编辑前版本'}
                                      </span>
                                      {revision.edited_by && (
                                        <span className="muted">by {revision.edited_by}</span>
                                      )}
                                      <span className="muted">{revision.section}</span>
                                      <span className="spacer" />
                                      <button
                                        className="ghost small"
                                        disabled={historyBusy}
                                        onClick={() => void restoreHistory(claim, revision)}
                                      >
                                        恢复
                                      </button>
                                    </div>
                                    <p className="writing-history-text">{revision.text}</p>
                                    {revision.tags.length > 0 && (
                                      <div className="writing-history-tags">
                                        {revision.tags.map((tag) => (
                                          <span key={tag}>#{tag}</span>
                                        ))}
                                      </div>
                                    )}
                                  </li>
                                ))}
                              </ul>
                            )}
                          </div>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </section>
          </div>
        )}

        {tab === 'outline' && (
          <div className="writing-export">
            <section className="writing-export-controls">
              <h3>写作大纲</h3>
              <div className="writing-row">
                <label className="writing-check-inline">
                  <input
                    type="checkbox"
                    checked={outlineDrafts}
                    onChange={(event) => {
                      setOutlineDrafts(event.target.checked)
                      setOutline(null)
                      setOutlineAttempted(false)
                    }}
                  />
                  包含草稿卡片
                </label>
                <button
                  className="primary small"
                  disabled={outlineBusy}
                  onClick={() => void runOutline()}
                >
                  {outlineBusy ? '整理中…' : '重新生成大纲'}
                </button>
              </div>
              <p className="sum-muted">
                按章节别名归章（引言→绪论、实验结果→实验与结果…），章节内按排序号排列。
                正文提到的图/表与卡片证据对不上、或引用键解析不了，会在卡片下列出。
              </p>
              {outline && (
                <p className="sum-muted">
                  {outline.total_claims} 张卡片 · {outline.sections.length} 个章节 ·{' '}
                  {outline.issue_count} 条检查提示
                </p>
              )}
              {outline && (
                <div className="writing-row">
                  <button
                    className="ghost small"
                    onClick={() =>
                      void copyText(outline.markdown).then(() => onToast('已复制大纲 Markdown'))
                    }
                  >
                    复制大纲
                  </button>
                  <button
                    className="ghost small"
                    onClick={() => downloadText(outline.filename, outline.markdown)}
                  >
                    下载 Markdown
                  </button>
                </div>
              )}
            </section>
            <section className="writing-export-preview">
              {!outline && !outlineBusy && (
                <p className="sum-muted">还没有大纲；点「重新生成大纲」。</p>
              )}
              {outline && outline.sections.length === 0 && (
                <p className="sum-muted">有卡片但没归出章节。</p>
              )}
              {outline &&
                outline.sections.map((section) => (
                  <div className="writing-outline-section" key={section.section}>
                    <h3>
                      {section.section}
                      <span className="muted">{section.claim_count} 张</span>
                    </h3>
                    <ul className="writing-outline-list">
                      {section.claims.map((claim) => (
                        <li className="writing-outline-claim" key={claim.claim_id}>
                          <div className="writing-outline-text">{claim.text}</div>
                          <div className="sum-muted">
                            {claim.paper_title ?? '未知文献'} ·{' '}
                            {claim.status === 'approved' ? '已认可' : '草稿'}
                            {claim.cite_key ? ` · ${claim.cite_key}` : ''}
                            {claim.figure_labels.length > 0
                              ? ` · 图证据：${claim.figure_labels.join('、')}`
                              : ''}
                            {claim.table_labels.length > 0
                              ? ` · 表证据：${claim.table_labels.join('、')}`
                              : ''}
                          </div>
                          {claim.issues.map((issue, index) => (
                            <p className="writing-ref-warn" key={`${issue.kind}-${index}`}>
                              ⚠ {issue.message}
                            </p>
                          ))}
                          {claim.notes.length > 0 && (
                            <p className="sum-muted">{claim.notes.join('；')}</p>
                          )}
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
            </section>
          </div>
        )}

        {tab === 'matrix' && (
          <div className="writing-matrix">
            <div className="writing-matrix-picker">
              <div className="writing-matrix-head">
                <strong>选择文献（{matrixPaperIds.size}/8）</strong>
                <input
                  value={matrixQuery}
                  onChange={(event) => setMatrixQuery(event.target.value)}
                  placeholder="按标题过滤…"
                />
                <button className="primary small" disabled={matrixBusy} onClick={() => void runMatrix()}>
                  {matrixBusy ? '生成中…' : '生成矩阵'}
                </button>
              </div>
              <ul className="writing-paper-list">
                {filteredMatrixPapers.map((paper) => (
                  <li key={paper.id}>
                    <label className={matrixPaperIds.has(paper.id) ? 'on' : ''}>
                      <input
                        type="checkbox"
                        checked={matrixPaperIds.has(paper.id)}
                        onChange={() => toggleMatrixPaper(paper.id)}
                      />
                      <span>{paper.title}</span>
                      <em>
                        {paper.year ?? '—'} · {paper.figure_count} 图
                      </em>
                    </label>
                  </li>
                ))}
              </ul>
            </div>
            <div className="writing-matrix-result">
              {matrixDims && matrixDims.length > 0 && (
                <section className="writing-matrix-dims">
                  <div className="writing-matrix-dims-head">
                    <strong>维度（{matrixSelected.size}/{matrixDims.length}）</strong>
                    <button className="link" onClick={selectAllMatrixDims}>
                      全选
                    </button>
                    <button className="link" onClick={resetMatrixDims}>
                      重置默认
                    </button>
                    <span className="sum-muted">改完维度后重新「生成矩阵」</span>
                  </div>
                  <ul className="writing-dim-list">
                    {matrixDims.map((dim) => (
                      <li key={dim.key}>
                        <label
                          className={matrixSelected.has(dim.key) ? 'on' : ''}
                          title={dim.description}
                        >
                          <input
                            type="checkbox"
                            checked={matrixSelected.has(dim.key)}
                            onChange={() => toggleMatrixDim(dim.key)}
                          />
                          {dim.label}
                        </label>
                      </li>
                    ))}
                  </ul>
                </section>
              )}
              {!matrix && <p className="sum-muted">勾选文献后点「生成矩阵」，不会写库，随时可重算。</p>}
              {matrix && (
                <div className="writing-table-wrap">
                  <table className="writing-table">
                    <thead>
                      <tr>
                        <th className="writing-row-label">维度</th>
                        {matrix.papers.map((paper) => (
                          <th key={paper.paper_id}>
                            <b>{paper.title}</b>
                            <span>
                              {paper.year ?? '—'} · {paper.cite_key ?? '无 cite_key'}
                            </span>
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {matrix.rows.map((row) => (
                        <tr key={row.key}>
                          <th className="writing-row-label">{row.label}</th>
                          {row.values.map((cell) => (
                            <td key={cell.paper_id}>{cell.text}</td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <p className="sum-muted">{matrix.note}</p>
                </div>
              )}
            </div>
          </div>
        )}

        {tab === 'export' && (
          <div className="writing-export">
            <section className="writing-export-controls">
              <div className="writing-row">
                <label className="writing-field">
                  <span>标题</span>
                  <input
                    value={exportTitle}
                    onChange={(event) => setExportTitle(event.target.value)}
                    placeholder="例：纳米流体强化传热研究进展"
                  />
                </label>
                <label className="writing-field">
                  <span>格式</span>
                  <select
                    value={fmt}
                    onChange={(event) =>
                      setFmt(event.target.value as 'latex' | 'bibtex' | 'markdown')
                    }
                  >
                    <option value="latex">LaTeX + refs.bib</option>
                    <option value="bibtex">BibTeX（仅文献）</option>
                    <option value="markdown">Markdown</option>
                  </select>
                </label>
              </div>
              <div className="writing-checks">
                <label>
                  <input
                    type="checkbox"
                    checked={includeDrafts}
                    onChange={(event) => setIncludeDrafts(event.target.checked)}
                  />
                  包含草稿卡片
                </label>
                <label>
                  <input
                    type="checkbox"
                    checked={withComments}
                    onChange={(event) => setWithComments(event.target.checked)}
                  />
                  带证据注释（% [claim …]）
                </label>
                <button
                  className="primary small"
                  disabled={exportBusy || (claims !== null && claims.length === 0)}
                  onClick={() => void runExport()}
                >
                  {exportBusy ? '生成中…' : '生成'}
                </button>
                <button
                  className="ghost small"
                  disabled={
                    docBusy !== null ||
                    (claims !== null && claims.length === 0) ||
                    (docCaps !== null && !docCaps.docx)
                  }
                  title={
                    docCaps && !docCaps.docx
                      ? docCaps.note || '当前环境没有 pandoc，Word 导出不可用'
                      : 'claim 草稿导出为 Word（pandoc + 官方 CSL 参考文献）'
                  }
                  onClick={() => void runDocExport('docx')}
                >
                  {docBusy === 'docx' ? '导出中…' : '下载 Word（.docx）'}
                </button>
                <button
                  className="ghost small"
                  disabled={
                    docBusy !== null ||
                    (claims !== null && claims.length === 0) ||
                    (docCaps !== null && !docCaps.pdf)
                  }
                  title={
                    docCaps && !docCaps.pdf
                      ? docCaps.note || '当前环境没有 typst，PDF 导出不可用'
                      : 'claim 草稿导出为 PDF（pandoc + typst 引擎）'
                  }
                  onClick={() => void runDocExport('pdf')}
                >
                  {docBusy === 'pdf' ? '导出中…' : '下载 PDF'}
                </button>
              </div>
              {docCaps?.note && <p className="sum-muted">{docCaps.note}</p>}
              <p className="sum-muted">
                Word/PDF 用 pandoc + 官方 CSL 样式生成，正文只来自 claim 卡片（图表排版仍留占位）。
              </p>
              {claims !== null && claims.length === 0 && (
                <p className="sum-muted">还没有卡片可导出；先去「素材」页签写一张。</p>
              )}
            </section>

            <section className="writing-export-preview">
              {!exported && <p className="sum-muted">生成后在这里预览；LaTeX 会额外给出 refs.bib。</p>}
              {exported && (
                <>
                  <div className="writing-export-head">
                    <strong>{exported.filename}</strong>
                    <span className="muted">
                      {exported.claim_count} 张卡片 · {exported.paper_count} 篇文献
                    </span>
                    <span className="spacer" />
                    <button
                      className="ghost small"
                      onClick={() => void copyText(exported.content).then(() => onToast('已复制正文'))}
                    >
                      复制
                    </button>
                    <button
                      className="primary small"
                      onClick={() => downloadText(exported.filename, exported.content)}
                    >
                      下载
                    </button>
                  </div>
                  <pre className="writing-code">{exported.content}</pre>
                  {Object.entries(exported.extra_files).map(([name, content]) => (
                    <div className="writing-extra" key={name}>
                      <div className="writing-export-head">
                        <strong>{name}</strong>
                        <span className="spacer" />
                        <button
                          className="ghost small"
                          onClick={() => void copyText(content).then(() => onToast(`已复制 ${name}`))}
                        >
                          复制
                        </button>
                        <button className="ghost small" onClick={() => downloadText(name, content)}>
                          下载
                        </button>
                      </div>
                      <pre className="writing-code">{content}</pre>
                    </div>
                  ))}
                  <button
                    className="ghost small"
                    onClick={() => {
                      setCiteText(exported.content)
                      setCiteResult(null)
                    }}
                  >
                    用这份稿子做引用校验
                  </button>
                </>
              )}
            </section>

            <section className="writing-cite-check">
              <h3>引用校验</h3>
              <p className="sum-muted">
                贴一段带 <code>\cite{'{'}key{'}'}</code> 的 LaTeX 或正文，检查每个键能否在文献库解析；
                不调模型，只做确定性匹配。
              </p>
              <textarea
                rows={4}
                value={citeText}
                onChange={(event) => setCiteText(event.target.value)}
                placeholder="例：膜状凝结是工程设计的依据~\\cite{doi:10.1234/abc}"
              />
              <button
                className="primary small"
                disabled={citeBusy || !citeText.trim()}
                onClick={() => void runCitationCheck()}
              >
                {citeBusy ? '校验中…' : '校验引用'}
              </button>
              {citeResult && (
                <div className={`writing-cite-result ${citeResult.unknown > 0 ? 'warn' : 'ok'}`}>
                  <strong>{citeResult.message}</strong>
                  <ul>
                    {citeResult.citations.map((item) => (
                      <li key={item.key}>
                        <code>{item.key}</code>
                        {item.status === 'resolved' ? (
                          <span className="writing-chip ok">可解析</span>
                        ) : (
                          <span className="writing-chip bad">找不到</span>
                        )}
                        {item.status === 'resolved' && <span>{item.title}</span>}
                        {item.status !== 'resolved' && item.suggestion && (
                          <span className="sum-muted">建议：{item.suggestion}</span>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </section>
          </div>
        )}

        {tab === 'thesis' && (
          <div className="writing-thesis">
            <section className="writing-export-controls">
              <h3>学位论文 LaTeX 骨架</h3>
              <p className="sum-muted">
                把 claim 卡片装进 ctexbook 骨架，附 refs.bib 与 GB/T 参考文献。
                封面、页边距、页眉按学校模板改；正文由你补写，工具不代写。
              </p>
              <div className="writing-row">
                <label className="writing-field">
                  <span>题目</span>
                  <input
                    value={thesisTitle}
                    onChange={(event) => setThesisTitle(event.target.value)}
                    placeholder="例：微通道传热预测方法研究"
                  />
                </label>
                <label className="writing-field">
                  <span>作者</span>
                  <input
                    value={thesisAuthor}
                    onChange={(event) => setThesisAuthor(event.target.value)}
                    placeholder="姓名"
                  />
                </label>
              </div>
              <div className="writing-row writing-row-3">
                <label className="writing-field">
                  <span>学校</span>
                  <input
                    value={thesisSchool}
                    onChange={(event) => setThesisSchool(event.target.value)}
                    placeholder="例：清华大学"
                  />
                </label>
                <label className="writing-field">
                  <span>学位</span>
                  <input
                    value={thesisDegree}
                    onChange={(event) => setThesisDegree(event.target.value)}
                    placeholder="博士 / 硕士"
                  />
                </label>
                <label className="writing-field">
                  <span>导师</span>
                  <input
                    value={thesisAdvisor}
                    onChange={(event) => setThesisAdvisor(event.target.value)}
                    placeholder="导师姓名"
                  />
                </label>
              </div>
              <div className="writing-row">
                <label className="writing-field">
                  <span>年份</span>
                  <input
                    value={thesisYear}
                    inputMode="numeric"
                    onChange={(event) => setThesisYear(event.target.value)}
                    placeholder="2026"
                  />
                </label>
                <label className="writing-field">
                  <span>章节顺序（逗号分隔，留空用默认）</span>
                  <input
                    value={thesisChapterOrder}
                    onChange={(event) => setThesisChapterOrder(event.target.value)}
                    placeholder="摘要, 绪论, 相关工作, 方法, 实验与结果, 结论"
                  />
                </label>
              </div>
              <div className="writing-checks">
                <label>
                  <input
                    type="checkbox"
                    checked={thesisIncludeDrafts}
                    onChange={(event) => setThesisIncludeDrafts(event.target.checked)}
                  />
                  包含草稿卡片
                </label>
                <button
                  className="primary small"
                  disabled={thesisBusy}
                  onClick={() => void runThesisExport()}
                >
                  {thesisBusy ? '生成中…' : '生成 LaTeX 包'}
                </button>
              </div>
              {thesisExport && thesisExport.warnings.length > 0 && (
                <ul className="writing-ref-warn">
                  {thesisExport.warnings.map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
              )}
            </section>

            <section className="writing-export-controls">
              <h3>导出范围（不勾选 = 全部可见）</h3>
              <p className="sum-muted">文献决定参考文献；卡片决定正文骨架。</p>
              <details className="writing-biblio">
                <summary>
                  卡片（{thesisClaimIds.size > 0 ? `已选 ${thesisClaimIds.size}` : '全部'}）
                </summary>
                <ul className="writing-paper-list">
                  {(claims ?? []).map((claim) => (
                    <li key={claim.claim_id}>
                      <label className={thesisClaimIds.has(claim.claim_id) ? 'on' : ''}>
                        <input
                          type="checkbox"
                          checked={thesisClaimIds.has(claim.claim_id)}
                          onChange={() => toggleThesisClaim(claim.claim_id)}
                        />
                        <span>
                          {claim.section || '正文'}：{claim.text.slice(0, 60)}
                        </span>
                        <em>
                          {claim.paper_title}
                          {claim.status === 'draft' ? ' · 草稿' : ''}
                        </em>
                      </label>
                    </li>
                  ))}
                </ul>
                {claims !== null && claims.length === 0 && (
                  <p className="sum-muted">还没有卡片；先去「素材」页签写一张。</p>
                )}
              </details>

              <details className="writing-biblio">
                <summary>
                  参考文献（{thesisPaperIds.size > 0 ? `已选 ${thesisPaperIds.size}` : '全部'}）
                </summary>
                <div className="writing-ref-paper-head">
                  <input
                    value={thesisPaperQuery}
                    onChange={(event) => setThesisPaperQuery(event.target.value)}
                    placeholder="按标题过滤…"
                  />
                  <button
                    className="link"
                    onClick={() => setThesisPaperIds(new Set(papers.map((paper) => paper.id)))}
                  >
                    全选
                  </button>
                  <button className="link" onClick={() => setThesisPaperIds(new Set())}>
                    清空
                  </button>
                </div>
                <ul className="writing-paper-list writing-ref-paper-list">
                  {filteredThesisPapers.map((paper) => (
                    <li key={paper.id}>
                      <label className={thesisPaperIds.has(paper.id) ? 'on' : ''}>
                        <input
                          type="checkbox"
                          checked={thesisPaperIds.has(paper.id)}
                          onChange={() => toggleThesisPaper(paper.id)}
                        />
                        <span>{paper.title}</span>
                        <em>
                          {paper.entry_type === 'thesis'
                            ? `学位论文 · ${paper.thesis_school ?? '缺学校'}`
                            : `${paper.year ?? '—'} · ${paper.venue ?? '无刊名'}`}
                        </em>
                      </label>
                    </li>
                  ))}
                </ul>
              </details>
            </section>

            {thesisExport && (
              <section className="writing-export-preview">
                <div className="writing-export-head">
                  <strong>{thesisExport.filename}</strong>
                  <span className="muted">
                    {thesisExport.claim_count} 张卡片 · {thesisExport.paper_count} 篇文献 ·{' '}
                    {thesisExport.chapter_count} 章
                  </span>
                  <span className="spacer" />
                  <button
                    className="ghost small"
                    onClick={() =>
                      void copyText(thesisExport.content).then(() => onToast('已复制正文'))
                    }
                  >
                    复制
                  </button>
                  <button
                    className="primary small"
                    onClick={() => downloadText(thesisExport.filename, thesisExport.content)}
                  >
                    下载 .tex
                  </button>
                </div>
                <pre className="writing-code">{thesisExport.content}</pre>
                {Object.entries(thesisExport.extra_files).map(([name, content]) => (
                  <div className="writing-extra" key={name}>
                    <div className="writing-export-head">
                      <strong>{name}</strong>
                      <span className="spacer" />
                      <button
                        className="ghost small"
                        onClick={() => void copyText(content).then(() => onToast(`已复制 ${name}`))}
                      >
                        复制
                      </button>
                      <button className="ghost small" onClick={() => downloadText(name, content)}>
                        下载
                      </button>
                    </div>
                    <pre className="writing-code">{content}</pre>
                  </div>
                ))}
                <button
                  className="ghost small"
                  onClick={() => {
                    setThesisCheckText(thesisExport.content)
                    setThesisCheck(null)
                  }}
                >
                  用这份稿子做交叉检查
                </button>
              </section>
            )}

            <section className="writing-cite-check">
              <h3>引用 / 图表标签交叉检查</h3>
              <p className="sum-muted">
                扫描 <code>\cite{'{'}key{'}'}</code>、<code>\ref{'{'}fig:*{'}'}</code>、
                <code>\ref{'{'}tab:*{'}'}</code>，与库内 cite_key / 图号 / 表号比对；只做词面检查。
              </p>
              <textarea
                rows={8}
                value={thesisCheckText}
                onChange={(event) => setThesisCheckText(event.target.value)}
                placeholder="粘贴导出的 .tex（或你改过的稿子）…"
              />
              <button
                className="primary small"
                disabled={thesisCheckBusy || !thesisCheckText.trim()}
                onClick={() => void runThesisCheck()}
              >
                {thesisCheckBusy ? '检查中…' : '检查引用'}
              </button>
              {thesisCheck && (
                <div
                  className={`writing-cite-result ${
                    thesisCheck.missing.length > 0 ? 'warn' : 'ok'
                  }`}
                >
                  <strong>{thesisCheck.message}</strong>
                  {thesisCheck.citations.length > 0 && (
                    <ul>
                      {thesisCheck.citations.map((item) => (
                        <li key={item.key}>
                          <code>{item.key}</code>
                          {item.status === 'resolved' ? (
                            <span className="writing-chip ok">可解析</span>
                          ) : (
                            <span className="writing-chip bad">找不到</span>
                          )}
                          {item.status === 'resolved' && <span>{item.title}</span>}
                          {item.status !== 'resolved' && item.suggestion && (
                            <span className="sum-muted">建议：{item.suggestion}</span>
                          )}
                        </li>
                      ))}
                    </ul>
                  )}
                  {thesisCheck.missing.length > 0 && (
                    <p>找不到：{thesisCheck.missing.join('、')}</p>
                  )}
                  {thesisCheck.unused.length > 0 && (
                    <p className="sum-muted">未引用：{thesisCheck.unused.join('、')}</p>
                  )}
                  {thesisCheck.warnings.length > 0 && (
                    <ul>
                      {thesisCheck.warnings.map((warning) => (
                        <li key={warning}>{warning}</li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </section>
          </div>
        )}

        {tab === 'refs' && (
          <div className="writing-refs">
            <section className="writing-ref-controls">
              <h3>参考文献条目</h3>
              <label className="writing-field">
                <span>格式预设（{refStyles?.length ?? 0} 种）</span>
                <select
                  value={refStyle}
                  onChange={(event) => {
                    setRefStyle(event.target.value)
                    setRefResult(null)
                    const next = (refStyles ?? []).find(
                      (row) => row.key === event.target.value,
                    )
                    if (!next?.csl_available) setRefEngine('template')
                  }}
                >
                  {(refStyles ?? []).map((row) => (
                    <option key={row.key} value={row.key}>
                      {row.label}
                    </option>
                  ))}
                </select>
              </label>
              {activeRefStyle && (
                <div className="writing-ref-sample">
                  <span>样例</span>
                  <code>{activeRefStyle.sample}</code>
                  <p className="sum-muted">{activeRefStyle.description}</p>
                </div>
              )}
              <div className="writing-ref-engine">
                <label className="writing-check-inline">
                  <input
                    type="checkbox"
                    checked={refEngine === 'csl'}
                    disabled={!cslSelectable}
                    onChange={(event) => {
                      setRefEngine(event.target.checked ? 'csl' : 'template')
                      setRefResult(null)
                    }}
                  />
                  用官方 CSL 样式渲染（pandoc）
                </label>
                <p className="sum-muted">
                  {activeRefStyle?.csl_available
                    ? 'CSL 引擎走官方 GB/T 7714-2015 / APA 7 / IEEE 模板，条目顺序与编号按样式规则；输出纯文本。'
                    : '当前格式没有内置 CSL 样式，只能用手写模板。'}
                  {cslNote ? ` ${cslNote}` : ''}
                </p>
              </div>
              {refStyle === 'custom' && (
                <label className="writing-field">
                  <span>自定义模板</span>
                  <textarea
                    rows={3}
                    value={refTemplate}
                    onChange={(event) => setRefTemplate(event.target.value)}
                    placeholder="{authors}. {title}[J]. {journal}, {year}, {volume}({issue}): {pages}."
                  />
                  <em className="sum-muted">
                    可用：{'{authors} {authors_short} {title} {journal} {year} {volume} {issue} {pages} {doi} {arxiv} {url} {cite_key}'}
                  </em>
                </label>
              )}

              <div className="writing-ref-paper-head">
                <strong>选择文献（{refPaperIds.size}）</strong>
                <input
                  value={refQuery}
                  onChange={(event) => setRefQuery(event.target.value)}
                  placeholder="按标题过滤…"
                />
                <button className="link" onClick={selectAllRefPapers}>
                  全选
                </button>
                <button className="link" onClick={clearRefPapers}>
                  清空
                </button>
              </div>
              <ul className="writing-paper-list writing-ref-paper-list">
                {filteredRefPapers.map((paper) => (
                  <li key={paper.id}>
                    <label className={refPaperIds.has(paper.id) ? 'on' : ''}>
                      <input
                        type="checkbox"
                        checked={refPaperIds.has(paper.id)}
                        onChange={() => toggleRefPaper(paper.id)}
                      />
                      <span>{paper.title}</span>
                      <em>
                        {paper.year ?? '—'} · {paper.venue ?? '无刊名'} ·{' '}
                        {paper.volume ? `${paper.volume}(${paper.issue ?? '—'})` : '无卷期'}
                      </em>
                    </label>
                  </li>
                ))}
              </ul>

              <details className="writing-biblio">
                <summary>补全卷期页（生成更完整的条目）</summary>
                <label className="writing-field">
                  <span>文献</span>
                  <select
                    value={biblioPaperId}
                    onChange={(event) => {
                      setBiblioPaperId(event.target.value)
                      resetMeta()
                    }}
                  >
                    <option value="">选一篇…</option>
                    {papers.map((paper) => (
                      <option key={paper.id} value={paper.id}>
                        {paper.title}
                      </option>
                    ))}
                  </select>
                </label>
                <div className="writing-row writing-row-3">
                  <label className="writing-field">
                    <span>卷</span>
                    <input value={biblioVolume} onChange={(event) => setBiblioVolume(event.target.value)} />
                  </label>
                  <label className="writing-field">
                    <span>期</span>
                    <input value={biblioIssue} onChange={(event) => setBiblioIssue(event.target.value)} />
                  </label>
                  <label className="writing-field">
                    <span>页码</span>
                    <input value={biblioPages} onChange={(event) => setBiblioPages(event.target.value)} />
                  </label>
                </div>
                <button
                  className="ghost small"
                  disabled={!biblioPaperId || biblioBusy}
                  onClick={() => void saveBiblio()}
                >
                  {biblioBusy ? '保存中…' : '保存著录字段'}
                </button>

                <div className="writing-meta">
                  <div className="writing-meta-head">
                    <button
                      className="ghost small"
                      disabled={!biblioPaperId || metaBusy !== null}
                      onClick={() => void runMetadataLookup('auto')}
                      title="按文献已有 DOI / arXiv ID 查 Crossref / arXiv"
                    >
                      {metaBusy === 'lookup' ? '查询中…' : '在线补全（Crossref / arXiv）'}
                    </button>
                    <input
                      value={metaQuery}
                      onChange={(event) => setMetaQuery(event.target.value)}
                      placeholder="按题名搜索（可选）"
                    />
                    <button
                      className="ghost small"
                      disabled={!biblioPaperId || metaBusy !== null}
                      onClick={() => void runMetadataLookup('search', { query: metaQuery.trim() })}
                      title="Crossref 题名搜索；搜到候选后再点「用这条」看字段 diff"
                    >
                      {metaBusy === 'search' ? '搜索中…' : '搜索候选'}
                    </button>
                  </div>
                  {metaError && <p className="writing-ref-warn">{metaError}</p>}
                  {metaResult && metaResult.fields.length > 0 && (
                    <div className="writing-meta-result">
                      <p className="sum-muted">
                        来源：{metaResult.source} · {metaResult.identifier}；默认只勾选缺失字段
                        {metaResult.warnings.length > 0 ? `（${metaResult.warnings.join('；')}）` : ''}
                      </p>
                      <ul className="writing-meta-list">
                        {metaResult.fields.map((row) => (
                          <li key={row.field} className={row.conflict ? 'conflict' : ''}>
                            <label className="writing-check-inline">
                              <input
                                type="checkbox"
                                checked={metaSelected.has(row.field)}
                                onChange={() => toggleMetaField(row.field)}
                              />
                              <strong>{row.label}</strong>
                            </label>
                            <span className="writing-meta-values">
                              {row.current_text || '（空）'} → {row.suggested_text}
                            </span>
                            {row.conflict && <em className="writing-ref-warn">已有值</em>}
                          </li>
                        ))}
                      </ul>
                      {metaSelectedHasConflict(metaResult, metaSelected) && (
                        <label className="writing-check-inline">
                          <input
                            type="checkbox"
                            checked={metaOverwrite}
                            onChange={(event) => setMetaOverwrite(event.target.checked)}
                          />
                          覆盖已有值
                        </label>
                      )}
                      <button
                        className="primary small"
                        disabled={metaBusy !== null || metaSelected.size === 0}
                        onClick={() => void applyMeta()}
                      >
                        {metaBusy === 'apply' ? '写入中…' : `应用选中字段（${metaSelected.size}）`}
                      </button>
                    </div>
                  )}
                  {metaResult && metaResult.candidates.length > 0 && (
                    <ul className="writing-meta-candidates">
                      {metaResult.candidates.map((candidate, index) => (
                        <li key={`${candidate.doi ?? candidate.title}-${index}`}>
                          <div>
                            <strong>{candidate.title}</strong>
                          </div>
                          <div className="sum-muted">
                            {candidate.year ?? '—'} · {candidate.venue ?? '无刊名'} ·{' '}
                            {candidate.authors || '无作者'}
                            {candidate.doi ? ` · ${candidate.doi}` : ''}
                          </div>
                          {candidate.doi && (
                            <button
                              className="ghost small"
                              disabled={metaBusy !== null}
                              onClick={() => void runMetadataLookup('doi', { doi: candidate.doi ?? '' })}
                            >
                              用这条看 diff
                            </button>
                          )}
                        </li>
                      ))}
                    </ul>
                  )}
                  {metaResult &&
                    metaResult.fields.length === 0 &&
                    metaResult.candidates.length === 0 && (
                      <p className="sum-muted">
                        没有可补全的字段（库里已有值，或接口没有返回可用字段）。
                        {metaResult.warnings.length > 0
                          ? ` ${metaResult.warnings.join('；')}`
                          : ''}
                      </p>
                    )}
                </div>
              </details>

              <div className="writing-ref-actions">
                <label className="writing-check-inline">
                  <input
                    type="checkbox"
                    checked={refNumbered}
                    disabled={refEngine === 'csl'}
                    onChange={(event) => setRefNumbered(event.target.checked)}
                  />
                  {refEngine === 'csl' ? '序号由 CSL 样式决定' : '带序号（[1]）'}
                </label>
                <button
                  className="primary small"
                  disabled={refBusy || refPaperIds.size === 0}
                  onClick={() => void runReferences()}
                >
                  {refBusy ? '生成中…' : '生成 entries'}
                </button>
                <button
                  className="ghost small"
                  disabled={cslJsonBusy || refPaperIds.size === 0}
                  onClick={() => void downloadCslJson()}
                  title="pandoc / Zotero 通用的 CSL JSON；后续 docx 导出也用它"
                >
                  {cslJsonBusy ? '导出中…' : '导出 CSL JSON'}
                </button>
                {refResult && (
                  <>
                    <button
                      className="ghost small"
                      onClick={() =>
                        void copyText(refResult.content).then(() => onToast('已复制全部条目'))
                      }
                    >
                      复制全部
                    </button>
                    <button
                      className="ghost small"
                      onClick={() =>
                        downloadText(
                          refResult.style === 'bibtex'
                            ? 'references.bib'
                            : refResult.style === 'ris'
                              ? 'references.ris'
                              : 'references.txt',
                          refResult.content,
                        )
                      }
                    >
                      下载
                    </button>
                  </>
                )}
              </div>
            </section>

            <section className="writing-ref-result">
              {!refResult && (
                <p className="sum-muted">
                  选好格式和文献后点「生成 entries」；只按库里的著录信息生成，不落库，换格式可重算。
                </p>
              )}
              {refResult && (
                <>
                  <div className="writing-export-head">
                    <strong>{refResult.label}</strong>
                    <span className="muted">{refResult.paper_count} 条</span>
                    {refResult.engine === 'csl' && <span className="muted">CSL 引擎</span>}
                    <span className="spacer" />
                    <span className="muted">缺字段会在条目下提示</span>
                  </div>
                  {refResult.note && <p className="sum-muted">{refResult.note}</p>}
                  <ul className="writing-ref-list">
                    {refResult.items.map((item, index) => (
                      <li className="writing-ref-item" key={`${item.paper_id}-${index}`}>
                        <div className="writing-ref-item-head">
                          <span className="writing-cite">
                            {item.cite_key ??
                              (item.paper_id ? item.paper_id.slice(0, 8) : '未匹配')}
                          </span>
                          <span className="spacer" />
                          <button
                            className="link"
                            onClick={() =>
                              void copyText(item.entry).then(() => onToast('已复制这一条'))
                            }
                          >
                            复制
                          </button>
                        </div>
                        <pre className="writing-ref-entry">{item.entry}</pre>
                        {item.warnings.length > 0 && (
                          <p className="writing-ref-warn">提示：{item.warnings.join('、')}</p>
                        )}
                      </li>
                    ))}
                  </ul>
                  <details className="writing-ref-raw">
                    <summary>整段文本（可直接粘进 Word / LaTeX）</summary>
                    <pre className="writing-code">{refResult.content}</pre>
                  </details>
                </>
              )}
            </section>
          </div>
        )}
      </aside>
    </div>
  )
}
