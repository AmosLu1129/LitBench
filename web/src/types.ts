// SPDX-License-Identifier: AGPL-3.0-or-later

export interface Figure {
  figure_id: string
  cite_key: string
  paper_id: string
  paper_title: string
  year: number | null
  version_id: string
  version_kind: string
  page: number
  label_raw: string | null
  label_norm: string | null
  sublabel: string | null
  caption: string
  figure_type: string | null
  license_hint: string
  bbox: number[]
  image_url: string | null
  review_status: 'pending' | 'ok' | 'wrong' | string
  review_note: string | null
  bbox_override: number[] | null
  // 图号校核（docs/14）：编号从哪来、有多可信、属于哪个子图组
  label_source: string | null
  label_confidence: number | null
  group_key: string | null
  /** parser = 解析器产出；manual = 用户在原文里框选添加 */
  origin: string
}

export interface FigureSearch {
  mode: 'provenance' | 'semantic' | 'browse' | string
  degraded: boolean
  /** 实际引擎：bm25（M9.1）/ like（旧基线）/ provenance / browse */
  engine?: string
  hint: string | null
  /** 符合条件的真实总数（后端批 E 起；items 只是当前 limit/offset 命中的一页） */
  total: number
  items: Figure[]
}

/** 图号校核的待确认计数（顶栏徽标用）。 */
export interface LabelStats {
  unlabeled: number
  low_confidence: number
  conflict: number
  unresolved: number
  review_total: number
}

export interface Paper {
  id: string
  title: string
  /** 作者原始结构：[{firstName,lastName} | {name}]；元数据补全后可更新 */
  authors_json?: Array<Record<string, unknown>>
  doi: string | null
  arxiv_id: string | null
  cite_key?: string | null
  year: number | null
  venue?: string | null
  /** 著录字段（2026-11-13）：生成 references 条目用；缺失时格式器省略并提示 */
  volume?: string | null
  issue?: string | null
  pages?: string | null
  /** 条目类型（迁移 0015）：journal|conference|preprint|thesis|book|report|other */
  entry_type?: string
  /** 学位论文著录字段（迁移 0015）：学校/学位/出版地；缺失时格式器省略并提示 */
  thesis_school?: string | null
  thesis_degree?: string | null
  thesis_place?: string | null
  status: string
  figure_count: number
  table_count: number
  // 知识库（M1）：Zotero 只读镜像
  abstract?: string | null
  tags_json?: string[]
  reading_status?: string
  metadata_source?: string
  zotero_item_key?: string | null
  /** 是否已有 PDF（paper_version）。Zotero 只读导入的纯元数据条目为 false。 */
  has_version?: boolean
  /** 工作台：用户备注与收藏（2026-11-12 改版） */
  note?: string | null
  favorite?: boolean
  /** 该文献所属的分组（知识库操作台显示勾选） */
  collection_ids?: string[]
  // 数据归属（M7 第二步，2026-11-15）：owner 为空 = legacy 全局数据
  owner_user_id?: string | null
  owner_username?: string | null
  /** 当前账号能否改这篇文献（共享文献对非 owner 只读） */
  can_edit?: boolean
  /** 这篇文献已共享到的工作组 id（我所在的组） */
  shared_workgroup_ids?: string[]
}

/** 文献正文速览：按章节组织的一份只读正文流。 */
export interface PaperTextBlock {
  block_id: string
  page: number
  type: string
  text: string
  bbox: number[]
}

export interface PaperText {
  paper_id: string
  version_id: string
  page_count: number | null
  block_count: number
  // 正文速览的章节结构：与「章节摘要」的 SectionItem 不是同一份数据，内联即可
  sections: Array<{
    key: string
    title: string
    level: number
    kind: 'heading' | 'page' | string
    page_start: number
    page_end: number
    blocks: PaperTextBlock[]
  }>
}

/** OCR 与大模型校对不一致的页级提案（轻量 OCR 工作流）。 */
export interface OcrProposal {
  proposal_id: string
  paper_version_id: string
  page: number
  ocr_text: string
  model_text: string
  differences: Array<{ ocr?: string; model?: string; reason?: string }>
  status: 'pending' | 'accepted_ocr' | 'accepted_model' | 'rejected' | string
  provider: string
  model: string
  cost: number
  created_at: string | null
}

/** 知识库目录树节点（M1）。平铺返回，前端按 parent_id 组装。 */
export interface Collection {
  id: string
  name: string
  parent_id: string | null
  sort_order: number
  source: string
  zotero_key: string | null
  paper_count: number
}

/** 一次 Zotero / JSON 导入的结果。 */
export interface ImportRun {
  id: string
  source: string
  started_at?: string | null
  finished_at?: string | null
  stats: Record<string, number | string>
  error?: string | null
}

/** 上传的返回：立刻建了 job，解析在后台跑。 */
export interface UploadResult {
  paper_id: string
  version_id: string
  job_id: string
  sha256: string
  deduplicated: boolean
  page_count: number | null
}

/** 后台任务快照。前端轮询它推进度，不依赖 SSE（少一条长连接要维护）。 */
export interface Job {
  id: string
  kind: string
  status: 'queued' | 'running' | 'done' | 'failed' | string
  progress: number
  error: string | null
  attempts: number
  paper_version_id: string | null
  created_at: string | null
  /** 解析后端；null = 全局默认（pdfium） */
  stage: string | null
  escalated_from: string | null
  escalated_to: string | null
  escalate_reason: string | null
}

export const MODE_LABEL: Record<string, string> = {
  provenance: '出处检索',
  semantic: '语义检索（降级）',
  browse: '浏览',
}

// ---------------------------------------------------------------------------
// 章节级摘要（M2a）
// ---------------------------------------------------------------------------

/** 一个章节：kind=heading 是作者标题，page 是按页兜底的伪章节。
 *  block_count=0 的是纯父节点（只分组，不生成摘要）。 */
export interface SectionItem {
  key: string
  title: string
  level: number
  kind: 'heading' | 'page' | string
  page_start: number
  page_end: number
  block_count: number
  char_count: number
  figure_count: number
  // 列表里只带摘要状态摘要；完整内容点开再拉 SummaryDetail
  summary: {
    summary_id: string
    status: 'draft' | 'published' | string
    source: 'agent' | 'human_edited' | string
    auto_accepted: boolean
    human_locked: boolean
    content_chars: number
  } | null
}

export interface PaperSections {
  paper_id: string
  version_id: string
  page_count: number
  sections: SectionItem[]
}

/** 摘要详情里的证据块：点它跳回原文那一页并高亮 bbox。 */
export interface SummaryEvidence {
  block_id: string
  page: number
  type: string
  text: string
  bbox: number[]
}

export interface Summary {
  summary_id: string
  paper_version_id: string
  section_key: string
  section_title: string
  level: number
  content: string
  key_points: string[]
  evidence_block_ids: string[]
  source: 'agent' | 'human_edited' | string
  provider: string
  model: string
  prompt_version: string
  status: 'draft' | 'published' | string
  auto_accepted: boolean
  human_locked: boolean
  tokens_in: number | null
  tokens_out: number | null
  cost: number | null
  /** 模型自评置信度（0–1）：自动接受阈值校准的依据；人工编辑后可能为空。 */
  confidence: number | null
  created_at: string | null
  updated_at: string | null
  edited_by: string | null
  edited_at: string | null
}

export interface SummaryDetail extends Summary {
  evidence: SummaryEvidence[]
}

/** 历史版本（编辑/发布/回滚都会留一条）。 */
export interface SummaryRevision {
  revision_id: string
  summary_id: string
  content: string
  key_points: string[]
  evidence_block_ids: string[]
  source: string
  provider: string
  model: string
  prompt_version: string
  confidence: number | null
  status: string
  reason: string
  edited_by: string | null
  created_at: string | null
}

export interface GenerateSectionResult {
  section_key: string
  section_title: string
  status:
    | 'created'
    | 'updated'
    | 'skipped_existing'
    | 'skipped_locked'
    | 'skipped_empty'
    | 'failed'
    | string
  summary_id: string | null
  error: string | null
  confidence?: number | null
  tokens_in?: number
  tokens_out?: number
  cost?: number
}

export interface GenerateSummaryResponse {
  paper_id: string
  version_id: string
  provider: string
  model: string
  prompt_version: string
  sections_total: number
  requested: number
  created: number
  updated: number
  skipped_existing: number
  skipped_locked: number
  skipped_empty: number
  failed: number
  tokens_in_total: number
  tokens_out_total: number
  cost_total: number
  results: GenerateSectionResult[]
}

/** 整篇速览（AI 一键总结文献）：一次模型调用的返回。 */
export interface PaperSummaryStats {
  paper_id: string
  version_id: string
  status: 'created' | 'updated' | string
  summary_id: string
  provider: string
  model: string
  prompt_version: string
  confidence: number
  auto_accepted: boolean
  tokens_in: number
  tokens_out: number
  cost: number
  latency_ms: number
}

// ---------------------------------------------------------------------------
// 图提取 AI 纠错（M3）：建议队列
// ---------------------------------------------------------------------------

/** 模型对一张图给出的建议（只提议，采纳前不改图）。 */
export interface FigureProposal {
  proposal_id: string
  figure_id: string
  paper_version_id: string
  kind: string
  proposed: {
    label_ok?: boolean
    suggested_label?: string | null
    suggested_label_norm?: string | null
    caption_issue?: string
    cleaned_caption?: string
    crop_issue?: string
    needs_change?: boolean
    reason?: string
    summary?: string
    confidence?: number
    current_label?: string | null
    current_caption?: string
    validation_notes?: string[]
    // M3c 框复核
    verdict?: 'ok' | 'wrong' | string
    bbox?: number[] | null
    bbox_points?: number[] | null
    current_bbox?: number[] | null
    iou?: number
    page?: number
    page_size?: number[]
    caption_anchor?: {
      ok?: boolean
      block_id?: string | null
      gap?: number | null
      caption?: string | null
    } | null
  }
  evidence: Array<{ type: string; value: string }>
  confidence: number
  status: 'pending' | 'accepted' | 'rejected' | 'superseded' | 'no_change' | 'rolled_back' | string
  provider: string
  model: string
  prompt_version: string
  /** vision = 模型看过裁剪图；text = 只有文字上下文 */
  mode: 'vision' | 'text' | string
  tokens_in: number
  tokens_out: number
  cost: number
  latency_ms: number
  created_at: string | null
  decided_at: string | null
  decided_by: string | null
  decision_reason: string | null
  /** M3：采纳前快照（只在采纳/自动接受后写入；pending 为 null）。回滚按它恢复。 */
  before: FigureSnapshot | null
  /** true = 自动接受器写入（不是人工采纳） */
  auto_accepted: boolean
  rolled_back_at: string | null
  rolled_back_by: string | null
  figure?: {
    figure_id: string
    label_raw: string | null
    label_norm: string | null
    caption: string
    page: number
    image_url: string | null
    bbox_override?: number[] | null
  } | null
  paper?: { paper_id: string; title: string } | null
}

/** 采纳前快照（proposal.before / 审计 before/after）：只含会被 AI 建议改写的图字段。 */
export interface FigureSnapshot {
  label_raw?: string | null
  label_norm?: string | null
  label_num?: number | null
  sublabel?: string | null
  label_source?: string | null
  label_confidence?: number | null
  caption?: string | null
  caption_source?: string | null
  review_status?: string | null
  review_note?: string | null
  reviewed_by?: string | null
  reviewed_at?: string | null
  /** M3c：框复核采纳前的修正框快照（回滚按它恢复） */
  bbox_override_json?: number[] | null
  image_path?: string | null
}

export interface ProposalDecideResult {
  proposal: FigureProposal
  applied: Record<string, unknown>
  figure: Figure | null
}

/** GET /api/proposals/stats：顶栏徽标 + 队列页签计数。 */
export interface ProposalStats {
  total: number
  pending: number
  accepted: number
  rejected: number
  rolled_back: number
  by_status: Record<string, number>
  by_kind: Record<string, number>
}

/** POST /api/proposals/auto-accept：自动接受扫描结果。 */
export interface AutoAcceptItem {
  proposal_id: string
  figure_id?: string
  applied?: Record<string, unknown>
  confidence?: number
}

export interface AutoAcceptSkippedItem {
  proposal_id: string
  figure_id?: string
  reason: string
}

export interface AutoAcceptResult {
  scanned: number
  accepted: number
  skipped: number
  items: AutoAcceptItem[]
  skipped_items: AutoAcceptSkippedItem[]
}

/** POST /api/proposals/decide-batch：批量采纳/拒绝结果（逐条失败不拖垮整批）。 */
export interface ProposalBatchResult {
  requested: number
  accepted: number
  rejected: number
  failed: Array<{ proposal_id: string; error: string }>
}

/** GET /api/audit：M3 审计（每次正式写都有 before/after）。 */
export interface AuditEntry {
  id: string
  actor: string
  action: string
  target_type: string
  target_id: string
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
  evidence: unknown[] | null
  note: string | null
  created_at: string | null
}

/** GET /api/tasks：agent 任务租约（M3），队列里用它显示「AI 复核进行中 N 条」。 */
export interface AgentTask {
  id: string
  kind: string
  target_type: string
  target_id: string
  payload: Record<string, unknown>
  status: 'queued' | 'running' | 'done' | 'failed' | 'canceled' | string
  priority: number
  attempts: number
  max_attempts: number
  actor: string | null
  lease_until: string | null
  heartbeat_at: string | null
  result: Record<string, unknown> | null
  error: string | null
  cost: number
  created_at: string | null
  started_at: string | null
  finished_at: string | null
}

/** POST /api/tasks/enqueue-figures：把待确认图排成 AI 复核任务。 */
export interface TaskEnqueueResult {
  created: number
  skipped: number
  task_ids: string[]
}

// ---------------------------------------------------------------------------
// 模型 API 设置（BYO key）
// ---------------------------------------------------------------------------

/** 后端支持的协议：名称/端点/说明都由后端下发，前端不再硬编码第二份。 */
export interface LlmProtocolOption {
  protocol: string
  label: string
  path: string
  hint: string
}

/** GET /api/settings/llm 的返回；api_key 永不回传，只给掩码。 */
export interface LlmSettings {
  configured: boolean
  source: 'ui' | 'env' | 'none' | string
  protocol: string | null
  protocol_label: string | null
  protocol_path: string | null
  base_url: string | null
  model: string | null
  has_api_key: boolean
  api_key_masked: string | null
  endpoint: string | null
  timeout_s: number | null
  updated_at: string | null
  /** 最近一次「测试连接」的结果（顶栏状态灯用） */
  last_test_ok: boolean
  last_tested_at: string | null
  supported_protocols: LlmProtocolOption[]
  note: string
}

/** PUT /api/settings/llm 的 body；api_key 留空 = 保留已保存的 key。 */
export interface LlmSettingsInput {
  protocol: string
  base_url: string
  model: string
  api_key?: string
  timeout_s?: number
}

/** POST /api/settings/llm/test 的返回（不落库，只测连通性）。 */
export interface LlmTestResult {
  ok: boolean
  error: string | null
  protocol: string
  model: string
  endpoint: string
  latency_ms: number | null
  reply: string | null
  tokens_in: number | null
  tokens_out: number | null
}

// ---------------------------------------------------------------------------
// M5 写作侧（claim 卡片 / 对比矩阵 / 导出 / 引用校验）
// ---------------------------------------------------------------------------
export interface ClaimEvidence {
  type: 'block' | 'figure' | 'table' | 'summary'
  id: string
  page?: number | null
  label?: string
  snippet?: string
}

export interface Claim {
  claim_id: string
  paper_id: string
  paper_title: string | null
  cite_key: string | null
  paper_version_id: string | null
  text: string
  section: string
  status: 'draft' | 'approved' | string
  source: string
  evidence: ClaimEvidence[]
  /** 重解析后有证据映射不回来：卡片保留，但需要重新选证 */
  evidence_stale?: boolean
  tags: string[]
  order_idx: number
  created_at: string | null
  updated_at: string | null
}

export interface ClaimInput {
  paper_id: string
  text: string
  section?: string
  evidence: Array<{ type: string; id: string }>
  tags?: string[]
  status?: 'draft' | 'approved'
}

/** claim 的历史版本（2026-11-15 小修）：每次改内容前留一份。 */
export interface ClaimRevision {
  revision_id: string
  claim_id: string
  text: string
  section: string
  status: 'draft' | 'approved' | string
  source: string
  evidence: ClaimEvidence[]
  tags: string[]
  order_idx: number
  paper_version_id: string | null
  reason: 'edited' | 'restored' | string
  edited_by: string | null
  created_at: string | null
}

export interface MatrixCell {
  paper_id: string
  text: string
}

export interface MatrixRow {
  key: string
  label: string
  values: MatrixCell[]
}

export interface MatrixPaper {
  paper_id: string
  title: string
  cite_key: string | null
  year: number | null
  venue: string | null
  authors: string[]
  has_version: boolean
  summary_id: string | null
  figure_count: number
  table_count: number
  claim_count: number
}

export interface ComparisonMatrix {
  paper_ids: string[]
  papers: MatrixPaper[]
  rows: MatrixRow[]
  note: string
}

/** 矩阵可选维度（GET /api/matrix/dimensions）。 */
export interface MatrixDimension {
  key: string
  label: string
  description: string
  default: boolean
}

export interface MatrixDimensions {
  dimensions: MatrixDimension[]
  note: string
}

export interface WritingExport {
  format: 'latex' | 'bibtex' | 'markdown' | string
  filename: string
  content: string
  extra_files: Record<string, string>
  claim_count: number
  paper_count: number
}

/** Word/PDF 导出能力（M8）：缺 pandoc/typst 时前端置灰并提示。 */
export interface DocExportCapabilities {
  docx: boolean
  pdf: boolean
  pandoc: boolean
  typst: boolean
  note: string
}

// ---------------------------------------------------------------------------
// 元数据补全（M8，Crossref / arXiv）
// ---------------------------------------------------------------------------
export interface MetadataFieldDiff {
  field: string
  label: string
  current: unknown
  current_text: string
  suggested: unknown
  suggested_text: string
  /** 当前为空 = 默认勾选；conflict = 当前已有不同值，覆盖要 overwrite */
  empty: boolean
  conflict: boolean
}

export interface MetadataCandidate {
  source: string
  doi: string | null
  arxiv_id: string | null
  title: string
  year: number | null
  venue: string | null
  authors: string
  score: number | null
}

export interface MetadataLookup {
  paper_id: string
  mode: string
  source: string
  identifier: string
  fields: MetadataFieldDiff[]
  candidates: MetadataCandidate[]
  warnings: string[]
}

// ---------------------------------------------------------------------------
// 写作大纲 + 引用/图表交叉检查（M8）
// ---------------------------------------------------------------------------
export interface OutlineEvidence {
  type: string
  id: string
  label: string
  page: number | null
  snippet: string
}

export interface OutlineIssue {
  kind: 'figure' | 'table' | 'citation' | string
  severity: string
  message: string
}

export interface OutlineClaim {
  claim_id: string
  text: string
  status: string
  paper_id: string
  paper_title: string | null
  cite_key: string | null
  order_idx: number
  evidence: OutlineEvidence[]
  figure_labels: string[]
  table_labels: string[]
  issues: OutlineIssue[]
  notes: string[]
}

export interface OutlineSection {
  section: string
  canonical: string
  claim_count: number
  claims: OutlineClaim[]
}

export interface WritingOutline {
  sections: OutlineSection[]
  total_claims: number
  issue_count: number
  section_order: string[]
  markdown: string
  filename: string
  warnings: string[]
}

export interface CitationItem {
  key: string
  status: 'resolved' | 'unknown' | string
  paper_id: string | null
  title: string | null
  cite_key: string | null
  suggestion: string | null
}

export interface CitationCheck {
  total: number
  resolved: number
  unknown: number
  citations: CitationItem[]
  message: string
}

// ---------------------------------------------------------------------------
// 学位论文 LaTeX 包（M8，2026-11-15）
// ---------------------------------------------------------------------------
export interface ThesisExport {
  format: string
  filename: string
  content: string
  extra_files: Record<string, string>
  claim_count: number
  paper_count: number
  chapter_count: number
  warnings: string[]
}

export interface ThesisLabelCheck {
  key: string
  status: 'resolved' | 'unknown' | string
  kind: 'figure' | 'table' | string
  object_id: string | null
  label: string | null
  suggestion: string | null
}

export interface ThesisCheck {
  total: number
  resolved: number
  unknown: number
  citations: CitationItem[]
  figures: ThesisLabelCheck[]
  tables: ThesisLabelCheck[]
  missing: string[]
  unused: string[]
  warnings: string[]
  message: string
}

/** 论文里的表格（GET /api/papers/{id}/tables）。 */
export interface PaperTable {
  table_id: string
  version_id: string
  page: number
  label_raw: string | null
  label_norm: string | null
  caption: string
  quality: string
  html: string | null
}

/** 表格 → LaTeX（M9.3a）：tabular 片段 + 可粘贴浮动体 + warnings。 */
export interface TableLatex {
  table_id: string
  paper_id: string
  caption: string
  label: string
  label_key: string
  tabular: string
  code: string
  columns: number
  rows: number
  warnings: string[]
  wrapped: boolean
  column_spec: string
}

// ---------------------------------------------------------------------------
// 参考文献条目（2026-11-13）
// ---------------------------------------------------------------------------
export interface ReferenceStyle {
  key: string
  label: string
  language: 'zh' | 'en' | 'neutral' | string
  kind: 'text' | 'bibtex' | 'ris' | string
  description: string
  sample: string
  // 是否有官方 CSL 样式（有就能切到 CSL 引擎）
  csl_available: boolean
}

export interface ReferenceStyles {
  styles: ReferenceStyle[]
  note: string
  csl_engine_available: boolean
  csl_note: string
}

export interface ReferenceItem {
  paper_id: string
  cite_key: string | null
  title: string
  entry: string
  warnings: string[]
}

export interface ReferenceFormat {
  style: string
  label: string
  paper_count: number
  content: string
  items: ReferenceItem[]
  engine: 'template' | 'csl' | string
  note: string
}

export interface CslJsonExport {
  count: number
  items: Record<string, unknown>[]
}

// ---------------------------------------------------------------------------
// 团队共享（2026-11-15）：账号 / 登录
// ---------------------------------------------------------------------------
export interface AuthUser {
  id: string
  username: string
  display_name: string
  role: 'admin' | 'member' | string
  is_active: boolean
  created_at: string | null
  last_login_at: string | null
}

export interface AuthStatus {
  auth_enabled: boolean
  initialized: boolean
  user: AuthUser | null
}

// ---------------------------------------------------------------------------
// 团队共享（2026-11-15 第二步）：工作组 / 共享
// ---------------------------------------------------------------------------
export interface Workgroup {
  id: string
  name: string
  description: string | null
  created_by: string | null
  created_by_name: string | null
  /** 我在这个组里的角色；管理员没加入也能看到（role=admin） */
  role: 'owner' | 'member' | 'admin' | string
  member_count: number
  paper_count: number
  created_at: string | null
}

export interface WorkgroupMember {
  user_id: string
  username: string
  display_name: string
  role: 'owner' | 'member' | string
  joined_at: string | null
}

export interface WorkgroupDetail extends Workgroup {
  members: WorkgroupMember[]
}

export interface PaperShare {
  paper_id: string
  workgroup_id: string
  workgroup_name: string
  shared_by: string | null
  shared_by_name: string | null
  shared_at: string | null
}

export interface PaperShareResult {
  share: PaperShare
  created: boolean
}
