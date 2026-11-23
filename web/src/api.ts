// SPDX-License-Identifier: AGPL-3.0-or-later

import type {
  AgentTask,
  AuditEntry,
  AuthStatus,
  AuthUser,
  AutoAcceptResult,
  CitationCheck,
  Claim,
  ClaimInput,
  ClaimRevision,
  Collection,
  ComparisonMatrix,
  CslJsonExport,
  DocExportCapabilities,
  Figure,
  FigureProposal,
  FigureSearch,
  GenerateSummaryResponse,
  ImportRun,
  Job,
  LabelStats,
  LlmSettings,
  LlmSettingsInput,
  LlmTestResult,
  MatrixDimensions,
  MetadataLookup,
  OcrProposal,
  Paper,
  PaperShare,
  PaperSections,
  PaperShareResult,
  PaperSummaryStats,
  PaperTable,
  PaperText,
  ProposalBatchResult,
  ProposalDecideResult,
  ProposalStats,
  ReferenceFormat,
  ReferenceStyles,
  Summary,
  SummaryDetail,
  SummaryRevision,
  TaskEnqueueResult,
  TableLatex,
  ThesisCheck,
  ThesisExport,
  UploadResult,
  Workgroup,
  WorkgroupDetail,
  WorkgroupMember,
  WritingExport,
  WritingOutline,
} from './types'

/**
 * 从错误响应体里抽出可读文本。
 *
 * 后端常规错误是 `{"detail": "..."}`；但 FastAPI/Pydantic 的 422 校验错误 detail 是
 * `[{loc: ["body","base_url"], msg: "field required", type: "..."}]` 数组，
 * 直接 String() 会显示成 `[object Object]`，这里拼成「字段: 原因」。
 */
function formatErrorDetail(parsed: unknown): string | null {
  if (!parsed || typeof parsed !== 'object') return null
  const detail = (parsed as { detail?: unknown }).detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const parts = detail
      .slice(0, 6)
      .map((item) => {
        if (typeof item === 'string') return item
        if (!item || typeof item !== 'object') return ''
        const { loc, msg } = item as { loc?: unknown; msg?: unknown }
        const where = Array.isArray(loc)
          ? loc
              .map((part) => String(part))
              // body/query/path 只是 FastAPI 的位置前缀，对用户没有信息量
              .filter((part) => part !== 'body' && part !== 'query' && part !== 'path')
              .join('.')
          : ''
        const reason = typeof msg === 'string' ? msg : ''
        if (!where) return reason
        return reason ? `${where}: ${reason}` : where
      })
      .filter(Boolean)
    return parts.length > 0 ? parts.join('；') : null
  }
  if (detail && typeof detail === 'object') return JSON.stringify(detail).slice(0, 200)
  return null
}

/**
 * 把一次失败的 HTTP 响应转成可读错误文本（普通 JSON / Pydantic 422 / 代理报错页都尽量兜住）。
 * 直接 fetch 二进制（PDF 等）的地方也走这里，保证错误信息口径一致。
 */
export async function describeHttpError(res: Response): Promise<string> {
  const text = await res.text().catch(() => '')
  let detail: string | null = null
  if (text) {
    try {
      detail = formatErrorDetail(JSON.parse(text))
    } catch {
      /* 不是 JSON（比如代理报错页），保留原文 */
      detail = text
    }
  }
  const suffix = detail ? ` · ${detail.slice(0, 240)}` : ''
  return `${res.status} ${res.statusText}${suffix}`
}

async function readJson<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(await describeHttpError(res))
  return (await res.json()) as T
}

export function searchFigures(
  query: string,
  limit = 60,
  labelState: 'review' | 'unlabeled' | 'low_confidence' | 'conflict' | 'unresolved' | null = null,
  opts: {
    favorite?: boolean
    collectionId?: string | null
    workgroupId?: string | null
    offset?: number
  } = {},
): Promise<FigureSearch> {
  const params = new URLSearchParams()
  if (query.trim()) params.set('q', query.trim())
  if (labelState) params.set('label_state', labelState)
  if (opts.favorite) params.set('favorite', 'true')
  if (opts.collectionId) params.set('collection_id', opts.collectionId)
  if (opts.workgroupId) params.set('workgroup_id', opts.workgroupId)
  params.set('limit', String(limit))
  // 批 E 契约：后端新增 offset 参数，total 是符合条件的真实总数（items 只是当前页）
  if (typeof opts.offset === 'number' && opts.offset > 0) params.set('offset', String(opts.offset))
  return fetch(`/api/figures?${params.toString()}`).then(readJson<FigureSearch>)
}

/** 图号校核待确认计数。 */
export function fetchLabelStats(): Promise<LabelStats> {
  return fetch('/api/figures/label-stats').then(readJson<LabelStats>)
}

/** 改图号（阶段 3 校核入口）。label 为空 = 确认这张图没有编号。 */
export function updateFigureLabel(figureId: string, label: string, reviewer = 'human'): Promise<Figure> {
  return fetch(`/api/figures/${figureId}/label`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ label, reviewer }),
  }).then(readJson<Figure>)
}

/** 系列子图拆开（split）或归并（merge）。 */
export function groupFigure(
  figureId: string,
  action: 'split' | 'merge',
  memberIds: string[] = [],
): Promise<Figure> {
  return fetch(`/api/figures/${figureId}/group`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, member_ids: memberIds }),
  }).then(readJson<Figure>)
}

/** 用户手动框选添加一张图（补解析器漏掉的图）。 */
export function createFigure(input: {
  paper_id: string
  page: number
  bbox: number[]
  label?: string
  caption?: string
}): Promise<Figure> {
  return fetch('/api/figures', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  }).then(readJson<Figure>)
}

/** 删除一张图（软删除，重解析不会让它复活）。 */
export function deleteFigure(figureId: string): Promise<{ figure_id: string; deleted: boolean }> {
  return fetch(`/api/figures/${figureId}`, { method: 'DELETE' }).then(
    readJson<{ figure_id: string; deleted: boolean }>,
  )
}

export function listPapers(
  collectionId?: string | null,
  opts: { favorite?: boolean; limit?: number; workgroupId?: string | null } = {},
): Promise<Paper[]> {
  const params = new URLSearchParams({ limit: String(opts.limit ?? 100) })
  if (collectionId) params.set('collection_id', collectionId)
  if (opts.favorite) params.set('favorite', 'true')
  if (opts.workgroupId) params.set('workgroup_id', opts.workgroupId)
  return fetch(`/api/papers?${params.toString()}`).then(readJson<Paper[]>)
}

/** 工作台：改备注 / 收藏 / 阅读状态（只动这三样，不改 Zotero 元数据）。 */
export function patchPaper(
  paperId: string,
  input: {
    note?: string | null
    favorite?: boolean
    reading_status?: string
    volume?: string | null
    issue?: string | null
    pages?: string | null
  },
): Promise<Paper> {
  return fetch(`/api/papers/${paperId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  }).then(readJson<Paper>)
}

/** 正文速览：按章节组织的正文块（只读）。 */
export function fetchPaperText(paperId: string): Promise<PaperText> {
  return fetch(`/api/papers/${paperId}/text`).then(readJson<PaperText>)
}

/** 轻量 OCR 校对提案：只有 OCR 与视觉模型不一致的页才会出现。 */
export function fetchOcrProposals(
  paperId: string,
  status: string = 'pending',
): Promise<OcrProposal[]> {
  return fetch(`/api/papers/${paperId}/ocr-proposals?status=${encodeURIComponent(status)}`).then(
    readJson<OcrProposal[]>,
  )
}

export function decideOcrProposal(
  proposalId: string,
  action: 'accept_ocr' | 'accept_model' | 'reject',
  reason?: string,
): Promise<OcrProposal> {
  return fetch(`/api/ocr-proposals/${proposalId}/decide`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, reason }),
  }).then(readJson<OcrProposal>)
}

/** 知识库目录树（M1）。 */
export function fetchCollections(): Promise<Collection[]> {
  return fetch('/api/collections').then(readJson<Collection[]>)
}

export function createCollection(name: string, parentId: string | null = null): Promise<Collection> {
  return fetch('/api/collections', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, parent_id: parentId }),
  }).then(readJson<Collection>)
}

/** 知识库操作台：改名 / 移动（parentId 传 null 表示移到顶层）。 */
export function updateCollection(
  collectionId: string,
  input: { name?: string; parent_id?: string | null },
): Promise<Collection> {
  return fetch(`/api/collections/${collectionId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  }).then(readJson<Collection>)
}

export function deleteCollection(collectionId: string): Promise<{ deleted: boolean }> {
  return fetch(`/api/collections/${collectionId}`, { method: 'DELETE' }).then(
    readJson<{ deleted: boolean }>,
  )
}

/** 把文献加入某个分组；已加入幂等。 */
export function addPaperToCollection(
  collectionId: string,
  paperId: string,
): Promise<{ collection_id: string; paper_id: string; added: boolean }> {
  return fetch(`/api/collections/${collectionId}/papers`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paper_id: paperId }),
  }).then(readJson<{ collection_id: string; paper_id: string; added: boolean }>)
}

/** 把文献移出某个分组（文献本身不删除）。 */
export function removePaperFromCollection(
  collectionId: string,
  paperId: string,
): Promise<{ collection_id: string; paper_id: string; removed: boolean }> {
  return fetch(`/api/collections/${collectionId}/papers/${paperId}`, { method: 'DELETE' }).then(
    readJson<{ collection_id: string; paper_id: string; removed: boolean }>,
  )
}

/** 从本机 Zotero（local API / sqlite）只读导入。 */
export function importZotero(): Promise<ImportRun> {
  return fetch('/api/import/zotero', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source: 'auto' }),
  }).then(readJson<ImportRun>)
}

/** 从 Zotero 导出的 JSON 文件导入（本机没装 Zotero 时用）。 */
export function importZoteroJson(file: File): Promise<ImportRun> {
  const form = new FormData()
  form.append('file', file)
  return fetch('/api/import/zotero/file', { method: 'POST', body: form }).then(
    readJson<ImportRun>,
  )
}

export function paperFigures(paperId: string): Promise<Figure[]> {
  return fetch(`/api/papers/${paperId}/figures`).then(readJson<Figure[]>)
}

// ---------------------------------------------------------------------------
// 章节级摘要（M2a）
// ---------------------------------------------------------------------------

/** 章节树（作者标题切章；block_count=0 的是纯父节点）。 */
export function fetchPaperSections(paperId: string): Promise<PaperSections> {
  return fetch(`/api/papers/${paperId}/sections`).then(readJson<PaperSections>)
}

/** 生成摘要：不传 sectionKeys = 整篇；force 才覆盖已有（人工锁定的永远跳过）。 */
export function generateSummaries(
  paperId: string,
  opts: { sectionKeys?: string[]; force?: boolean } = {},
): Promise<GenerateSummaryResponse> {
  const body: { section_keys?: string[]; force?: boolean } = {}
  if (opts.sectionKeys && opts.sectionKeys.length > 0) body.section_keys = opts.sectionKeys
  if (opts.force) body.force = true
  return fetch(`/api/papers/${paperId}/summaries/generate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(readJson<GenerateSummaryResponse>)
}

export function getSummary(summaryId: string): Promise<SummaryDetail> {
  return fetch(`/api/summaries/${summaryId}`).then(readJson<SummaryDetail>)
}

/** 人工编辑：保存后 source→human_edited、human_locked=true，重新生成不再覆盖。 */
export function updateSummary(
  summaryId: string,
  input: { content: string; key_points?: string[] },
): Promise<Summary> {
  return fetch(`/api/summaries/${summaryId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...input, actor: 'human' }),
  }).then(readJson<Summary>)
}

export function publishSummary(summaryId: string): Promise<Summary> {
  return fetch(`/api/summaries/${summaryId}/publish`, { method: 'POST' }).then(
    readJson<Summary>,
  )
}

export function listSummaryRevisions(summaryId: string): Promise<SummaryRevision[]> {
  return fetch(`/api/summaries/${summaryId}/revisions`).then(readJson<SummaryRevision[]>)
}

export function restoreSummaryRevision(
  summaryId: string,
  revisionId: string,
): Promise<Summary> {
  return fetch(`/api/summaries/${summaryId}/revisions/${revisionId}/restore`, {
    method: 'POST',
  }).then(readJson<Summary>)
}

// ---------------------------------------------------------------------------
// 整篇速览（AI 一键总结文献）
// ---------------------------------------------------------------------------

/** 取整篇速览；还没生成过返回 null（不是错误）。 */
export async function getPaperSummary(paperId: string): Promise<SummaryDetail | null> {
  const res = await fetch(`/api/papers/${paperId}/summary`)
  if (res.status === 404) return null
  return readJson<SummaryDetail>(res)
}

/** 一键总结整篇文献（一次模型调用）；force 才覆盖人工编辑过的。 */
export function generatePaperSummary(
  paperId: string,
  opts: { force?: boolean } = {},
): Promise<PaperSummaryStats> {
  return fetch(`/api/papers/${paperId}/summary`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ force: Boolean(opts.force) }),
  }).then(readJson<PaperSummaryStats>)
}

// ---------------------------------------------------------------------------
// 图提取 AI 纠错（M3）：建议队列
// ---------------------------------------------------------------------------

/** 对一张图跑一次 AI 纠错，返回 pending 建议（不直接改图）。 */
export function aiReviewFigure(figureId: string): Promise<FigureProposal> {
  return fetch(`/api/figures/${figureId}/ai-review`, { method: 'POST' }).then(
    readJson<FigureProposal>,
  )
}

/** 某张图的建议历史（默认只看 pending）。 */
export function listFigureProposals(
  figureId: string,
  status = 'pending',
): Promise<FigureProposal[]> {
  const params = new URLSearchParams({ status, limit: '20' })
  return fetch(`/api/figures/${figureId}/proposals?${params.toString()}`).then(
    readJson<FigureProposal[]>,
  )
}

/** 待审建议列表（可按文献/状态过滤）。status=all 返回全部历史（含 rolled_back）。 */
export function listProposals(
  opts: { status?: string; paperId?: string; limit?: number } = {},
): Promise<FigureProposal[]> {
  const params = new URLSearchParams()
  if (opts.status) params.set('status', opts.status)
  if (opts.paperId) params.set('paper_id', opts.paperId)
  if (opts.limit) params.set('limit', String(opts.limit))
  return fetch(`/api/proposals?${params.toString()}`).then(readJson<FigureProposal[]>)
}

/** 采纳/拒绝一条建议；采纳会写正式字段并返回更新后的图。 */
export function decideProposal(
  proposalId: string,
  action: 'accept' | 'reject',
  reason?: string,
): Promise<ProposalDecideResult> {
  return fetch(`/api/proposals/${proposalId}/decide`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, actor: 'human', reason }),
  }).then(readJson<ProposalDecideResult>)
}

/** 待审队列统计（顶栏徽标 / 页签计数）。 */
export function fetchProposalStats(): Promise<ProposalStats> {
  return fetch('/api/proposals/stats').then(readJson<ProposalStats>)
}

/**
 * 扫一遍 pending 建议，把满足自动接受条件的直接写正式字段（纯数据库操作，不调模型）。
 * 返回 accepted/skipped 明细。
 */
export function autoAcceptProposals(
  opts: { limit?: number; paperId?: string | null } = {},
): Promise<AutoAcceptResult> {
  const body: { limit: number; paper_id?: string } = { limit: opts.limit ?? 50 }
  if (opts.paperId) body.paper_id = opts.paperId
  return fetch('/api/proposals/auto-accept', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(readJson<AutoAcceptResult>)
}

/** 批量采纳/拒绝；逐条失败放进 failed，不影响其它条。 */
export function decideProposalBatch(
  proposalIds: string[],
  action: 'accept' | 'reject',
  opts: { actor?: string; reason?: string } = {},
): Promise<ProposalBatchResult> {
  return fetch('/api/proposals/decide-batch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      proposal_ids: proposalIds,
      action,
      actor: opts.actor ?? 'human',
      reason: opts.reason,
    }),
  }).then(readJson<ProposalBatchResult>)
}

/** 回滚一条已采纳建议：按 before 快照恢复图号/图注/复核标记，并写审计。 */
export function rollbackProposal(
  proposalId: string,
  actor = 'human',
): Promise<ProposalDecideResult> {
  return fetch(`/api/proposals/${proposalId}/rollback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ actor }),
  }).then(readJson<ProposalDecideResult>)
}

/** 提案审计日志（每次正式写都有 before/after）。 */
export function fetchAuditLog(
  opts: { targetType?: string; targetId?: string; action?: string; limit?: number } = {},
): Promise<AuditEntry[]> {
  const params = new URLSearchParams()
  if (opts.targetType) params.set('target_type', opts.targetType)
  if (opts.targetId) params.set('target_id', opts.targetId)
  if (opts.action) params.set('action', opts.action)
  params.set('limit', String(opts.limit ?? 50))
  return fetch(`/api/audit?${params.toString()}`).then(readJson<AuditEntry[]>)
}

/** 把待确认图排成 AI 复核任务（worker 后台逐张跑，不阻塞 UI）。 */
export function enqueueFigureReviewTasks(
  opts: {
    labelState?: 'unlabeled' | 'low_confidence' | 'unreviewed_all'
    limit?: number
    paperId?: string | null
    figureIds?: string[]
  } = {},
): Promise<TaskEnqueueResult> {
  const body: {
    label_state: string
    limit: number
    paper_id?: string
    figure_ids?: string[]
  } = {
    label_state: opts.labelState ?? 'unreviewed_all',
    limit: opts.limit ?? 20,
  }
  if (opts.paperId) body.paper_id = opts.paperId
  if (opts.figureIds && opts.figureIds.length > 0) body.figure_ids = opts.figureIds
  return fetch('/api/tasks/enqueue-figures', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(readJson<TaskEnqueueResult>)
}

/** agent 任务快照（M3）：队列里显示「AI 复核进行中 N 条」。 */
export function listAgentTasks(
  opts: { status?: string; kind?: string; limit?: number } = {},
): Promise<AgentTask[]> {
  const params = new URLSearchParams()
  if (opts.status) params.set('status', opts.status)
  if (opts.kind) params.set('kind', opts.kind)
  params.set('limit', String(opts.limit ?? 100))
  return fetch(`/api/tasks?${params.toString()}`).then(readJson<AgentTask[]>)
}


export function getFigure(figureId: string): Promise<Figure> {
  return fetch(`/api/figures/${figureId}`).then(readJson<Figure>)
}

export function reviewFigure(
  figureId: string,
  verdict: 'ok' | 'wrong',
  opts: { note?: string; bbox?: number[]; reviewer?: string } = {},
): Promise<Figure> {
  return fetch(`/api/figures/${figureId}/review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ verdict, reviewer: 'human', ...opts }),
  }).then(readJson<Figure>)
}

/** 上传 PDF。服务端流式落盘 + sha256 去重，返回一个待消费的 job。 */
export function uploadPaper(file: File): Promise<UploadResult> {
  const form = new FormData()
  form.append('file', file)
  return fetch('/api/papers', { method: 'POST', body: form }).then(readJson<UploadResult>)
}

/** 给已有文献补传 PDF（Zotero 只读导入的条目只有元数据）：建版本 + 排队解析。 */
export function attachPaperPdf(paperId: string, file: File): Promise<UploadResult> {
  const form = new FormData()
  form.append('file', file)
  return fetch(`/api/papers/${paperId}/pdf`, { method: 'POST', body: form }).then(
    readJson<UploadResult>,
  )
}

export function getPaper(paperId: string): Promise<Paper> {
  return fetch(`/api/papers/${paperId}`).then(readJson<Paper>)
}

export function getJob(jobId: string): Promise<Job> {
  return fetch(`/api/jobs/${jobId}`).then(readJson<Job>)
}

export async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    // 非安全上下文（http + 非 localhost）下 clipboard 不可用，退回老办法
    const area = document.createElement('textarea')
    area.value = text
    document.body.appendChild(area)
    area.select()
    document.execCommand('copy')
    document.body.removeChild(area)
  }
}

// ---------------------------------------------------------------------------
// 模型 API 设置（BYO key；key 只存服务端，读回只有掩码）
// ---------------------------------------------------------------------------

export function fetchLlmSettings(): Promise<LlmSettings> {
  return fetch('/api/settings/llm').then(readJson<LlmSettings>)
}

/** 保存配置。api_key 留空 = 保留已保存的 key（后端不回传原文）。 */
export function saveLlmSettings(input: LlmSettingsInput): Promise<LlmSettings> {
  return fetch('/api/settings/llm', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  }).then(readJson<LlmSettings>)
}

/** 清除界面配置，回落到环境变量 / mock。 */
export function clearLlmSettings(): Promise<LlmSettings> {
  return fetch('/api/settings/llm', { method: 'DELETE' }).then(readJson<LlmSettings>)
}

/** 用当前表单值测连通性，不落库；不传 body 时用已保存配置（不能发 `{}`，否则后端 422）。 */
export function testLlmSettings(input?: LlmSettingsInput): Promise<LlmTestResult> {
  return fetch('/api/settings/llm/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: input ? JSON.stringify(input) : undefined,
  }).then(readJson<LlmTestResult>)
}

// ---------------------------------------------------------------------------
// M5 写作侧
// ---------------------------------------------------------------------------
/** 写作素材卡片（claim + 证据）。 */
export function listClaims(
  opts: { paperId?: string; status?: string; q?: string; limit?: number } = {},
): Promise<Claim[]> {
  const params = new URLSearchParams()
  if (opts.paperId) params.set('paper_id', opts.paperId)
  if (opts.status) params.set('status', opts.status)
  if (opts.q) params.set('q', opts.q)
  params.set('limit', String(opts.limit ?? 200))
  return fetch(`/api/claims?${params.toString()}`).then(readJson<Claim[]>)
}

export function createClaim(input: ClaimInput): Promise<Claim> {
  return fetch('/api/claims', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  }).then(readJson<Claim>)
}

export function updateClaim(
  claimId: string,
  input: Partial<Omit<ClaimInput, 'paper_id'>>,
): Promise<Claim> {
  return fetch(`/api/claims/${claimId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  }).then(readJson<Claim>)
}

export async function deleteClaim(claimId: string): Promise<void> {
  const res = await fetch(`/api/claims/${claimId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(await describeHttpError(res))
}

/** claim 的历史版本（新→旧）。 */
export function listClaimRevisions(claimId: string): Promise<ClaimRevision[]> {
  return fetch(`/api/claims/${claimId}/revisions`).then(readJson<ClaimRevision[]>)
}

/** 恢复到某个历史版本（恢复前会先把当前状态压一份）。 */
export function restoreClaimRevision(
  claimId: string,
  revisionId: string,
): Promise<Claim> {
  return fetch(`/api/claims/${claimId}/revisions/${revisionId}/restore`, {
    method: 'POST',
  }).then(readJson<Claim>)
}

/** 对比矩阵（派生，不落库）；dimensions 不传 = 默认 7 行。 */
export function fetchMatrix(
  paperIds: string[],
  dimensions?: string[],
): Promise<ComparisonMatrix> {
  const params = new URLSearchParams({ paper_ids: paperIds.join(',') })
  if (dimensions && dimensions.length > 0) params.set('dimensions', dimensions.join(','))
  return fetch(`/api/matrix?${params.toString()}`).then(readJson<ComparisonMatrix>)
}

/** 矩阵可选维度清单。 */
export function fetchMatrixDimensions(): Promise<MatrixDimensions> {
  return fetch('/api/matrix/dimensions').then(readJson<MatrixDimensions>)
}

export function exportWriting(input: {
  format: 'latex' | 'bibtex' | 'markdown'
  paperIds?: string[]
  claimIds?: string[]
  title?: string
  includeDrafts?: boolean
  withEvidenceComments?: boolean
}): Promise<WritingExport> {
  return fetch('/api/writing/export', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      format: input.format,
      paper_ids: input.paperIds,
      claim_ids: input.claimIds,
      title: input.title ?? '',
      include_drafts: input.includeDrafts ?? true,
      with_evidence_comments: input.withEvidenceComments ?? false,
    }),
  }).then(readJson<WritingExport>)
}

/** 写作大纲 + 引用/图表交叉检查（只读派生，不落库）。 */
export function fetchWritingOutline(input: {
  paperIds?: string[]
  claimIds?: string[]
  includeDrafts?: boolean
  sectionOrder?: string[]
} = {}): Promise<WritingOutline> {
  return fetch('/api/writing/outline', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      paper_ids: input.paperIds,
      claim_ids: input.claimIds,
      include_drafts: input.includeDrafts ?? true,
      section_order: input.sectionOrder,
    }),
  }).then(readJson<WritingOutline>)
}

/** Word/PDF 导出能力探测（缺 pandoc/typst 时 UI 置灰）。 */
export function fetchDocExportCapabilities(): Promise<DocExportCapabilities> {
  return fetch('/api/writing/export/capabilities').then(
    readJson<DocExportCapabilities>,
  )
}

/** 导出 claim 草稿为 Word/PDF（二进制响应；错误按 describeHttpError 口径抛出）。 */
export async function exportWritingDocument(input: {
  format: 'docx' | 'pdf'
  paperIds?: string[]
  claimIds?: string[]
  title?: string
  includeDrafts?: boolean
  cslStyle?: string
}): Promise<{ blob: Blob; filename: string }> {
  const res = await fetch('/api/writing/export/document', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      format: input.format,
      paper_ids: input.paperIds,
      claim_ids: input.claimIds,
      title: input.title ?? '',
      include_drafts: input.includeDrafts ?? true,
      csl_style: input.cslStyle ?? 'gbt7714',
    }),
  })
  if (!res.ok) throw new Error(await describeHttpError(res))
  const disposition = res.headers.get('content-disposition') ?? ''
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(disposition)?.[1]
  const filename = encoded
    ? decodeURIComponent(encoded)
    : `litbench-draft.${input.format}`
  return { blob: await res.blob(), filename }
}

/** 元数据补全查询（Crossref / arXiv；只读，不落库）。 */
export function lookupPaperMetadata(
  paperId: string,
  input: {
    mode?: 'auto' | 'doi' | 'arxiv' | 'search'
    doi?: string | null
    arxivId?: string | null
    query?: string | null
  } = {},
): Promise<MetadataLookup> {
  return fetch(`/api/papers/${paperId}/metadata/lookup`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode: input.mode ?? 'auto',
      doi: input.doi ?? null,
      arxiv_id: input.arxivId ?? null,
      query: input.query ?? null,
    }),
  }).then(readJson<MetadataLookup>)
}

/** 把确认过的字段写入文献（默认只填空白；覆盖已有值要 overwrite）。 */
export function applyPaperMetadata(
  paperId: string,
  fields: Record<string, unknown>,
  overwrite = false,
): Promise<Paper> {
  return fetch(`/api/papers/${paperId}/metadata/apply`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ fields, overwrite }),
  }).then(readJson<Paper>)
}

export function checkCitations(text: string): Promise<CitationCheck> {
  return fetch('/api/writing/check-citations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  }).then(readJson<CitationCheck>)
}

/** 学位论文 LaTeX 骨架（M8）：.tex + refs.bib + references_gbt.tex，只读派生。 */
export function exportThesis(input: {
  title?: string
  author?: string
  school?: string
  degree?: string
  advisor?: string
  year?: number | null
  paperIds?: string[]
  claimIds?: string[]
  chapterOrder?: string[]
  includeDrafts?: boolean
}): Promise<ThesisExport> {
  return fetch('/api/thesis/export', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      title: input.title ?? '',
      author: input.author ?? '',
      school: input.school ?? '',
      degree: input.degree ?? '',
      advisor: input.advisor ?? '',
      year: input.year ?? null,
      paper_ids: input.paperIds,
      claim_ids: input.claimIds,
      chapter_order: input.chapterOrder,
      include_drafts: input.includeDrafts ?? true,
    }),
  }).then(readJson<ThesisExport>)
}

/** 学位论文稿子的引用/图表标签交叉检查（确定性，不调模型）。 */
export function checkThesis(input: {
  text: string
  paperIds?: string[]
}): Promise<ThesisCheck> {
  return fetch('/api/thesis/check', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: input.text, paper_ids: input.paperIds }),
  }).then(readJson<ThesisCheck>)
}

/** 论文里的表格（claim 证据选择用）。 */
export function paperTables(paperId: string): Promise<PaperTable[]> {
  return fetch(`/api/papers/${paperId}/tables`).then(readJson<PaperTable[]>)
}

/** 单张表 → LaTeX（M9.3a）：tabular / 浮动体 / 合并单元格 warnings。 */
export function tableLatex(paperId: string, tableId: string): Promise<TableLatex> {
  return fetch(`/api/papers/${paperId}/tables/${tableId}/latex`).then(
    readJson<TableLatex>,
  )
}

/** 人工重试 failed / canceled 的 agent 任务（M3 收尾任务面板）。 */
export function retryAgentTask(taskId: string): Promise<AgentTask> {
  return fetch(`/api/tasks/${taskId}/retry`, { method: 'POST' }).then(readJson<AgentTask>)
}

/** 回收过期租约（把长期 running 但没人续租的任务重排）。 */
export function recoverExpiredTasks(): Promise<{ recovered: number }> {
  return fetch('/api/tasks/recover-expired', { method: 'POST' }).then(
    readJson<{ recovered: number }>,
  )
}

// ---------------------------------------------------------------------------
// 参考文献条目（2026-11-13）
// ---------------------------------------------------------------------------
export function fetchReferenceStyles(): Promise<ReferenceStyles> {
  return fetch('/api/references/styles').then(readJson<ReferenceStyles>)
}

export function formatReferences(input: {
  paperIds: string[]
  style: string
  numbered?: boolean
  template?: string | null
  engine?: 'template' | 'csl'
}): Promise<ReferenceFormat> {
  return fetch('/api/references/format', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      paper_ids: input.paperIds,
      style: input.style,
      numbered: input.numbered ?? true,
      template: input.template ?? null,
      engine: input.engine ?? 'template',
    }),
  }).then(readJson<ReferenceFormat>)
}

/** CSL JSON 导出（pandoc / Zotero 通用输入；不落库）。 */
export function exportReferenceCslJson(paperIds: string[]): Promise<CslJsonExport> {
  return fetch('/api/references/csl-json', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paper_ids: paperIds }),
  }).then(readJson<CslJsonExport>)
}

// ---------------------------------------------------------------------------
// 团队共享（2026-11-15）：账号 / 登录
// ---------------------------------------------------------------------------
export function getAuthStatus(): Promise<AuthStatus> {
  return fetch('/api/auth/status').then(readJson<AuthStatus>)
}

export function login(username: string, password: string): Promise<AuthStatus> {
  return fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  }).then(readJson<AuthStatus>)
}

export function setupAdmin(input: {
  username: string
  password: string
  displayName?: string
}): Promise<AuthStatus> {
  return fetch('/api/auth/setup', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      username: input.username,
      password: input.password,
      display_name: input.displayName ?? '',
    }),
  }).then(readJson<AuthStatus>)
}

export async function logout(): Promise<void> {
  const res = await fetch('/api/auth/logout', { method: 'POST' })
  if (!res.ok) throw new Error(await describeHttpError(res))
}

export function listUsers(): Promise<AuthUser[]> {
  return fetch('/api/users').then(readJson<AuthUser[]>)
}

export function createUser(input: {
  username: string
  password: string
  displayName?: string
  role?: 'admin' | 'member'
}): Promise<AuthUser> {
  return fetch('/api/users', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      username: input.username,
      password: input.password,
      display_name: input.displayName ?? '',
      role: input.role ?? 'member',
    }),
  }).then(readJson<AuthUser>)
}

export function patchUser(
  userId: string,
  input: { isActive?: boolean; role?: 'admin' | 'member'; password?: string },
): Promise<AuthUser> {
  return fetch(`/api/users/${userId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      is_active: input.isActive,
      role: input.role,
      password: input.password,
    }),
  }).then(readJson<AuthUser>)
}

// ---------------------------------------------------------------------------
// 团队共享（2026-11-15 第二步）：工作组 / 成员 / 文献共享
// ---------------------------------------------------------------------------
export function listWorkgroups(): Promise<Workgroup[]> {
  return fetch('/api/workgroups').then(readJson<Workgroup[]>)
}

export function createWorkgroup(name: string, description = ''): Promise<WorkgroupDetail> {
  return fetch('/api/workgroups', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, description }),
  }).then(readJson<WorkgroupDetail>)
}

export function getWorkgroup(workgroupId: string): Promise<WorkgroupDetail> {
  return fetch(`/api/workgroups/${workgroupId}`).then(readJson<WorkgroupDetail>)
}

export function updateWorkgroup(
  workgroupId: string,
  input: { name?: string; description?: string | null },
): Promise<WorkgroupDetail> {
  return fetch(`/api/workgroups/${workgroupId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  }).then(readJson<WorkgroupDetail>)
}

export async function deleteWorkgroup(workgroupId: string): Promise<void> {
  const res = await fetch(`/api/workgroups/${workgroupId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(await describeHttpError(res))
}

export function addWorkgroupMember(
  workgroupId: string,
  username: string,
): Promise<WorkgroupMember> {
  return fetch(`/api/workgroups/${workgroupId}/members`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username }),
  }).then(readJson<WorkgroupMember>)
}

export async function removeWorkgroupMember(
  workgroupId: string,
  userId: string,
): Promise<void> {
  const res = await fetch(`/api/workgroups/${workgroupId}/members/${userId}`, {
    method: 'DELETE',
  })
  if (!res.ok) throw new Error(await describeHttpError(res))
}

export function sharePaper(paperId: string, workgroupId: string): Promise<PaperShareResult> {
  return fetch(`/api/papers/${paperId}/share`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ workgroup_id: workgroupId }),
  }).then(readJson<PaperShareResult>)
}

export async function unsharePaper(paperId: string, workgroupId: string): Promise<void> {
  const res = await fetch(`/api/papers/${paperId}/share/${workgroupId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(await describeHttpError(res))
}

export function listPaperShares(paperId: string): Promise<PaperShare[]> {
  return fetch(`/api/papers/${paperId}/shares`).then(readJson<PaperShare[]>)
}
