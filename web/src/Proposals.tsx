// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * M3 待审提案队列（AI 纠错）。
 *
 * 抽屉里三个页签：
 * - 待审：pending 建议，before/after 对比 + 置信度 + 采纳/拒绝（可多选批量）；
 * - 已自动接受：auto_accepted=true，可一键回滚（按 before 快照恢复图字段）；
 * - 全部历史：accepted / rejected / rolled_back / no_change / superseded。
 *
 * 每条建议都能展开「审计」：GET /api/audit?target_type=figure_proposal&target_id=…
 * 所有写操作（采纳/拒绝/回滚）都由后端写审计，前端只负责展示。
 *
 * 队列里的动作只刷新本抽屉与图数据（通过 onChanged 让外层静默刷新），不整页重载。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  autoAcceptProposals,
  decideProposal,
  decideProposalBatch,
  enqueueFigureReviewTasks,
  fetchAuditLog,
  fetchProposalStats,
  listAgentTasks,
  listProposals,
  recoverExpiredTasks,
  retryAgentTask,
  rollbackProposal,
} from './api'
import { formatTime } from './format'
import type { AgentTask, AuditEntry, FigureProposal, ProposalStats } from './types'

interface Props {
  open: boolean
  onClose: () => void
  onToast: (message: string) => void
  /** 采纳/拒绝/回滚/自动接受后通知外层刷新图网格与详情（静默刷新，不整页重载） */
  onChanged: () => void
  /** 模型是否可用：不可用时禁用「AI 复核待确认图」 */
  aiReady: boolean
}

type Tab = 'pending' | 'auto_accepted' | 'history' | 'tasks'

const TAB_LABEL: Record<Tab, string> = {
  pending: '待审',
  auto_accepted: '已自动接受',
  history: '全部历史',
  tasks: '任务',
}

const TASK_STATUS_LABEL: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  done: '已完成',
  failed: '失败',
  canceled: '已取消',
}

const TASK_KIND_LABEL: Record<string, string> = {
  figure_review: '图纠错',
  figure_bbox_review: '框复核',
  auto_accept_sweep: '自动接受扫描',
}

const STATUS_LABEL: Record<string, string> = {
  pending: '待审',
  accepted: '已采纳',
  rejected: '已拒绝',
  rolled_back: '已回滚',
  no_change: 'AI 检查通过',
  superseded: '已被取代',
}

const CROP_LABEL: Record<string, string> = {
  cut_off: '图被切掉一部分',
  too_large: '框里混进了正文',
  too_small: '框太小 / 只有空白',
  not_figure: '这块不是图',
  none: '正常',
  unknown: '未判断',
}

const FIELD_LABEL: Record<string, string> = {
  label_raw: '图号',
  label_norm: '图号',
  caption: '图注',
  crop_issue: '裁框标记',
}

const AUDIT_ACTION_LABEL: Record<string, string> = {
  'proposal.accept': '人工采纳',
  'proposal.auto_accept': '自动接受',
  'proposal.reject': '人工拒绝',
  'proposal.rollback': '回滚',
}

const AUDIT_FIELD_LABEL: Record<string, string> = {
  label_raw: '图号',
  label_norm: '图号',
  label_num: '图号数字',
  sublabel: '子图号',
  label_source: '图号来源',
  label_confidence: '图号置信度',
  caption: '图注',
  caption_source: '图注来源',
  review_status: '定位状态',
  review_note: '定位备注',
  reviewed_by: '复核人',
  reviewed_at: '复核时间',
  status: '状态',
  reason: '原因',
}

/** 审计里按这个顺序展示字段（只展示 before/after 有差异的）。 */
const AUDIT_FIELD_ORDER = [
  'label_raw',
  'label_norm',
  'label_num',
  'sublabel',
  'label_source',
  'label_confidence',
  'caption',
  'caption_source',
  'review_status',
  'review_note',
  'reviewed_by',
  'reviewed_at',
  'status',
  'reason',
]

const EMPTY_TEXT = '（无）'

/** null / 空串统一显示成「（无）」，布尔显示成是/否。 */
function display(value: unknown): string {
  if (value === null || value === undefined) return EMPTY_TEXT
  if (typeof value === 'boolean') return value ? '是' : '否'
  const text = String(value).replace(/\s+/g, ' ').trim()
  return text || EMPTY_TEXT
}

/** 比较用的归一化：null / 空串视为同一个「无」。 */
function norm(value: unknown): string {
  if (value === null || value === undefined) return ''
  return String(value)
}

function shortText(value: unknown, limit: number): string {
  const text = display(value)
  return text.length > limit ? `${text.slice(0, limit)}…` : text
}

function fieldLabel(key: string): string {
  return FIELD_LABEL[key] ?? AUDIT_FIELD_LABEL[key] ?? key
}

type DiffRow = { field: string; oldValue: string; newValue: string; mono?: boolean }

/**
 * 图号/图注的「旧值 → 新值」。
 *
 * - pending：旧值取模型看到的当前值（current_label/current_caption，缺省用图当前值），
 *   新值取建议值；
 * - accepted：旧值取采纳前快照 before，新值取图当前值（= 采纳后的正式字段）；
 * - rolled_back：图已恢复成 before，差异通常为空，卡片上另有「已回滚」说明。
 */
function buildDiffs(item: FigureProposal): DiffRow[] {
  const proposed = item.proposed
  const fig = item.figure
  const before = item.before
  const adopted = item.status === 'accepted' || item.status === 'rolled_back'
  const rows: DiffRow[] = []

  const oldLabel = adopted && before ? before.label_raw : (proposed.current_label ?? fig?.label_raw ?? null)
  // suggested_label=null 且 label_ok=true 表示「图号不用改」，不能显示成「→（无）」；
  // 只有明确给了新图号，或 label_ok=false（明确要求无编号）才是一行 diff。
  const suggestedLabel = proposed.suggested_label
  const explicitNoLabel = proposed.label_ok === false
  const newLabel = adopted
    ? (fig?.label_raw ?? null)
    : typeof suggestedLabel === 'string' && suggestedLabel.trim()
      ? suggestedLabel
      : explicitNoLabel
        ? null
        : (fig?.label_raw ?? null)
  if (norm(oldLabel) !== norm(newLabel)) {
    rows.push({ field: '图号', oldValue: display(oldLabel), newValue: display(newLabel), mono: true })
  }

  const oldCaption =
    adopted && before ? before.caption : (proposed.current_caption ?? fig?.caption ?? null)
  const newCaption = adopted
    ? (fig?.caption ?? null)
    : proposed.cleaned_caption
      ? proposed.cleaned_caption
      : (fig?.caption ?? null)
  if (norm(oldCaption) !== norm(newCaption)) {
    rows.push({
      field: '图注',
      oldValue: shortText(oldCaption, 160),
      newValue: shortText(newCaption, 160),
    })
  }

  const crop = proposed.crop_issue
  if (crop && CROP_LABEL[crop] && (item.status === 'pending' || adopted)) {
    rows.push({ field: '裁框', oldValue: '—', newValue: CROP_LABEL[crop] })
  }

  // M3c 框复核：显示「当前框 → 建议框」+ IoU + caption 锚点，让人审有依据。
  if (item.kind === 'figure_bbox') {
    const fmtBox = (box: unknown): string => {
      if (!Array.isArray(box) || box.length !== 4) return EMPTY_TEXT
      return `[${box.map((v) => Math.round(Number(v))).join(', ')}]`
    }
    const oldBox = proposed.current_bbox ?? before?.bbox_override_json ?? null
    const newBox = adopted
      ? (fig?.bbox_override ?? proposed.bbox_points ?? null)
      : (proposed.bbox_points ?? null)
    rows.push({
      field: '定位框（pt）',
      oldValue: fmtBox(oldBox),
      newValue: fmtBox(newBox),
      mono: true,
    })
    if (typeof proposed.iou === 'number') {
      rows.push({ field: '与现框 IoU', oldValue: '—', newValue: proposed.iou.toFixed(2) })
    }
    if (proposed.caption_anchor) {
      rows.push({
        field: 'caption 锚点',
        oldValue: '—',
        newValue: proposed.caption_anchor.ok ? '一致' : '不一致（自动接受会拦）',
      })
    }
  }
  return rows
}

/** 审计一条记录里的 before→after 差异（只列已知字段，且只列变化项）。 */
function auditRows(entry: AuditEntry): DiffRow[] {
  const before = entry.before ?? {}
  const after = entry.after ?? {}
  const keys = Array.from(new Set([...Object.keys(before), ...Object.keys(after)])).filter(
    (key) => AUDIT_FIELD_ORDER.includes(key),
  )
  const rows: DiffRow[] = []
  for (const key of keys) {
    if (norm(before[key]) === norm(after[key])) continue
    rows.push({
      field: AUDIT_FIELD_LABEL[key] ?? key,
      oldValue: shortText(before[key], 100),
      newValue: shortText(after[key], 100),
    })
  }
  return rows
}

type NoteKind = 'ok' | 'warn' | 'bad'
type Note = { kind: NoteKind; text: string; details?: string[] }

export default function Proposals({ open, onClose, onToast, onChanged, aiReady }: Props) {
  const [tab, setTab] = useState<Tab>('pending')
  const [items, setItems] = useState<FigureProposal[] | null>(null)
  const [stats, setStats] = useState<ProposalStats | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // 单条操作进行中（proposal_id）/ 批量操作进行中
  const [busyId, setBusyId] = useState<string | null>(null)
  const [batchBusy, setBatchBusy] = useState(false)
  const [scanning, setScanning] = useState(false)
  const [enqueuing, setEnqueuing] = useState(false)
  // 后台还在排队/执行的 AI 复核任务数（「AI 复核进行中 N 条」）
  const [runningTasks, setRunningTasks] = useState(0)
  // 「任务」页签：整张任务列表（不只计数）
  const [taskRows, setTaskRows] = useState<AgentTask[] | null>(null)
  const [taskError, setTaskError] = useState<string | null>(null)
  const [taskBusy, setTaskBusy] = useState<string | null>(null)
  // 多选（只在「待审」页签生效）
  const [selected, setSelected] = useState<Set<string>>(new Set())
  // 最近一次操作的结果提示
  const [note, setNote] = useState<Note | null>(null)
  // 审计：展开的 proposal / 已拉到的审计记录（null = 载入中）/ 单条错误
  const [auditOpen, setAuditOpen] = useState<Set<string>>(new Set())
  const [auditData, setAuditData] = useState<Record<string, AuditEntry[] | null>>({})
  const [auditErrors, setAuditErrors] = useState<Record<string, string | undefined>>({})

  const loadList = useCallback(async (target: Tab, silent = false) => {
    if (target === 'tasks') return
    if (!silent) setLoading(true)
    setError(null)
    try {
      if (target === 'pending') {
        setItems(await listProposals({ status: 'pending', limit: 200 }))
      } else if (target === 'auto_accepted') {
        // 后端没有 auto_accepted 过滤参数：先取 accepted，再按自动接受标记过滤
        const rows = await listProposals({ status: 'accepted', limit: 200 })
        setItems(rows.filter((row) => row.auto_accepted))
      } else {
        setItems(await listProposals({ status: 'all', limit: 200 }))
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      if (!silent) setLoading(false)
    }
  }, [])

  const loadStats = useCallback(async () => {
    try {
      setStats(await fetchProposalStats())
    } catch {
      setStats(null)
    }
  }, [])

  const loadTasks = useCallback(async () => {
    try {
      const rows = await listAgentTasks({ kind: 'figure_review', limit: 200 })
      setRunningTasks(rows.filter((row) => row.status === 'queued' || row.status === 'running').length)
    } catch {
      setRunningTasks(0)
    }
  }, [])

  /** 「任务」页签：拿全部 kind 的任务（图纠错 / 框复核 / 自动接受扫描）。 */
  const loadTaskRows = useCallback(async () => {
    setTaskError(null)
    try {
      setTaskRows(await listAgentTasks({ limit: 200 }))
    } catch (err) {
      setTaskError(err instanceof Error ? err.message : String(err))
    }
  }, [])

  // 打开时复位到「待审」并拉数据
  useEffect(() => {
    if (!open) return
    setTab('pending')
    setSelected(new Set())
    setNote(null)
    setAuditOpen(new Set())
    setAuditData({})
    setAuditErrors({})
    void loadList('pending')
    void loadStats()
    void loadTasks()
    void loadTaskRows()
  }, [open, loadList, loadStats, loadTasks, loadTaskRows])

  // 队列开着时轻量轮询任务数（「AI 复核进行中 N 条」）；复核跑完自动刷新列表
  const prevRunning = useRef(0)
  useEffect(() => {
    if (!open) return
    const handle = window.setInterval(() => void loadTasks(), 8000)
    return () => window.clearInterval(handle)
  }, [open, loadTasks])

  useEffect(() => {
    if (!open) {
      prevRunning.current = runningTasks
      return
    }
    // 从「有任务在跑」变成「跑完了」：把当前页签和统计刷一遍（新建议可能刚落库）
    if (prevRunning.current > 0 && runningTasks === 0) {
      void loadList(tab, true)
      void loadStats()
    }
    prevRunning.current = runningTasks
  }, [runningTasks, open, tab, loadList, loadStats])

  // Esc 关闭 + 锁背景滚动（与其它浮层一致）
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    document.body.classList.add('overlay-open')
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.classList.remove('overlay-open')
    }
  }, [open, onClose])

  const switchTab = (next: Tab) => {
    if (next === tab) return
    setTab(next)
    setSelected(new Set())
    setNote(null)
    setAuditOpen(new Set())
    if (next === 'tasks') void loadTaskRows()
    else void loadList(next)
  }

  const refreshAll = () => {
    void loadList(tab)
    void loadStats()
    void loadTasks()
    if (tab === 'tasks') void loadTaskRows()
  }

  const handleRetryTask = async (taskId: string) => {
    setTaskBusy(taskId)
    setTaskError(null)
    try {
      await retryAgentTask(taskId)
      setNote({ kind: 'ok', text: '任务已重新排队' })
      await loadTaskRows()
      await loadTasks()
    } catch (err) {
      const text = err instanceof Error ? `重试失败：${err.message}` : '重试失败'
      setTaskError(text)
      setNote({ kind: 'bad', text })
    } finally {
      setTaskBusy(null)
    }
  }

  const handleRecover = async () => {
    setTaskBusy('recover')
    setTaskError(null)
    try {
      const result = await recoverExpiredTasks()
      setNote({ kind: 'ok', text: `已回收 ${result.recovered} 条过期租约任务` })
      await loadTaskRows()
      await loadTasks()
    } catch (err) {
      setTaskError(err instanceof Error ? err.message : String(err))
    } finally {
      setTaskBusy(null)
    }
  }

  /** 某条建议状态变了：清掉它的审计缓存，下次展开重新拉。 */
  const invalidateAudit = (proposalId: string) => {
    setAuditData((prev) => {
      if (!(proposalId in prev)) return prev
      const next = { ...prev }
      delete next[proposalId]
      return next
    })
    setAuditErrors((prev) => {
      if (!(proposalId in prev)) return prev
      const next = { ...prev }
      delete next[proposalId]
      return next
    })
  }

  /** 处理完一批动作后：原地刷新当前页签 + 统计，并通知外层刷新图数据。 */
  const afterChange = async (quiet = true) => {
    await Promise.all([loadList(tab, quiet), loadStats()])
    onChanged()
  }

  const toggleSelected = (proposalId: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(proposalId)) next.delete(proposalId)
      else next.add(proposalId)
      return next
    })
  }

  const toggleSelectAll = () => {
    if (!items || items.length === 0) return
    setSelected((prev) =>
      prev.size === items.length ? new Set() : new Set(items.map((item) => item.proposal_id)),
    )
  }

  const decide = async (item: FigureProposal, action: 'accept' | 'reject') => {
    if (busyId || batchBusy) return
    setBusyId(item.proposal_id)
    setNote(null)
    try {
      const result = await decideProposal(
        item.proposal_id,
        action,
        action === 'reject' ? '人工拒绝' : undefined,
      )
      invalidateAudit(item.proposal_id)
      await afterChange()
      if (action === 'accept') {
        const fields = Object.keys(result.applied).map((key) => fieldLabel(key))
        const text = fields.length > 0 ? `已采纳：更新${fields.join('、')}` : '已采纳（无需改动图字段）'
        setNote({ kind: 'ok', text })
        onToast(text)
      } else {
        setNote({ kind: 'ok', text: '已拒绝这条建议（保留记录，可在「全部历史」查看）' })
        onToast('已拒绝这条建议')
      }
    } catch (err) {
      const text = err instanceof Error ? `处理失败：${err.message}` : '处理失败'
      setNote({ kind: 'bad', text })
      onToast(text)
    } finally {
      setBusyId(null)
    }
  }

  const runBatch = async (action: 'accept' | 'reject') => {
    if (batchBusy || busyId) return
    const ids = [...selected]
    if (ids.length === 0) return
    const actionLabel = action === 'accept' ? '采纳' : '拒绝'
    if (!window.confirm(`批量${actionLabel} ${ids.length} 条建议？逐条失败不影响其它条。`)) return
    setBatchBusy(true)
    setNote(null)
    try {
      const result = await decideProposalBatch(ids, action)
      const done = action === 'accept' ? result.accepted : result.rejected
      const failed = result.failed ?? []
      const details = failed
        .slice(0, 5)
        .map((row) => `${row.proposal_id.slice(0, 8)}…：${row.error}`)
      const text = `批量${actionLabel} ${result.requested} 条：成功 ${done} 条${
        failed.length > 0 ? `，失败 ${failed.length} 条` : ''
      }`
      setNote({ kind: failed.length > 0 ? 'warn' : 'ok', text, details })
      onToast(text)
      ids.forEach(invalidateAudit)
      setSelected(new Set())
      await afterChange()
    } catch (err) {
      const text = err instanceof Error ? `批量处理失败：${err.message}` : '批量处理失败'
      setNote({ kind: 'bad', text })
      onToast(text)
    } finally {
      setBatchBusy(false)
    }
  }

  const rollback = async (item: FigureProposal) => {
    if (busyId) return
    const oldLabel = display(item.before?.label_raw)
    const oldCaption = shortText(item.before?.caption, 60)
    if (
      !window.confirm(
        `回滚这条已采纳建议？图号会恢复为「${oldLabel}」，图注恢复为「${oldCaption}」。`,
      )
    ) {
      return
    }
    setBusyId(item.proposal_id)
    setNote(null)
    try {
      const result = await rollbackProposal(item.proposal_id)
      invalidateAudit(item.proposal_id)
      await afterChange()
      const text = `已回滚：图号恢复为「${display(result.figure?.label_raw)}」`
      setNote({ kind: 'ok', text })
      onToast('已回滚到采纳前状态')
    } catch (err) {
      const text = err instanceof Error ? `回滚失败：${err.message}` : '回滚失败'
      setNote({ kind: 'bad', text })
      onToast(text)
    } finally {
      setBusyId(null)
    }
  }

  const runScan = async () => {
    if (scanning) return
    setScanning(true)
    setNote(null)
    try {
      const result = await autoAcceptProposals({ limit: 50 })
      const details = result.skipped_items
        .slice(0, 4)
        .map((row) => `跳过 ${row.proposal_id.slice(0, 8)}…：${row.reason}`)
      const text = `自动接受扫描 ${result.scanned} 条：接受 ${result.accepted} 条，跳过 ${result.skipped} 条`
      setNote({ kind: result.accepted > 0 ? 'ok' : 'warn', text, details })
      onToast(text)
      setSelected(new Set())
      if (result.accepted > 0) await afterChange()
      else await Promise.all([loadList(tab, true), loadStats()])
    } catch (err) {
      const text = err instanceof Error ? `自动接受扫描失败：${err.message}` : '自动接受扫描失败'
      setNote({ kind: 'bad', text })
      onToast(text)
    } finally {
      setScanning(false)
    }
  }

  const runEnqueue = async () => {
    if (enqueuing) return
    if (!aiReady) {
      onToast('先配置模型 API 才能排 AI 复核任务')
      return
    }
    setEnqueuing(true)
    setNote(null)
    try {
      const result = await enqueueFigureReviewTasks({ labelState: 'unreviewed_all', limit: 20 })
      const details = [
        result.created > 0
          ? 'worker 会在后台逐张复核，完成后的建议出现在「待审」页签（本面板会显示复核进行中的数量）'
          : '当前没有可排的待确认图（或都已在队列里）',
      ]
      const text = `AI 复核任务：新建 ${result.created} 条，跳过 ${result.skipped} 条`
      setNote({ kind: result.created > 0 ? 'ok' : 'warn', text, details })
      onToast(text)
      void loadTasks()
    } catch (err) {
      const text = err instanceof Error ? `排任务失败：${err.message}` : '排任务失败'
      setNote({ kind: 'bad', text })
      onToast(text)
    } finally {
      setEnqueuing(false)
    }
  }

  const toggleAudit = (item: FigureProposal) => {
    const proposalId = item.proposal_id
    if (auditOpen.has(proposalId)) {
      setAuditOpen((prev) => {
        const next = new Set(prev)
        next.delete(proposalId)
        return next
      })
      return
    }
    setAuditOpen((prev) => new Set(prev).add(proposalId))
    if (Object.prototype.hasOwnProperty.call(auditData, proposalId)) return
    setAuditData((prev) => ({ ...prev, [proposalId]: null }))
    setAuditErrors((prev) => {
      const next = { ...prev }
      delete next[proposalId]
      return next
    })
    void (async () => {
      try {
        const rows = await fetchAuditLog({
          targetType: 'figure_proposal',
          targetId: proposalId,
          limit: 50,
        })
        setAuditData((prev) => ({ ...prev, [proposalId]: rows }))
      } catch (err) {
        setAuditData((prev) => ({ ...prev, [proposalId]: [] }))
        setAuditErrors((prev) => ({
          ...prev,
          [proposalId]: err instanceof Error ? err.message : String(err),
        }))
      }
    })()
  }

  if (!open) return null

  const allSelected =
    !!items && items.length > 0 && selected.size === items.length && tab === 'pending'
  const currentCount =
    tab === 'pending' ? (stats?.pending ?? items?.length ?? 0) : (items?.length ?? 0)

  return (
    <div className="proposal-backdrop" role="presentation" onPointerDown={onClose}>
      <aside
        className="proposal-panel"
        role="dialog"
        aria-modal="true"
        aria-label="AI 纠错队列"
        onPointerDown={(e) => e.stopPropagation()}
      >
        <header className="proposal-head">
          <div>
            <strong>AI 纠错队列</strong>
            <span className="muted">
              {' '}
              · {stats
                ? `待审 ${stats.pending} · 已接受 ${stats.accepted} · 已拒绝 ${stats.rejected} · 已回滚 ${stats.rolled_back}`
                : `共 ${currentCount} 条`}
            </span>
          </div>
          <div className="proposal-head-actions">
            <button className="close" onClick={onClose} aria-label="关闭">
              ✕
            </button>
          </div>
        </header>

        <div className="proposal-tabs" role="tablist" aria-label="AI 建议状态">
          {(['pending', 'auto_accepted', 'history', 'tasks'] as Tab[]).map((name) => (
            <button
              key={name}
              role="tab"
              aria-selected={tab === name}
              tabIndex={tab === name ? 0 : -1}
              className={`proposal-tab ${tab === name ? 'on' : ''}`}
              onClick={() => switchTab(name)}
            >
              {TAB_LABEL[name]}
              {name === 'pending' && stats && stats.pending > 0 && (
                <span className="count">{stats.pending}</span>
              )}
              {name === 'tasks' && runningTasks > 0 && (
                <span className="count">{runningTasks}</span>
              )}
            </button>
          ))}
        </div>

        <div className="proposal-toolbar">
          {tab === 'tasks' ? (
            <button
              className="ghost small"
              disabled={taskBusy !== null}
              onClick={() => void handleRecover()}
              title="把租约过期仍在 running 的任务回收：还有次数回 queued，否则 failed"
            >
              {taskBusy === 'recover' ? '回收中…' : '回收过期租约'}
            </button>
          ) : (
            <>
              <button
                className="ghost small"
                disabled={scanning || batchBusy}
                onClick={() => void runScan()}
                title="扫一遍待审建议：满足自动接受条件（白名单 + 置信阈值 + 证据 + 未人工修改）的直接写正式字段"
              >
                {scanning ? '扫描中…' : '自动接受扫描'}
              </button>
              <button
                className="ghost small"
                disabled={!aiReady || enqueuing}
                onClick={() => void runEnqueue()}
                title={aiReady ? '把最多 20 张待确认图排成 AI 复核任务，worker 后台逐张跑' : '先配置模型 API'}
              >
                {enqueuing ? '排队中…' : 'AI 复核待确认图'}
              </button>
            </>
          )}
          {runningTasks > 0 && (
            <span className="proposal-running">AI 复核进行中 {runningTasks} 条</span>
          )}
          <span className="spacer" />
          <button className="ghost small" disabled={loading} onClick={refreshAll}>
            刷新
          </button>
        </div>

        {note && (
          <div className={`proposal-note ${note.kind}`} role="status">
            <span>{note.text}</span>
            {note.details && note.details.length > 0 && (
              <ul>
                {note.details.map((detail, index) => (
                  <li key={index}>{detail}</li>
                ))}
              </ul>
            )}
          </div>
        )}

        {loading && items === null && <p className="sum-muted proposal-loading">载入建议…</p>}
        {error && (
          <div className="banner error proposal-error">
            建议列表加载失败：{error}
            <button className="link" onClick={refreshAll}>
              重试
            </button>
          </div>
        )}

        {tab === 'pending' && items && items.length > 0 && (
          <div className="proposal-selectbar">
            <label className="proposal-check">
              <input type="checkbox" checked={allSelected} onChange={toggleSelectAll} />
              全选
            </label>
            <span className="muted">
              已选 {selected.size} / {items.length} 条
            </span>
            <span className="spacer" />
            <button
              className="primary small"
              disabled={batchBusy || busyId !== null || selected.size === 0}
              onClick={() => void runBatch('accept')}
            >
              {batchBusy ? '批量处理中…' : `批量采纳（${selected.size}）`}
            </button>
            <button
              className="ghost small"
              disabled={batchBusy || busyId !== null || selected.size === 0}
              onClick={() => void runBatch('reject')}
            >
              批量拒绝
            </button>
          </div>
        )}

        {tab !== 'tasks' && !loading && !error && items && items.length === 0 && (
          <div className="empty proposal-empty">
            {tab === 'pending' ? (
              <>
                <h3>没有待审建议</h3>
                <p>
                  在图的详情里点「AI 检查这张图」，或点上面的「AI 复核待确认图」把一批图排给后台
                  worker；模型只提议，采纳才写正式字段。
                </p>
              </>
            ) : tab === 'auto_accepted' ? (
              <>
                <h3>还没有自动接受的建议</h3>
                <p>模型建议满足白名单 + 置信阈值 + 证据条件时会被自动接受，可在这里一键回滚。</p>
              </>
            ) : (
              <>
                <h3>还没有历史建议</h3>
                <p>采纳、拒绝、回滚过的建议都会留在这里，可展开「审计」查看每次写操作的 before/after。</p>
              </>
            )}
          </div>
        )}

        {tab !== 'tasks' && items && items.length > 0 && (
          <ul className="proposal-list">
            {items.map((item) => {
              const diffs = buildDiffs(item)
              const showDiffs =
                item.status === 'pending' ||
                item.status === 'accepted' ||
                item.status === 'rolled_back'
              const canDecide = item.status === 'pending'
              const canRollback = item.status === 'accepted'
              const isBusy = busyId === item.proposal_id
              const auditEntries = auditData[item.proposal_id]
              const auditError = auditErrors[item.proposal_id]
              return (
                <li
                  className={`proposal-card ${selected.has(item.proposal_id) ? 'selected' : ''}`}
                  key={item.proposal_id}
                >
                  <div className="proposal-card-top">
                    {tab === 'pending' && (
                      <label className="proposal-check proposal-card-check">
                        <input
                          type="checkbox"
                          checked={selected.has(item.proposal_id)}
                          onChange={() => toggleSelected(item.proposal_id)}
                          aria-label="选择这条建议"
                        />
                      </label>
                    )}
                    <div className="proposal-card-main">
                      <div className="proposal-card-head">
                        <span className="proposal-fig-label">
                          {item.figure?.label_raw ?? '（无编号）'}
                          <span className="muted"> · p.{item.figure?.page ?? '?'}</span>
                        </span>
                        <span className={`proposal-mode ${item.mode === 'vision' ? 'vision' : 'text'}`}>
                          {item.mode === 'vision' ? '视觉' : '文字'}
                        </span>
                        <span className="proposal-tag">
                          {item.kind === 'figure_bbox' ? '框复核' : '图号/图注'}
                        </span>
                        <span className={`proposal-status ${item.status}`}>
                          {STATUS_LABEL[item.status] ?? item.status}
                        </span>
                        <span
                          className={`proposal-conf ${item.confidence >= 0.9 ? 'high' : 'mid'}`}
                          title="模型自评置信度（自动接受阈值参考）"
                        >
                          {Math.round(item.confidence * 100)}%
                        </span>
                      </div>

                      <div className="proposal-paper">{item.paper?.title ?? '（未知文献）'}</div>
                      <div className="proposal-meta">
                        <span>创建 {formatTime(item.created_at)}</span>
                        {item.decided_at && (
                          <span>
                            · {item.auto_accepted ? '自动接受' : '处理'} {formatTime(item.decided_at)}
                            {item.decided_by ? `（${item.decided_by}）` : ''}
                          </span>
                        )}
                        {item.rolled_back_at && (
                          <span>
                            · 回滚 {formatTime(item.rolled_back_at)}
                            {item.rolled_back_by ? `（${item.rolled_back_by}）` : ''}
                          </span>
                        )}
                        {item.auto_accepted && <span className="proposal-tag">自动接受</span>}
                      </div>

                      {item.figure?.image_url && (
                        <img
                          className="proposal-thumb"
                          src={item.figure.image_url}
                          alt={item.figure.label_raw ?? '建议图'}
                          loading="lazy"
                        />
                      )}

                      {showDiffs && diffs.length > 0 && (
                        <div className="proposal-diffs">
                          {diffs.map((row) => (
                            <div className="proposal-diff" key={row.field}>
                              <span className="proposal-field">{row.field}</span>
                              <span className="proposal-old" title={row.oldValue}>
                                {row.oldValue}
                              </span>
                              <span className="proposal-arrow">→</span>
                              {row.mono ? (
                                <code className="proposal-new">{row.newValue}</code>
                              ) : (
                                <span className="proposal-new-text" title={row.newValue}>
                                  {row.newValue}
                                </span>
                              )}
                            </div>
                          ))}
                        </div>
                      )}

                      {item.status === 'rolled_back' && item.before && (
                        <p className="proposal-rollback-note">
                          已回滚到采纳前：图号「{display(item.before.label_raw)}」，图注「
                          {shortText(item.before.caption, 100)}」
                        </p>
                      )}

                      {(item.proposed.summary || item.proposed.reason) && (
                        <p className="proposal-reason">{item.proposed.summary || item.proposed.reason}</p>
                      )}
                      {item.decision_reason && item.status !== 'pending' && (
                        <p className="proposal-note">人工备注：{item.decision_reason}</p>
                      )}
                      {item.proposed.validation_notes && item.proposed.validation_notes.length > 0 && (
                        <p className="proposal-note">
                          模型建议中被丢弃的部分：{item.proposed.validation_notes.join('；')}
                        </p>
                      )}

                      <div className="proposal-actions">
                        {canDecide && (
                          <>
                            <button
                              className="primary small"
                              disabled={isBusy || batchBusy}
                              onClick={() => void decide(item, 'accept')}
                            >
                              {isBusy ? '处理中…' : '采纳'}
                            </button>
                            <button
                              className="ghost small"
                              disabled={isBusy || batchBusy}
                              onClick={() => void decide(item, 'reject')}
                            >
                              拒绝
                            </button>
                          </>
                        )}
                        {canRollback && (
                          <button
                            className="ghost small danger"
                            disabled={isBusy || batchBusy}
                            onClick={() => void rollback(item)}
                            title="按采纳前快照恢复图号/图注/复核标记"
                          >
                            {isBusy ? '回滚中…' : '回滚'}
                          </button>
                        )}
                        <button className="ghost small" onClick={() => toggleAudit(item)}>
                          {auditOpen.has(item.proposal_id) ? '收起审计' : '审计'}
                        </button>
                      </div>

                      {auditOpen.has(item.proposal_id) && (
                        <div className="proposal-audit">
                          {auditEntries === null || auditEntries === undefined ? (
                            <p className="sum-muted">载入审计…</p>
                          ) : auditError ? (
                            <p className="sum-muted">审计加载失败：{auditError}</p>
                          ) : auditEntries.length === 0 ? (
                            <p className="sum-muted">还没有审计记录（采纳/拒绝/回滚后会写审计）</p>
                          ) : (
                            <ul className="proposal-audit-list">
                              {auditEntries.map((entry) => {
                                const rows = auditRows(entry)
                                return (
                                  <li className="proposal-audit-item" key={entry.id}>
                                    <div className="proposal-audit-head">
                                      <span className="proposal-audit-action">
                                        {AUDIT_ACTION_LABEL[entry.action] ?? entry.action}
                                      </span>
                                      <span className="muted">· {entry.actor}</span>
                                      <span className="muted proposal-audit-time">
                                        {formatTime(entry.created_at)}
                                      </span>
                                    </div>
                                    {rows.length === 0 ? (
                                      <p className="sum-muted">before / after 无字段差异</p>
                                    ) : (
                                      <div className="proposal-audit-diff">
                                        {rows.map((row) => (
                                          <div className="proposal-audit-row" key={row.field}>
                                            <span className="proposal-field">{row.field}</span>
                                            <span className="proposal-old" title={row.oldValue}>
                                              {row.oldValue}
                                            </span>
                                            <span className="proposal-arrow">→</span>
                                            <span className="proposal-new-text" title={row.newValue}>
                                              {row.newValue}
                                            </span>
                                          </div>
                                        ))}
                                      </div>
                                    )}
                                    {entry.note && (
                                      <p className="sum-muted proposal-audit-note">{entry.note}</p>
                                    )}
                                  </li>
                                )
                              })}
                            </ul>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                </li>
              )
            })}
          </ul>
        )}

        {tab === 'tasks' && (
          <>
            {taskError && (
              <div className="banner error proposal-error">
                任务加载失败：{taskError}
                <button className="link" onClick={() => void loadTaskRows()}>
                  重试
                </button>
              </div>
            )}
            {taskRows === null && !taskError && (
              <p className="sum-muted proposal-loading">载入任务…</p>
            )}
            {taskRows && taskRows.length === 0 && (
              <div className="empty proposal-empty">
                <h3>还没有 agent 任务</h3>
                <p>
                  在「待审」页签点「AI 复核待确认图」会排一批任务；外部 MCP agent
                  领任务后，这里能看到排队 / 运行 / 失败与重试。
                </p>
              </div>
            )}
            {taskRows && taskRows.length > 0 && (
              <ul className="proposal-list">
                {taskRows.map((task) => {
                  const busy = taskBusy === task.id
                  const canRetry = task.status === 'failed' || task.status === 'canceled'
                  return (
                    <li className="proposal-card" key={task.id}>
                      <div className="proposal-card-main">
                        <div className="proposal-card-head">
                          <span className="proposal-fig-label">
                            {TASK_KIND_LABEL[task.kind] ?? task.kind}
                          </span>
                          <span className={`proposal-status ${task.status}`}>
                            {TASK_STATUS_LABEL[task.status] ?? task.status}
                          </span>
                          <span className="proposal-conf">
                            {task.attempts}/{task.max_attempts} 次
                          </span>
                        </div>
                        <div className="proposal-paper">
                          目标 {task.target_type}:{task.target_id.slice(0, 8)}
                          {task.actor ? ` · 执行者 ${task.actor}` : ''}
                        </div>
                        <div className="proposal-meta">
                          <span>创建 {formatTime(task.created_at)}</span>
                          {task.lease_until && <span>租约至 {formatTime(task.lease_until)}</span>}
                          {task.cost > 0 && <span>费用 ${task.cost.toFixed(4)}</span>}
                        </div>
                        {task.error && <p className="proposal-note bad">错误：{task.error}</p>}
                        {canRetry && (
                          <div className="proposal-actions">
                            <button
                              className="ghost small"
                              disabled={busy}
                              onClick={() => void handleRetryTask(task.id)}
                              title="清空错误与租约，attempts 归零，重新排队"
                            >
                              {busy ? '重试中…' : '重试'}
                            </button>
                          </div>
                        )}
                      </div>
                    </li>
                  )
                })}
              </ul>
            )}
          </>
        )}
      </aside>
    </div>
  )
}
