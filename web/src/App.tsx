// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, ReactElement } from 'react'
import {
  aiReviewFigure,
  attachPaperPdf,
  copyText,
  createCollection,
  decideProposal,
  deleteFigure,
  fetchCollections,
  fetchLabelStats,
  fetchLlmSettings,
  fetchProposalStats,
  getAuthStatus,
  getFigure,
  getJob,
  getPaper,
  getWorkgroup,
  groupFigure,
  importZotero,
  importZoteroJson,
  listFigureProposals,
  listPapers,
  listWorkgroups,
  paperFigures,
  patchPaper,
  reviewFigure,
  searchFigures,
  updateFigureLabel,
  uploadPaper,
} from './api'
import PdfViewer from './PdfViewer'
import ChapterSummary from './ChapterSummary'
import KnowledgeManager from './KnowledgeManager'
import LlmSettings from './LlmSettings'
import PaperOverview from './PaperOverview'
import PaperReader from './PaperReader'
import PaperTables from './PaperTables'
import Proposals from './Proposals'
import UserMenu from './UserMenu'
import Writing from './Writing'
import { ShareMenu, WorkgroupPanel, WorkspaceSwitcher } from './Team'
import type {
  AuthUser,
  Collection,
  Figure,
  FigureProposal,
  FigureSearch,
  Job,
  LabelStats,
  LlmSettings as LlmSettingsState,
  Paper,
  PaperTable,
  Workgroup,
  WorkgroupDetail,
} from './types'
import { MODE_LABEL } from './types'

const EXAMPLES = ['FIG. 3', 'Figure 2', 'Wang 2019', 'simulated shock']

/** 顶栏那条上传/解析进度条的状态。手写一份而不是直接用 Job：上传阶段还没有 job id。 */
type UploadState = {
  id: string | null
  name: string
  status: 'uploading' | 'queued' | 'running' | 'done' | 'failed'
  progress: number
  error: string | null
  deduplicated: boolean
  /** 正在跑的是按需升级后的增强档（Docling），不是默认的 pdfium */
  escalated: boolean
  reason: string | null
}

/** 查询写进 URL：可分享、可刷新恢复、可被浏览器前进后退带回来。 */
function readQueryFromUrl(): string {
  return new URLSearchParams(window.location.search).get('q') ?? ''
}

/**
 * localStorage 的安全读写：隐私模式 / 站点禁用 storage 时读写都会抛异常，
 * 不能让一个 UI 偏好把整个应用打白（批 D10）。存不进去就退回默认值。
 */
function readLocal(key: string): string | null {
  try {
    return window.localStorage.getItem(key)
  } catch {
    return null
  }
}

function writeLocal(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    /* 偏好存不进去不影响主流程 */
  }
}

export default function App() {
  const [papers, setPapers] = useState<Paper[]>([])
  const [query, setQuery] = useState<string>(() => readQueryFromUrl())
  const [result, setResult] = useState<FigureSearch | null>(null)
  const [activePaper, setActivePaper] = useState<Paper | null>(null)
  const [paperFigs, setPaperFigs] = useState<Figure[]>([])
  // 文献主区的子页签（图表 / 表格 / AI 速览 / 章节摘要 / 正文速览），默认还是图表，打开文献时重置
  const [paperTab, setPaperTab] = useState<
    'figures' | 'tables' | 'overview' | 'summaries' | 'reader'
  >('figures')
  const [detail, setDetail] = useState<Figure | null>(null)
  const [viewer, setViewer] = useState<Figure | null>(null)
  // 表格「原文」入口：只读 PDF 查看器，跳到表格所在页（不画框）
  const [tableViewer, setTableViewer] = useState<{
    paper: Paper
    table: PaperTable
  } | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // 图号校核（阶段 3）：待确认计数 + 「待确认」筛选
  const [labelStats, setLabelStats] = useState<LabelStats | null>(null)
  const [labelFilter, setLabelFilter] = useState<'review' | null>(null)
  // 系列图集：点组卡片打开「系列浮层」——背景虚化，只有子图可交互；收起后背景恢复
  const [seriesKey, setSeriesKey] = useState<string | null>(null)
  // 纯「框选加图」查看器（不挂在某张已有图上，用于给解析器漏掉的图补录）
  const [addTarget, setAddTarget] = useState<{ paperId: string; page: number; title: string } | null>(
    null,
  )
  // 模型 API 设置（BYO key）：顶栏齿轮打开浮层
  const [settingsOpen, setSettingsOpen] = useState(false)
  // M3：AI 图纠错的待审建议数量 + 建议抽屉
  const [pendingProposals, setPendingProposals] = useState(0)
  const [proposalsOpen, setProposalsOpen] = useState(false)
  // M5：写作台（claim 卡片 / 对比矩阵 / LaTeX·BibTeX 导出）
  const [writingOpen, setWritingOpen] = useState(false)
  // 从表格「写进卡片」进入写作台时预选的文献（普通入口为 null）
  const [writingPaperId, setWritingPaperId] = useState<string | null>(null)
  // AI 逐张修图的进度（一次最多 8 张，避免一次请求太久/太贵）
  const [aiBatch, setAiBatch] = useState<{ done: number; total: number; failed: number } | null>(
    null,
  )
  // 当前模型配置状态：没配置时 AI 功能禁用，配好并测试通过后顶栏亮绿点
  const [llmSettings, setLlmSettings] = useState<LlmSettingsState | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  // 知识库（M1）：目录树 + 选中集合 + Zotero 只读导入
  const [collections, setCollections] = useState<Collection[]>([])
  const [collectionId, setCollectionId] = useState<string | null>(null)
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [kbBusy, setKbBusy] = useState(false)
  // 2026-11-12 工作台改版：侧栏可收起 / 收藏夹 / 知识库操作台 / 宣纸纹理开关
  const [kbCollapsed, setKbCollapsed] = useState(
    () => readLocal('litbench.kbCollapsed') === '1',
  )
  const [papersCollapsed, setPapersCollapsed] = useState(
    () => readLocal('litbench.papersCollapsed') === '1',
  )
  const [favoriteOnly, setFavoriteOnly] = useState(false)
  // 非文献视图的浏览单位：文献卡片优先，图表作为可切换的第二视图（2026-11-12 交互修订）
  const [browseMode, setBrowseMode] = useState<'papers' | 'figures'>('papers')
  const [kbManagerOpen, setKbManagerOpen] = useState(false)
  const [textureOn, setTextureOn] = useState(
    () => readLocal('litbench.texture') !== '0',
  )
  // 主题：auto（跟随系统）/ light（宣纸书卷）/ dark（古墨简卷）；手动选择存 localStorage
  const [themeMode, setThemeMode] = useState<'auto' | 'light' | 'dark'>(() => {
    const saved = readLocal('litbench.theme')
    return saved === 'light' || saved === 'dark' ? saved : 'auto'
  })
  const [favoriteCount, setFavoriteCount] = useState(0)
  // 顶栏总数（不受左侧集合过滤影响；集合视图只过滤文献列表）
  const [totals, setTotals] = useState({ papers: 0, figures: 0, withFigures: 0 })

  // 团队共享（M7 第二步）：当前账号 + 我的工作组 + 「个人库 / 工作组」范围
  const [authUser, setAuthUser] = useState<AuthUser | null>(null)
  const [workgroups, setWorkgroups] = useState<Workgroup[]>([])
  const [workgroupsLoaded, setWorkgroupsLoaded] = useState(false)
  const [workspace, setWorkspace] = useState<string>(
    () => readLocal('litbench.workspace') ?? 'personal',
  )
  const [teamDetail, setTeamDetail] = useState<WorkgroupDetail | null>(null)
  const [wgPanelOpen, setWgPanelOpen] = useState(false)
  const [wgPanelGroup, setWgPanelGroup] = useState<string | null>(null)
  // 认证关闭时永远按个人库处理（旧单机行为）
  const activeWorkspace = authUser && workspace !== 'personal' ? workspace : null
  const workspaceGroup = activeWorkspace
    ? (workgroups.find((group) => group.id === activeWorkspace) ?? null)
    : null

  // 上传 → 解析：job 在后台跑，这里只轮询快照（不引 SSE，少一条长连接要维护）
  const [upload, setUpload] = useState<UploadState | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const activePaperRef = useRef<Paper | null>(null)
  // 详情面板当前打开的是哪张图（静默刷新时按它决定要不要回填详情）
  const detailRef = useRef<Figure | null>(null)
  // 打开文献 / 图表检索的请求序号：慢请求返回时如果已经切走就丢弃（批 D1/D14）
  const paperOpenSeq = useRef(0)
  const searchSeq = useRef(0)
  // 图表检索的「加载更多」进行中
  const [searchingMore, setSearchingMore] = useState(false)

  // 深链：?fig=<figure_id> 直接打开原文查看器（可分享到某个具体图）
  const pendingFig = useRef<string | null>(new URLSearchParams(window.location.search).get('fig'))

  useEffect(() => {
    // 深链没处理完之前不要把 ?fig= 冲掉
    if (pendingFig.current && !viewer) return
    const params = new URLSearchParams()
    if (query.trim()) params.set('q', query.trim())
    if (viewer) params.set('fig', viewer.figure_id)
    const search = params.toString()
    window.history.replaceState(null, '', search ? `?${search}` : window.location.pathname)
  }, [query, viewer])

  useEffect(() => {
    const figId = pendingFig.current
    if (!figId) return
    const wantSource = new URLSearchParams(window.location.search).get('src') === '1'
    void (async () => {
      try {
        const fig = await getFigure(figId)
        setDetail(fig)
        if (wantSource) setViewer(fig)
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err))
      } finally {
        pendingFig.current = null
      }
    })()
  }, [])

  const loadPapers = useCallback(async () => {
    try {
      setPapers(
        await listPapers(favoriteOnly ? null : collectionId, {
          favorite: favoriteOnly,
          workgroupId: activeWorkspace,
        }),
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [collectionId, favoriteOnly, activeWorkspace])

  const loadCollections = useCallback(async () => {
    try {
      setCollections(await fetchCollections())
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [])

  const loadTotals = useCallback(async () => {
    try {
      const all = await listPapers(null, { workgroupId: activeWorkspace })
      setTotals({
        papers: all.length,
        figures: all.reduce((sum, paper) => sum + paper.figure_count, 0),
        withFigures: all.filter((paper) => paper.figure_count > 0).length,
      })
    } catch {
      /* 顶栏统计失败不影响主流程 */
    }
  }, [activeWorkspace])

  const loadWorkgroups = useCallback(async () => {
    try {
      const rows = await listWorkgroups()
      setWorkgroups(rows)
      setWorkspace((prev) =>
        prev !== 'personal' && !rows.some((group) => group.id === prev) ? 'personal' : prev,
      )
    } catch {
      setWorkgroups([])
    } finally {
      setWorkgroupsLoaded(true)
    }
  }, [])

  // 认证状态只决定「有没有工作组功能」；认证关闭时不请求 /workgroups（会 503）
  useEffect(() => {
    void getAuthStatus()
      .then((status) => {
        if (status.auth_enabled && status.user) {
          setAuthUser(status.user)
          void loadWorkgroups()
        }
      })
      .catch(() => setAuthUser(null))
  }, [loadWorkgroups])

  useEffect(() => {
    writeLocal('litbench.workspace', workspace)
  }, [workspace])

  // 工作区被解散/退出（或登录失效）时回到个人库，避免停在一个不存在的组
  useEffect(() => {
    if (!workgroupsLoaded) return
    if (workspace === 'personal') return
    if (!authUser) {
      setWorkspace('personal')
      return
    }
    if (!workgroups.some((group) => group.id === workspace)) setWorkspace('personal')
  }, [workgroupsLoaded, workgroups, workspace, authUser])

  // 团队库左栏：成员名单只在进入工作组时拉一次
  useEffect(() => {
    if (!activeWorkspace) {
      setTeamDetail(null)
      return
    }
    let alive = true
    void getWorkgroup(activeWorkspace)
      .then((detail) => {
        if (alive) setTeamDetail(detail)
      })
      .catch(() => {
        if (alive) setTeamDetail(null)
      })
    return () => {
      alive = false
    }
  }, [activeWorkspace, workgroups])

  // 打开工作组管理浮层时刷新列表，避免「共享数」停在登录时的旧值
  useEffect(() => {
    if (wgPanelOpen) void loadWorkgroups()
  }, [wgPanelOpen, loadWorkgroups])

  /** 切换「个人库 / 工作组」：清掉只在个人库有意义的过滤，重新拉列表。 */
  const selectWorkspace = useCallback((next: string) => {
    setWorkspace(next)
    setActivePaper(null)
    setDetail(null)
    setCollectionId(null)
    setFavoriteOnly(false)
    setLabelFilter(null)
    setQuery('')
    setKbManagerOpen(false)
  }, [])

  useEffect(() => {
    void loadPapers()
  }, [loadPapers])

  useEffect(() => {
    void loadCollections()
  }, [loadCollections])

  useEffect(() => {
    void loadTotals()
  }, [loadTotals])

  // 工作台改版：侧栏收起状态 / 宣纸纹理开关都记在 localStorage，刷新不丢
  useEffect(() => {
    writeLocal('litbench.kbCollapsed', kbCollapsed ? '1' : '0')
  }, [kbCollapsed])
  useEffect(() => {
    writeLocal('litbench.papersCollapsed', papersCollapsed ? '1' : '0')
  }, [papersCollapsed])
  useEffect(() => {
    writeLocal('litbench.texture', textureOn ? '1' : '0')
    document.documentElement.classList.toggle('texture-on', textureOn)
  }, [textureOn])

  // 主题三态：auto 不设属性（跟随系统）；light/dark 用 data-theme 覆盖媒体查询
  useEffect(() => {
    writeLocal('litbench.theme', themeMode)
    if (themeMode === 'auto') document.documentElement.removeAttribute('data-theme')
    else document.documentElement.dataset.theme = themeMode
  }, [themeMode])

  // 收藏夹计数（侧栏入口用）：不在收藏夹视图时也显示总数。
  // 收藏数一律以服务端为准，不在本地 +1/-1 猜（收藏夹里取消收藏时列表会少一行）。
  const loadFavoriteCount = useCallback(async () => {
    try {
      setFavoriteCount((await listPapers(null, { favorite: true })).length)
    } catch {
      setFavoriteCount(0)
    }
  }, [])

  useEffect(() => {
    void loadFavoriteCount()
  }, [loadFavoriteCount, refreshKey, papers.length])

  // 导入/解析完成后刷新知识库树和顶栏总数
  useEffect(() => {
    if (refreshKey > 0) {
      void loadCollections()
      void loadTotals()
    }
  }, [refreshKey, loadCollections, loadTotals])

  // 待确认计数：解析完成 / 检索变化 / 校核动作后刷新
  useEffect(() => {
    void fetchLabelStats()
      .then(setLabelStats)
      .catch(() => setLabelStats(null))
  }, [refreshKey, query])

  // AI 建议徽标：用 /proposals/stats 的 pending 计数（队列三页签共用这一份统计）
  useEffect(() => {
    void fetchProposalStats()
      .then((data) => setPendingProposals(data.pending))
      .catch(() => setPendingProposals(0))
  }, [refreshKey, proposalsOpen])

  useEffect(() => {
    activePaperRef.current = activePaper
  }, [activePaper])

  useEffect(() => {
    detailRef.current = detail
  }, [detail])

  /** 上传后立刻拿到 job id，解析在后台；进度靠下面的轮询。 */
  const startUpload = useCallback(async (file: File | null | undefined) => {
    if (!file) return
    setError(null)
    const base = { name: file.name, deduplicated: false, escalated: false, reason: null }
    setUpload({ ...base, id: null, status: 'uploading', progress: 0, error: null })
    try {
      const created = await uploadPaper(file)
      setUpload({
        ...base,
        id: created.job_id,
        status: created.job_id ? 'queued' : 'done',
        progress: 0,
        error: null,
        deduplicated: created.deduplicated,
      })
    } catch (err) {
      setUpload({
        ...base,
        id: null,
        status: 'failed',
        progress: 0,
        error: err instanceof Error ? err.message : String(err),
      })
    }
  }, [])

  /** 给「只有元数据」的文献补传 PDF：建版本 + 排队解析，复用上传进度条。 */
  const startAttachUpload = useCallback(
    async (file: File | null | undefined) => {
      const paper = activePaperRef.current
      if (!file || !paper) return
      setError(null)
      const base = { name: file.name, deduplicated: false, escalated: false, reason: null }
      setUpload({ ...base, id: null, status: 'uploading', progress: 0, error: null })
      try {
        const created = await attachPaperPdf(paper.id, file)
        setUpload({
          ...base,
          id: created.job_id,
          status: created.job_id ? 'queued' : 'done',
          progress: 0,
          error: null,
          deduplicated: created.deduplicated,
        })
      } catch (err) {
        setUpload({
          ...base,
          id: null,
          status: 'failed',
          progress: 0,
          error: err instanceof Error ? err.message : String(err),
        })
      }
    },
    [],
  )

  // 轮询 job：done 时刷新文献列表 + 当前视图；failed 时把服务端的错误原样显示出来。
  // 轻量档解析完如果按需升级了，就**接着盯增强档那个 job**——否则用户会看到
  // 「解析完成」，然后图在几分钟后自己变一遍。
  const jobId = upload?.id ?? null
  useEffect(() => {
    if (!jobId) return
    let alive = true
    let handle = 0

    const settle = (next: Pick<UploadState, 'status' | 'progress' | 'error'>) => {
      if (!alive) return
      setUpload((prev) => (prev && prev.id === jobId ? { ...prev, ...next } : prev))
      if (next.status === 'done' || next.status === 'failed') {
        window.clearInterval(handle)
        if (next.status === 'done') {
          void loadPapers()
          setRefreshKey((k) => k + 1)
        }
      }
    }

    const tick = async () => {
      try {
        const snapshot: Job = await getJob(jobId)
        if (!alive) return

        // 快速档完成且已排增强档：先刷新（pdfium 的结果立刻可见），再切去盯增强 job
        if (snapshot.status === 'done' && snapshot.escalated_to) {
          window.clearInterval(handle)
          void loadPapers()
          setRefreshKey((k) => k + 1)
          setUpload((prev) =>
            prev && prev.id === jobId
              ? {
                  ...prev,
                  id: snapshot.escalated_to,
                  status: 'queued',
                  progress: 0,
                  escalated: true,
                  reason: snapshot.escalate_reason,
                }
              : prev,
          )
          return
        }

        settle({
          status: snapshot.status as UploadState['status'],
          progress: snapshot.progress,
          error: snapshot.error,
        })
      } catch (err) {
        settle({
          status: 'failed',
          progress: 0,
          error: err instanceof Error ? err.message : String(err),
        })
      }
    }

    void tick()
    handle = window.setInterval(() => void tick(), 800)
    return () => {
      alive = false
      window.clearInterval(handle)
    }
  }, [jobId, loadPapers])

  // 搜索框防抖：300ms 内连续输入只打一次接口
  const timer = useRef<number | undefined>(undefined)
  useEffect(() => {
    if (activePaper) return
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(async () => {
      const requestId = ++searchSeq.current
      setLoading(true)
      setError(null)
      try {
        const data = await searchFigures(query, 60, labelFilter, {
          favorite: favoriteOnly,
          collectionId,
          workgroupId: activeWorkspace,
          offset: 0,
        })
        // 慢请求返回时如果查询/筛选已经变了，丢弃旧结果（不能覆盖新结果）
        if (requestId !== searchSeq.current) return
        setResult(data)
      } catch (err) {
        if (requestId !== searchSeq.current) return
        setError(err instanceof Error ? err.message : String(err))
      } finally {
        if (requestId === searchSeq.current) setLoading(false)
      }
    }, 300)
    return () => window.clearTimeout(timer.current)
  }, [query, activePaper, refreshKey, labelFilter, favoriteOnly, collectionId, activeWorkspace])

  /**
   * 图表检索「加载更多」（批 E 契约）：GET /api/figures 支持 offset，limit 固定 60，
   * total 是符合条件的真实总数。改 query / 筛选后上面的 effect 会重新从 offset 0 拉，
   * 因此这里不需要单独维护可复位的 offset 状态（以当前 items 长度为准）。
   */
  const loadMoreFigures = useCallback(async () => {
    if (searchingMore) return
    const current = result
    if (!current || current.items.length >= current.total) return
    const requestId = searchSeq.current
    setSearchingMore(true)
    try {
      const next = await searchFigures(query, 60, labelFilter, {
        favorite: favoriteOnly,
        collectionId,
        workgroupId: activeWorkspace,
        offset: current.items.length,
      })
      if (requestId !== searchSeq.current) return
      setResult((prev) => {
        if (!prev) return next
        const seen = new Set(prev.items.map((fig) => fig.figure_id))
        return {
          ...next,
          items: [...prev.items, ...next.items.filter((fig) => !seen.has(fig.figure_id))],
        }
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSearchingMore(false)
    }
  }, [searchingMore, result, query, labelFilter, favoriteOnly, collectionId, activeWorkspace])

  /**
   * 打开一篇文献。批 D1：先清空旧的图列表；用自增请求号区分先后，
   * 打开 A 后马上切到 B 时，A 的慢请求（包括错误）返回后直接丢弃。
   */
  const openPaper = useCallback(async (paper: Paper) => {
    const requestId = ++paperOpenSeq.current
    const switching = activePaperRef.current?.id !== paper.id
    setActivePaper(paper)
    activePaperRef.current = paper
    if (switching) setPaperFigs([])
    setDetail(null)
    setPaperTab('figures')
    setLoading(true)
    setError(null)
    try {
      const figs = await paperFigures(paper.id)
      if (requestId !== paperOpenSeq.current) return
      setPaperFigs(figs)
    } catch (err) {
      // 旧请求的错误不要弹到新文献上
      if (requestId !== paperOpenSeq.current) return
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      if (requestId === paperOpenSeq.current) setLoading(false)
    }
  }, [])

  /**
   * 解析完成 / 收藏 / 增图等外部变化后，**静默刷新**当前文献（批 D2）。
   *
   * 旧实现复用 openPaper()，而它内部 setDetail(null) + setPaperTab('figures')，
   * 导致保存图号、采纳建议后详情面板被关、页签被踢回「图表」。
   * 现在只重新拉 getPaper + paperFigures，并（同一篇时）回填当前详情，不动打开状态。
   */
  useEffect(() => {
    if (refreshKey === 0) return
    const current = activePaperRef.current
    if (!current) return
    let cancelled = false
    void (async () => {
      try {
        const [freshPaper, freshFigs] = await Promise.all([
          getPaper(current.id),
          paperFigures(current.id),
        ])
        // await 期间可能已经切到别的文献：旧结果整个丢弃
        if (cancelled || activePaperRef.current?.id !== current.id) return
        setActivePaper(freshPaper)
        activePaperRef.current = freshPaper
        setPaperFigs(freshFigs)
        const openDetail = detailRef.current
        if (!openDetail || openDetail.paper_id !== current.id) return
        try {
          const freshDetail = await getFigure(openDetail.figure_id)
          if (cancelled || activePaperRef.current?.id !== current.id) return
          if (detailRef.current?.figure_id !== openDetail.figure_id) return
          setDetail(freshDetail)
        } catch {
          /* 单张详情刷新失败保留旧数据，不打扰用户 */
        }
      } catch {
        /* 静默刷新失败不清空界面：旧数据还能用，下一次动作再刷 */
      }
    })()
    return () => {
      cancelled = true
    }
  }, [refreshKey])

  // 模型配置状态：启动拉一次；设置浮层里保存/测试/清除会通过 onChanged 同步
  useEffect(() => {
    void fetchLlmSettings()
      .then(setLlmSettings)
      .catch(() => setLlmSettings(null))
  }, [])

  const figures = activePaper ? paperFigs : (result?.items ?? [])
  const mode = activePaper ? null : result?.mode ?? null
  /** 配了模型才开放 AI 功能（章节摘要）；没配就禁用并引导去设置 */
  const aiReady = Boolean(llmSettings?.configured)
  /** 摘要还需要 PDF/解析版本；Zotero 纯元数据条目要先把 PDF 补上 */
  const paperHasPdf = activePaper ? activePaper.has_version !== false : false
  const summariesReady = aiReady && paperHasPdf
  /** 非文献视图、无查询/待确认筛选时，可以在「文献 / 图表」两种浏览单位间切换 */
  const browseFiltersActive = !activePaper && !query.trim() && !labelFilter
  const showPapersView = browseFiltersActive && browseMode === 'papers'
  const llmStatus = useMemo(() => {
    if (!llmSettings) return null
    if (!llmSettings.configured) return { text: '模型未配置', cls: 'none' }
    if (llmSettings.source === 'env') return { text: '已配置', cls: 'env' }
    return llmSettings.last_test_ok
      ? { text: '已连接', cls: 'ok' }
      : { text: '未连接', cls: 'bad' }
  }, [llmSettings])

  // M3：哪些图值得先让 AI 看一眼（无编号 / 低置信 / 双 OCR 冲突）
  const aiReviewQueue = useMemo(
    () =>
      figures.filter(
        (fig) =>
          !fig.label_raw ||
          fig.label_source === 'ocr_cross_conflict' ||
          fig.label_source === 'ocr_cross_unresolved' ||
          (typeof fig.label_confidence === 'number' && fig.label_confidence < 0.6),
      ),
    [figures],
  )

  // 逐张跑 AI 修图：上限 8 张（一次请求只做一张，前端串行，进度可见；失败不拖累其它张）
  const runAiBatch = useCallback(
    async (targets: Figure[]) => {
      if (!aiReady) {
        setSettingsOpen(true)
        return
      }
      if (aiBatch) return
      const batch = targets.slice(0, 8)
      if (batch.length === 0) {
        setToast('没有需要 AI 复核的图（编号都够可信）')
        return
      }
      setAiBatch({ done: 0, total: batch.length, failed: 0 })
      let ok = 0
      let failed = 0
      let firstError: string | null = null
      for (let index = 0; index < batch.length; index += 1) {
        try {
          await aiReviewFigure(batch[index].figure_id)
          ok += 1
        } catch (err) {
          failed += 1
          if (!firstError) firstError = err instanceof Error ? err.message : String(err)
        }
        setAiBatch({ done: index + 1, total: batch.length, failed })
      }
      setAiBatch(null)
      setToast(
        failed === 0
          ? `AI 复核完成：生成 ${ok} 条建议，点右上角「AI 建议」查看`
          : `AI 复核：成功 ${ok} 张、失败 ${failed} 张${firstError ? `（${firstError}）` : ''}`,
      )
      setRefreshKey((k) => k + 1)
    },
    [aiReady, aiBatch],
  )

  // 系列子图默认折叠成一张组卡片；点组卡片打开「系列浮层」，而不是原地展开。
  const rows = useMemo(() => {
    const out: Array<{ fig: Figure; members?: Figure[] }> = []
    const seen = new Set<string>()
    for (const fig of figures) {
      if (!fig.group_key) {
        out.push({ fig })
        continue
      }
      if (seen.has(fig.group_key)) continue
      seen.add(fig.group_key)
      const members = figures.filter((x) => x.group_key === fig.group_key)
      const parent =
        members.find((m) => m.label_source === 'text' || m.label_source === 'ocr_consensus') ??
        members.find((m) => m.label_raw) ??
        fig
      out.push({ fig: parent, members })
    }
    return out
  }, [figures])

  const seriesMembers = useMemo(
    () => (seriesKey ? figures.filter((f) => f.group_key === seriesKey) : []),
    [figures, seriesKey],
  )

  // toast 自动消失
  useEffect(() => {
    if (!toast) return
    const handle = window.setTimeout(() => setToast(null), 3500)
    return () => window.clearTimeout(handle)
  }, [toast])

  const currentCollection = collections.find((c) => c.id === collectionId) ?? null

  const selectCollection = useCallback((id: string | null) => {
    setCollectionId(id)
    setFavoriteOnly(false)
    setActivePaper(null)
    setDetail(null)
  }, [])

  /** 收藏夹是虚拟视图：不按集合过滤，只按 favorite=true 拉文献。 */
  const selectFavorites = useCallback(() => {
    setCollectionId(null)
    setFavoriteOnly(true)
    setActivePaper(null)
    setDetail(null)
    setKbManagerOpen(false)
  }, [])

  /** 侧栏星标：直接在列表里收藏/取消，不打断当前阅读。 */
  const toggleFavorite = useCallback(
    async (paper: Paper) => {
      try {
        const updated = await patchPaper(paper.id, { favorite: !paper.favorite })
        // 收藏夹是虚拟视图：在这里取消收藏要立刻从列表里移除；其它视图只更新星标
        setPapers((prev) => {
          if (favoriteOnly && !updated.favorite) return prev.filter((p) => p.id !== updated.id)
          return prev.map((p) => (p.id === paper.id ? updated : p))
        })
        if (activePaperRef.current?.id === paper.id) {
          setActivePaper((prev) => (prev ? { ...prev, ...updated } : prev))
        }
        setToast(updated.favorite ? '已加入收藏夹' : '已移出收藏夹')
        // 计数以服务端为准（不在本地 +1/-1 猜，避免与收藏夹列表不一致）
        void loadFavoriteCount()
      } catch (err) {
        setToast(err instanceof Error ? `收藏失败：${err.message}` : '收藏失败')
      }
    },
    [favoriteOnly, loadFavoriteCount],
  )

  const toggleCollapse = useCallback((id: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  const handleCreateCollection = async () => {
    const name = window.prompt('新集合名称')
    if (!name?.trim()) return
    try {
      await createCollection(name.trim(), collectionId)
      await loadCollections()
      setToast('已新建集合')
    } catch (err) {
      setToast(err instanceof Error ? `新建失败：${err.message}` : '新建失败')
    }
  }

  const runImport = async (
    fn: () => Promise<{ stats: Record<string, number | string> }>,
  ) => {
    setKbBusy(true)
    try {
      const run = await fn()
      await Promise.all([loadCollections(), loadPapers()])
      const stats = run.stats
      const pdfs = Number(stats.pdfs_imported ?? 0)
      const missing = Number(stats.pdf_files_missing ?? 0)
      const parts = [
        `新文献 ${stats.papers_created ?? 0}`,
        `更新 ${stats.papers_updated ?? 0}`,
        `新集合 ${stats.collections_created ?? 0}`,
      ]
      if (pdfs > 0) parts.push(`导入 PDF ${pdfs}`)
      if (missing > 0) parts.push(`${missing} 个附件文件不在本机`)
      setToast(`导入完成：${parts.join('、')}`)
    } catch (err) {
      setToast(err instanceof Error ? `导入失败：${err.message}` : '导入失败')
    } finally {
      setKbBusy(false)
    }
  }

  const handleImportZotero = () => void runImport(importZotero)
  const handleImportJson = (file: File | null) => {
    if (file) void runImport(() => importZoteroJson(file))
  }

  // 平铺的 collections 按 parent_id 组装成树；缩进由 depth 决定
  const collectionTotal = (id: string): number => {
    const direct = collections.find((c) => c.id === id)?.paper_count ?? 0
    return collections
      .filter((c) => c.parent_id === id)
      .reduce((sum, child) => sum + collectionTotal(child.id), direct)
  }

  const renderTree = (parentId: string | null, depth: number): ReactElement[] => {
    const children = collections
      .filter((c) => c.parent_id === parentId)
      .sort((a, b) => a.sort_order - b.sort_order || a.name.localeCompare(b.name))
    return children.flatMap((col) => {
      const hasKids = collections.some((c) => c.parent_id === col.id)
      const open = !collapsed.has(col.id)
      const row = (
        <li key={col.id}>
          <button
            className={`tree-node ${collectionId === col.id ? 'active' : ''}`}
            style={{ paddingLeft: 10 + depth * 14 }}
            onClick={() => selectCollection(col.id)}
            title={`${col.name} · ${collectionTotal(col.id)} 篇${col.source === 'zotero' ? '（Zotero）' : ''}`}
          >
            {hasKids ? (
              <span
                className="tree-toggle"
                onClick={(e) => {
                  e.preventDefault()
                  e.stopPropagation()
                  toggleCollapse(col.id)
                }}
              >
                {open ? '▾' : '▸'}
              </span>
            ) : (
              <span className="tree-toggle ghost">·</span>
            )}
            <span className="tree-name">{col.name}</span>
            <span className="count">{collectionTotal(col.id)}</span>
          </button>
        </li>
      )
      return hasKids && open ? [row, ...renderTree(col.id, depth + 1)] : [row]
    })
  }

  return (
    <div className="app">
      <header className="topbar">
        <button
          className="brand"
          onClick={() => {
            setKbManagerOpen(false)
            setActivePaper(null)
            setDetail(null)
            setCollectionId(null)
            setFavoriteOnly(false)
            setLabelFilter(null)
          }}
          title="回到工作台首页"
        >
          <img
            className="mark"
            src="/brand/litbench-emblem-90.png"
            alt=""
            aria-hidden="true"
            draggable={false}
          />
          <span className="name">LitBench</span>
          <span className="sub">文献工作台</span>
        </button>

        <div className="searchbox">
          <input
            value={query}
            onChange={(e) => {
              setQuery(e.target.value)
              setActivePaper(null)
            }}
            placeholder="搜索图号、作者+年份、DOI、标题片段…"
            spellCheck={false}
          />
          {query.trim() && mode && (
            <span className={`chip mode ${mode}`}>{MODE_LABEL[mode] ?? mode}</span>
          )}
          {loading && <span className="chip busy">检索中</span>}
        </div>

        <button
          className="icon-btn theme-toggle"
          data-mode={themeMode}
          onClick={() =>
            setThemeMode(themeMode === 'auto' ? 'light' : themeMode === 'light' ? 'dark' : 'auto')
          }
          title={`主题：${THEME_LABEL[themeMode]}（点击切换：自动 → 宣纸亮色 → 古墨暗色）`}
          aria-label="切换亮色/暗色主题"
        >
          <span className="theme-glyph" aria-hidden="true">
            {themeMode === 'auto' ? '◐' : themeMode === 'light' ? '☀' : '☾'}
          </span>
        </button>

        <button
          className={`icon-btn texture-toggle ${textureOn ? 'on' : ''}`}
          onClick={() => setTextureOn((v) => !v)}
          title={textureOn ? '关闭宣纸纹理（安静阅读）' : '开启宣纸纹理'}
          aria-label="宣纸纹理开关"
        >
          纸
        </button>

        <button
          className="icon-btn"
          onClick={() => setSettingsOpen(true)}
          title="模型 API 设置（BYO key）"
          aria-label="设置"
        >
          ⚙
        </button>

        <label className={`upload ${upload?.status === 'uploading' ? 'busy' : ''}`}>
          <input
            type="file"
            accept="application/pdf,.pdf"
            onChange={(e) => {
              const file = e.target.files?.[0] ?? null
              e.target.value = '' // 允许连续选同一个文件重新上传
              void startUpload(file)
            }}
          />
          {upload?.status === 'uploading' ? '上传中…' : '上传 PDF'}
        </label>

        <span
          className="stat-inline"
          title={`${totals.papers} 篇文献 · ${totals.figures} 张图${
            totals.withFigures < totals.papers
              ? ` · ${totals.papers - totals.withFigures} 篇无图`
              : ''
          }`}
        >
          {totals.papers} 篇 · {totals.figures} 图
        </span>

        <div className="top-actions">
          {(labelStats?.review_total ?? 0) > 0 && (
            <button
              className={`chip review-queue ${labelFilter === 'review' ? 'on' : ''}`}
              title="低置信 / 两套 OCR 冲突 / 无编号的图，需要人工或 agent 确认"
              onClick={() => {
                setActivePaper(null)
                setLabelFilter(labelFilter === 'review' ? null : 'review')
              }}
            >
              待确认 {labelStats?.review_total}
            </button>
          )}
          {aiReady && (
            <button
              className={`chip ai-proposals ${proposalsOpen ? 'on' : ''} ${
                pendingProposals > 0 ? 'has' : ''
              }`}
              title="AI 纠错队列：待审建议 / 已自动接受（可回滚）/ 全部历史（含审计）；采纳后才写正式字段"
              onClick={() => setProposalsOpen(true)}
            >
              AI 建议{pendingProposals > 0 ? ` ${pendingProposals}` : ''}
            </button>
          )}
        </div>

        {authUser && (
          <WorkspaceSwitcher
            current={activeWorkspace ?? 'personal'}
            workgroups={workgroups}
            onSelect={selectWorkspace}
            onManage={(workgroupId) => {
              setWgPanelGroup(workgroupId)
              setWgPanelOpen(true)
            }}
          />
        )}

        <UserMenu />

        {llmStatus && (
          <button
            className={`llm-status ${llmStatus.cls}`}
            onClick={() => setSettingsOpen(true)}
            title="模型 API 配置状态；点击修改"
          >
            <i className="llm-dot" aria-hidden="true" />
            {llmStatus.text}
          </button>
        )}

        <button
          className={`writing-cta ${writingOpen ? 'on' : ''}`}
          title="写作台：把图/表/摘要钉成可引用的写作卡片，导出 LaTeX + BibTeX，并校验 \\cite{}"
          onClick={() => {
            setWritingPaperId(null)
            setWritingOpen(true)
          }}
        >
          <span className="writing-cta-glyph" aria-hidden="true">✎</span>
          写作
        </button>
      </header>

      {error && <div className="banner error">接口错误：{error}（服务端起了吗？）</div>}
      {upload && <UploadBar state={upload} onClose={() => setUpload(null)} />}
      {!activePaper && result?.hint && <div className="banner hint">{result.hint}</div>}

      <div className="body">
        <div className="left-cols">
          <aside className={`sidebar kb-col ${!activeWorkspace && kbCollapsed ? 'collapsed' : ''}`}>
            {activeWorkspace ? (
              <div className="team-side">
                <div className="sidebar-head">
                  <h2>团队库</h2>
                  <button
                    className="link manager-link"
                    onClick={() => {
                      setWgPanelGroup(activeWorkspace)
                      setWgPanelOpen(true)
                    }}
                    title="建组 / 看成员 / 按用户名加人"
                  >
                    管理
                  </button>
                </div>
                <div className="team-scope">
                  <div className="team-scope-name">{workspaceGroup?.name ?? '工作组'}</div>
                  <p className="team-scope-desc">
                    {workspaceGroup?.description || '成员主动共享的文献会出现在这里'}
                  </p>
                  <div className="team-stats">
                    <span>
                      <b>{papers.length}</b> 篇共享
                    </span>
                    <span>
                      <b>{workspaceGroup?.member_count ?? teamDetail?.member_count ?? 0}</b> 位成员
                    </span>
                  </div>
                  <ul className="team-member-chips">
                    {(teamDetail?.members ?? []).map((member) => (
                      <li key={member.user_id} title={`@${member.username}`}>
                        {(member.display_name || member.username).slice(0, 1)}
                      </li>
                    ))}
                  </ul>
                  <button
                    className="primary small team-manage-btn"
                    onClick={() => {
                      setWgPanelGroup(activeWorkspace)
                      setWgPanelOpen(true)
                    }}
                  >
                    管理工作组
                  </button>
                  <p className="team-note">
                    团队库是<strong>文献级共享</strong>：图 / 表 / 摘要随文献可见；
                    组员不能改别人的文献，笔记与收藏各看各的。
                  </p>
                  <button className="link team-back" onClick={() => selectWorkspace('personal')}>
                    ← 回到个人库
                  </button>
                </div>
              </div>
            ) : kbCollapsed ? (
              <button
                className="col-rail"
                onClick={() => setKbCollapsed(false)}
                title="展开知识库（把宽度还给内容区）"
                aria-label="展开知识库"
              >
                <span className="col-rail-text">知识库</span>
                <span className="col-rail-count">{totals.papers}</span>
              </button>
            ) : (
              <>
                <div className="sidebar-head">
                  <button
                    className="collapse-btn"
                    onClick={() => setKbCollapsed(true)}
                    title="收起知识库（水平收起，把宽度让给内容区）"
                    aria-label="收起知识库"
                  >
                    ◀
                  </button>
                  <h2>知识库</h2>
                  <button
                    className="link manager-link"
                    onClick={() => {
                      setKbManagerOpen(true)
                      setActivePaper(null)
                      setDetail(null)
                    }}
                    title="打开知识库操作台：自由分组 / 备注 / 收藏"
                  >
                    管理
                  </button>
                </div>
                <div className="kb-tools">
                  <button
                    className="link"
                    disabled={kbBusy}
                    onClick={() => void handleImportZotero()}
                    title="只读导入本机 Zotero（local API 或 sqlite）"
                  >
                    {kbBusy ? '导入中…' : '导入 Zotero'}
                  </button>
                  <label
                    className="link file-import"
                    title="本机没装 Zotero 时：导出 JSON 再选这个文件"
                  >
                    JSON
                    <input
                      type="file"
                      accept=".json,application/json"
                      onChange={(e) => {
                        const file = e.target.files?.[0] ?? null
                        e.target.value = ''
                        handleImportJson(file)
                      }}
                    />
                  </label>
                  <button
                    className="link"
                    onClick={() => void handleCreateCollection()}
                    title="新建分组（更多整理动作在「管理」里）"
                  >
                    ＋
                  </button>
                </div>
                <ul className="tree">
                  <li>
                    <button
                      className={`tree-node ${collectionId === null && !favoriteOnly ? 'active' : ''}`}
                      onClick={() => {
                        setKbManagerOpen(false)
                        selectCollection(null)
                      }}
                      title="不按集合过滤"
                    >
                      <span className="tree-toggle ghost">·</span>
                      <span className="tree-name">全部文献</span>
                      <span className="count">{totals.papers}</span>
                    </button>
                  </li>
                  <li>
                    <button
                      className={`tree-node favorites ${favoriteOnly ? 'active' : ''}`}
                      onClick={() => {
                        setKbManagerOpen(false)
                        selectFavorites()
                      }}
                      title="收藏夹（虚拟分组：所有星标文献）"
                    >
                      <span className="tree-toggle ghost">★</span>
                      <span className="tree-name">收藏夹</span>
                      <span className="count">{favoriteCount}</span>
                    </button>
                  </li>
                  {renderTree(null, 0)}
                </ul>
              </>
            )}
          </aside>

          <aside className={`sidebar papers-col ${papersCollapsed ? 'collapsed' : ''}`}>
            {papersCollapsed ? (
              <button
                className="col-rail"
                onClick={() => setPapersCollapsed(false)}
                title="展开文献列表（把宽度还给内容区）"
                aria-label="展开文献列表"
              >
                <span className="col-rail-text">文献</span>
                <span className="col-rail-count">{papers.length}</span>
              </button>
            ) : (
              <>
                <div className="sidebar-head papers-head">
                  <button
                    className="collapse-btn"
                    onClick={() => setPapersCollapsed(true)}
                    title="收起文献列表（水平收起，把宽度让给内容区）"
                    aria-label="收起文献列表"
                  >
                    ◀
                  </button>
                  <h2>文献</h2>
                  <button
                    className={activePaper ? 'link' : 'link active'}
                    onClick={() => setActivePaper(null)}
                  >
                    全部图
                  </button>
                </div>
                <ul className="paper-list">
                  {papers.map((paper) => (
                    <li key={paper.id} className="paper-row">
                      <button
                        className={`star ${paper.favorite ? 'on' : ''}`}
                        onClick={() => void toggleFavorite(paper)}
                        title={paper.favorite ? '移出收藏夹' : '加入收藏夹'}
                        aria-label={paper.favorite ? '移出收藏夹' : '加入收藏夹'}
                      >
                        {paper.favorite ? '★' : '☆'}
                      </button>
                      <button
                        className={`paper ${activePaper?.id === paper.id ? 'active' : ''}`}
                        onClick={() => void openPaper(paper)}
                        title={
                          paper.note?.trim() ? `${paper.title}\n备注：${paper.note}` : paper.title
                        }
                      >
                        <span className="paper-title">{paper.title}</span>
                        <span className="paper-meta">
                          {paper.figure_count} 图
                          {paper.table_count > 0 && ` · ${paper.table_count} 表`}
                          {paper.status === 'needs_ocr' && ' · 需 OCR'}
                          {paper.metadata_source === 'zotero' && ' · Zotero'}
                          {paper.has_version === false && ' · 仅元数据'}
                          {activeWorkspace && paper.owner_username && ` · ${paper.owner_username}`}
                        </span>
                        {paper.note?.trim() && (
                          <span className="paper-note-mark" title="有备注">✎</span>
                        )}
                      </button>
                    </li>
                  ))}
                  {papers.length === 0 && !error && (
                    <li className="muted">
                      {activeWorkspace
                        ? '组员在个人库点「共享」后，文献会出现在这里'
                        : favoriteOnly
                          ? '收藏夹还是空的：在文献行点 ☆ 收藏'
                          : collectionId
                            ? '这个集合里还没有文献'
                            : '还没有文献，点右上角「上传 PDF」开始'}
                    </li>
                  )}
                </ul>
              </>
            )}
          </aside>
        </div>

        <main className={`content ${kbManagerOpen ? 'manager-open' : ''}`}>
          {kbManagerOpen ? (
            <KnowledgeManager
              onOpenPaper={(paper) => {
                setKbManagerOpen(false)
                void openPaper(paper)
              }}
              onToast={setToast}
              onChanged={() => {
                void loadCollections()
                void loadTotals()
                void loadPapers()
                // 收藏/备注/分组变化后让收藏计数与当前视图一起刷新（只 bump 一次，不会循环）
                setRefreshKey((k) => k + 1)
              }}
              onClose={() => setKbManagerOpen(false)}
            />
          ) : (
            <>
              <div className="content-head">
                <h1>
                  {activePaper
                    ? activePaper.title
                    : labelFilter === 'review'
                      ? '待确认的图号'
                      : query
                        ? `“${query}” 的结果`
                        : favoriteOnly
                          ? '收藏夹'
                          : activeWorkspace
                            ? `团队库 · ${workspaceGroup?.name ?? ''}`
                            : collectionId
                              ? `知识库 · ${currentCollection?.name ?? ''}`
                              : showPapersView
                                ? '全部文献'
                                : '全部图表'}
                  {!activePaper && (
                    <span className="count">
                      {labelFilter === 'review'
                        ? rows.length
                        : showPapersView
                          ? papers.length
                          : (result?.total ?? 0)}
                    </span>
                  )}
                </h1>

                <div className="head-tools">
                  {activePaper && activePaper.can_edit !== false && (
                    <ShareMenu
                      paper={activePaper}
                      workgroups={workgroups}
                      label
                      onChanged={() => {
                        setRefreshKey((k) => k + 1)
                        void loadPapers()
                      }}
                    />
                  )}
                  {browseFiltersActive && (
                    <div className="view-toggle" role="tablist" aria-label="浏览单位">
                      <button
                        role="tab"
                        aria-selected={browseMode === 'papers'}
                        tabIndex={browseMode === 'papers' ? 0 : -1}
                        className={`vt ${browseMode === 'papers' ? 'on' : ''}`}
                        onClick={() => setBrowseMode('papers')}
                        title="以文献为单位浏览"
                      >
                        文献
                      </button>
                      <button
                        role="tab"
                        aria-selected={browseMode === 'figures'}
                        tabIndex={browseMode === 'figures' ? 0 : -1}
                        className={`vt ${browseMode === 'figures' ? 'on' : ''}`}
                        onClick={() => setBrowseMode('figures')}
                        title="以图表为单位浏览"
                      >
                        图表
                      </button>
                    </div>
                  )}
                  {activePaper &&
                    activePaper.has_version === false &&
                    activePaper.can_edit !== false && (
                      <label className="ghost addfig attach-upload" title="先上传 PDF 并解析">
                        ＋ 上传 PDF
                        <input
                          type="file"
                          accept="application/pdf,.pdf"
                          onChange={(e) => {
                            const file = e.target.files?.[0] ?? null
                            e.target.value = ''
                            void startAttachUpload(file)
                          }}
                        />
                      </label>
                    )}
                  {activePaper &&
                    activePaper.can_edit !== false &&
                    activePaper.has_version !== false &&
                    paperTab === 'figures' && (
                      <>
                        <button
                          className="ghost addfig"
                          onClick={() =>
                            setAddTarget({
                              paperId: activePaper.id,
                              page: 1,
                              title: activePaper.title,
                            })
                          }
                          title="解析器漏掉的图：在原文页面里框选补录"
                        >
                          ＋ 增图
                        </button>
                        {aiReady && (
                          <button
                            className="ghost addfig ai-fix"
                            disabled={!!aiBatch}
                            onClick={() => void runAiBatch(aiReviewQueue)}
                            title="让模型逐张检查图号 / 图注 / 裁框；建议进「AI 建议」队列，采纳才改数据"
                          >
                            {aiBatch
                              ? `AI 修图中 ${aiBatch.done}/${aiBatch.total}`
                              : `AI 修图${aiReviewQueue.length ? `（${aiReviewQueue.length}）` : ''}`}
                          </button>
                        )}
                      </>
                    )}
                  {!activePaper && labelFilter === 'review' && aiReady && aiReviewQueue.length > 0 && (
                    <button
                      className="ghost addfig ai-fix"
                      disabled={!!aiBatch}
                      onClick={() => void runAiBatch(aiReviewQueue)}
                      title="让模型逐张检查图号 / 图注 / 裁框；建议进「AI 建议」队列，采纳才改数据"
                    >
                      {aiBatch
                        ? `AI 复核中 ${aiBatch.done}/${aiBatch.total}`
                        : `AI 复核这 ${Math.min(8, aiReviewQueue.length)} 张`}
                    </button>
                  )}
                </div>
              </div>

              {activePaper && activePaper.can_edit === false && (
                <div className="banner hint team-readonly">
                  这篇文献由 <b>{activePaper.owner_username || '组员'}</b>{' '}
                  共享到工作组：可以阅读图表与摘要，但不能修改、增图或再共享；你的笔记 / 收藏只属于你自己。
                </div>
              )}

              {activePaper ? (
                <div className="paper-work">
                  <nav className="paper-rail" role="tablist" aria-label="文献视图">
                    <button
                      role="tab"
                      aria-selected={paperTab === 'figures'}
                      tabIndex={paperTab === 'figures' ? 0 : -1}
                      className={`rail-tab ${paperTab === 'figures' ? 'on' : ''}`}
                      onClick={() => setPaperTab('figures')}
                      title="图表"
                    >
                      <span className="rail-icon">▦</span>
                      <span className="rail-text">图表</span>
                      <span className="rail-count">{paperFigs.length}</span>
                    </button>
                    <button
                      role="tab"
                      aria-selected={paperTab === 'tables'}
                      tabIndex={paperTab === 'tables' ? 0 : -1}
                      className={`rail-tab ${paperTab === 'tables' ? 'on' : ''}`}
                      onClick={() => setPaperTab('tables')}
                      title="表格：Docling 增强档抽出的结构化表，可回原文 / 复制引用 / 写进卡片"
                    >
                      <span className="rail-icon">▤</span>
                      <span className="rail-text">表格</span>
                      <span className="rail-count">{activePaper.table_count}</span>
                    </button>
                    <button
                      role="tab"
                      aria-selected={paperTab === 'overview'}
                      aria-disabled={!summariesReady}
                      tabIndex={paperTab === 'overview' ? 0 : -1}
                      className={`rail-tab ${paperTab === 'overview' ? 'on' : ''} ${
                        summariesReady ? '' : 'disabled'
                      }`}
                      onClick={() => {
                        if (summariesReady) setPaperTab('overview')
                        else if (!aiReady) setSettingsOpen(true)
                        else setToast('这篇文献还没有 PDF，先上传 PDF 并解析')
                      }}
                      title={
                        summariesReady
                          ? '一次模型调用总结整篇文献（综述 + 要点 + 证据）'
                          : aiReady
                            ? '先上传 PDF 并解析后才能总结'
                            : '先配置模型 API 才能使用 AI 功能'
                      }
                    >
                      <span className="rail-icon">✦</span>
                      <span className="rail-text">AI 速览</span>
                      {!summariesReady && (
                        <span className="rail-hint">{aiReady ? '需 PDF' : '需模型'}</span>
                      )}
                    </button>
                    <button
                      role="tab"
                      aria-selected={paperTab === 'summaries'}
                      aria-disabled={!summariesReady}
                      tabIndex={paperTab === 'summaries' ? 0 : -1}
                      className={`rail-tab ${paperTab === 'summaries' ? 'on' : ''} ${
                        summariesReady ? '' : 'disabled'
                      }`}
                      onClick={() => {
                        if (summariesReady) setPaperTab('summaries')
                        else if (!aiReady) setSettingsOpen(true)
                        else setToast('这篇文献还没有 PDF，先上传 PDF 并解析')
                      }}
                      title={summariesReady ? '按标题切章生成摘要' : '先上传 PDF / 配置模型'}
                    >
                      <span className="rail-icon">≡</span>
                      <span className="rail-text">章节摘要</span>
                      {!summariesReady && (
                        <span className="rail-hint">{aiReady ? '需 PDF' : '需模型'}</span>
                      )}
                    </button>
                    <button
                      role="tab"
                      aria-selected={paperTab === 'reader'}
                      aria-disabled={!paperHasPdf}
                      tabIndex={paperTab === 'reader' ? 0 : -1}
                      className={`rail-tab ${paperTab === 'reader' ? 'on' : ''} ${
                        paperHasPdf ? '' : 'disabled'
                      }`}
                      onClick={() => {
                        if (paperHasPdf) setPaperTab('reader')
                        else setToast('这篇文献还没有 PDF，先上传 PDF 并解析')
                      }}
                      title={
                        paperHasPdf ? '按章节摊开解析后的正文，点击对照原文' : '先上传 PDF 并解析'
                      }
                    >
                      <span className="rail-icon">☰</span>
                      <span className="rail-text">正文速览</span>
                    </button>
                  </nav>

                  <div className="paper-pane">
                    {activePaper.has_version === false ? (
                      <div className="empty">
                        <h3>这篇文献还没有 PDF</h3>
                        <p>只有 Zotero 元数据。上传 PDF 后自动解析图表，才能增图与生成摘要。</p>
                        <label className="primary attach-upload">
                          上传 PDF 并解析
                          <input
                            type="file"
                            accept="application/pdf,.pdf"
                            onChange={(e) => {
                              const file = e.target.files?.[0] ?? null
                              e.target.value = ''
                              void startAttachUpload(file)
                            }}
                          />
                        </label>
                        {activePaper.can_edit === false && (
                          <p className="muted">共享文献只能由 owner 补传 PDF。</p>
                        )}
                      </div>
                    ) : paperTab === 'tables' ? (
                      <PaperTables
                        paperId={activePaper.id}
                        paperTitle={activePaper.title}
                        onOpenSource={(table) =>
                          setTableViewer({ paper: activePaper, table })
                        }
                        onUseInWriting={() => {
                          setWritingPaperId(activePaper.id)
                          setWritingOpen(true)
                        }}
                        onToast={setToast}
                      />
                    ) : paperTab === 'overview' ? (
                      aiReady ? (
                        <PaperOverview paper={activePaper} onToast={setToast} />
                      ) : (
                        <div className="empty">
                          <h3>先配置模型 API</h3>
                          <p>配置 Base URL / Model / API Key 后可用。</p>
                          <button className="primary" onClick={() => setSettingsOpen(true)}>
                            去配置
                          </button>
                        </div>
                      )
                    ) : paperTab === 'summaries' ? (
                      aiReady ? (
                        <ChapterSummary paper={activePaper} onToast={setToast} />
                      ) : (
                        <div className="empty">
                          <h3>先配置模型 API</h3>
                          <p>配置 Base URL / Model / API Key 后可用。</p>
                          <button className="primary" onClick={() => setSettingsOpen(true)}>
                            去配置
                          </button>
                        </div>
                      )
                    ) : paperTab === 'reader' ? (
                      <PaperReader paper={activePaper} />
                    ) : (
                      <div className="grid">
                        {rows.map(({ fig, members }, i) => (
                          <FigureCard
                            key={fig.figure_id}
                            fig={fig}
                            members={members}
                            index={i}
                            onOpen={() => {
                              if (members && members.length > 1 && fig.group_key)
                                setSeriesKey(fig.group_key)
                              else setDetail(fig)
                            }}
                          />
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              ) : activeWorkspace && showPapersView && papers.length === 0 ? (
                <div className="empty">
                  <h3>这个工作组还没有共享文献</h3>
                  <p>在个人库的文献卡片上点「共享」，勾选这个工作组。</p>
                  <button className="ghost" onClick={() => selectWorkspace('personal')}>
                    回到个人库
                  </button>
                </div>
              ) : favoriteOnly && papers.length === 0 ? (
                <div className="empty">
                  <h3>收藏夹还是空的</h3>
                  <p>在文献行点 ☆ 收藏。</p>
                </div>
              ) : browseFiltersActive && collectionId && papers.length === 0 ? (
                <div className="empty">
                  <h3>这个集合还没有文献</h3>
                  <p>重新导入 Zotero，或在「管理」里把文献加进来。</p>
                </div>
              ) : showPapersView ? (
                <PaperList
                  papers={papers}
                  workgroups={workgroups}
                  teamMode={Boolean(activeWorkspace)}
                  onOpen={(paper) => void openPaper(paper)}
                  onToggleFavorite={(paper) => void toggleFavorite(paper)}
                  onShareChanged={() => {
                    setRefreshKey((k) => k + 1)
                    void loadPapers()
                    void loadTotals()
                  }}
                />
              ) : figures.length === 0 && !loading ? (
                favoriteOnly ? (
                  <div className="empty">
                    <h3>收藏夹里还没有图表</h3>
                    <p>先收藏文献；图表列表只显示收藏文献里的图。</p>
                  </div>
                ) : activeWorkspace ? (
                  <div className="empty">
                    <h3>这个工作组的共享文献里还没有图表</h3>
                    <p>共享文献解析出图后会出现在这里。</p>
                  </div>
                ) : collectionId ? (
                  <div className="empty">
                    <h3>这个集合里还没有图表</h3>
                    <p>文献解析出图后会出现在这里。</p>
                  </div>
                ) : (
                  <Empty query={query} mode={mode} onPick={setQuery} />
                )
              ) : (
                <>
                  <div className="grid">
                    {rows.map(({ fig, members }, i) => (
                      <FigureCard
                        key={fig.figure_id}
                        fig={fig}
                        members={members}
                        index={i}
                        onOpen={() => {
                          if (members && members.length > 1 && fig.group_key)
                            setSeriesKey(fig.group_key)
                          else setDetail(fig)
                        }}
                      />
                    ))}
                  </div>
                  {result && figures.length < result.total && (
                    <div
                      style={{
                        display: 'flex',
                        flexDirection: 'column',
                        alignItems: 'center',
                        gap: 8,
                        padding: '20px 0 8px',
                      }}
                    >
                      <span className="muted">
                        已显示 {figures.length} / {result.total} 张
                      </span>
                      <button
                        className="ghost"
                        disabled={searchingMore || loading}
                        onClick={() => void loadMoreFigures()}
                        title="继续加载后面的图表（每次 60 张）"
                      >
                        {searchingMore ? '加载中…' : '加载更多（+60）'}
                      </button>
                    </div>
                  )}
                </>
              )}
            </>
          )}
        </main>

        {detail && (
          <Detail
            key={detail.figure_id}
            fig={detail}
            aiReady={aiReady}
            onClose={() => setDetail(null)}
            onShowSource={() => setViewer(detail)}
            onOpenSettings={() => setSettingsOpen(true)}
            onToast={setToast}
            onAddFigure={() =>
              setAddTarget({
                paperId: detail.paper_id,
                page: detail.page,
                title: detail.paper_title,
              })
            }
            onDeleted={() => {
              setDetail(null)
              setToast('已删除这张图')
              setRefreshKey((k) => k + 1)
              void loadPapers()
            }}
            onChanged={(updated) => {
              setDetail(updated)
              setRefreshKey((k) => k + 1)
            }}
          />
        )}
      </div>

      {seriesKey && seriesMembers.length > 0 && (
        <SeriesOverlay
          figures={seriesMembers}
          onClose={() => setSeriesKey(null)}
          onPick={(fig) => {
            setSeriesKey(null)
            setDetail(fig)
          }}
        />
      )}

      {settingsOpen && (
        <LlmSettings
          onClose={() => setSettingsOpen(false)}
          onToast={setToast}
          onChanged={setLlmSettings}
        />
      )}

      <Proposals
        open={proposalsOpen}
        onClose={() => setProposalsOpen(false)}
        onToast={setToast}
        aiReady={aiReady}
        onChanged={() => {
          setRefreshKey((k) => k + 1)
          if (activePaperRef.current) void loadPapers()
        }}
      />

      <Writing
        open={writingOpen}
        initialPaperId={writingPaperId}
        onClose={() => {
          setWritingOpen(false)
          setWritingPaperId(null)
        }}
        onToast={setToast}
      />

      {authUser && wgPanelOpen && (
        <WorkgroupPanel
          open={wgPanelOpen}
          initialGroupId={wgPanelGroup}
          currentUser={authUser}
          workgroups={workgroups}
          onClose={() => setWgPanelOpen(false)}
          onChanged={() => {
            void loadWorkgroups()
            void loadPapers()
            void loadTotals()
            setRefreshKey((k) => k + 1)
          }}
          onCurrentRemoved={(workgroupId) => {
            if (workgroupId === activeWorkspace) selectWorkspace('personal')
          }}
          onToast={setToast}
        />
      )}

      {viewer && (
        <PdfViewer
          figureId={viewer.figure_id}
          paperId={viewer.paper_id}
          page={viewer.page}
          bbox={viewer.bbox && viewer.bbox.length === 4 ? viewer.bbox : null}
          title={viewer.paper_title}
          label={viewer.label_raw}
          allowAdd
          onClose={() => setViewer(null)}
          onAdded={() => {
            setToast('已添加一张图')
            setRefreshKey((k) => k + 1)
            void loadPapers()
            if (activePaperRef.current) void openPaper(activePaperRef.current)
          }}
          onCorrected={(updated) => {
            // 修正框落地后：详情面板、overlay、当前网格三处一起换成新框/新缩略图
            setViewer(updated)
            setDetail(updated)
            setRefreshKey((k) => k + 1)
          }}
        />
      )}

      {tableViewer && (
        <PdfViewer
          figureId={null}
          paperId={tableViewer.paper.id}
          page={tableViewer.table.page}
          bbox={null}
          title={tableViewer.paper.title}
          label={tableViewer.table.label_raw}
          onClose={() => setTableViewer(null)}
        />
      )}

      {addTarget && (
        <PdfViewer
          figureId={null}
          paperId={addTarget.paperId}
          page={addTarget.page}
          bbox={null}
          title={addTarget.title}
          label={null}
          allowAdd
          onClose={() => setAddTarget(null)}
          onAdded={() => {
            setToast('已添加一张图')
            setRefreshKey((k) => k + 1)
            void loadPapers()
            if (activePaperRef.current) void openPaper(activePaperRef.current)
          }}
        />
      )}

      {toast && (
        <div className="toast" role="status">
          <span>{toast}</span>
          <button className="link" onClick={() => setToast(null)}>
            知道了
          </button>
        </div>
      )}
    </div>
  )
}

const UPLOAD_TEXT: Record<UploadState['status'], string> = {
  uploading: '正在上传',
  queued: '已排队，等 worker 取任务',
  running: '解析中',
  done: '解析完成',
  failed: '解析失败',
}

function UploadBar({ state, onClose }: { state: UploadState; onClose: () => void }) {
  const pct = Math.round(Math.min(1, Math.max(0, state.progress)) * 100)
  const busy =
    state.status === 'uploading' || state.status === 'queued' || state.status === 'running'
  // 增强档（Docling）单篇要几十秒，进度只有 10/80/100 三档，显示百分比是假的
  const text = state.escalated
    ? state.status === 'failed'
      ? `增强解析失败，已保留快速解析的结果（${state.error ?? '未知错误'}）`
      : state.status === 'done'
        ? '增强解析完成，列表已刷新'
        : state.status === 'running'
          ? '增强解析中（Docling，比默认档慢）'
          : '已排队增强解析（Docling）'
    : state.status === 'running'
      ? `${UPLOAD_TEXT.running} ${pct}%`
      : state.status === 'done'
        ? `${UPLOAD_TEXT.done}${state.deduplicated ? '（同一文件已在库中，按幂等重解析）' : ''}，列表已刷新`
        : state.status === 'failed'
          ? `${UPLOAD_TEXT.failed}：${state.error ?? '未知错误'}`
          : UPLOAD_TEXT[state.status]

  return (
    <div className={`banner job ${state.status}`}>
      <span className="job-text" title={state.reason ?? ''}>
        {text} · {state.name}
      </span>
      {busy && (
        <span className={`job-track ${state.escalated ? 'indeterminate' : ''}`} aria-hidden>
          <i
            style={
              state.escalated
                ? undefined
                : { width: `${state.status === 'running' ? Math.max(4, pct) : 4}%` }
            }
          />
        </span>
      )}
      <button className="link job-close" onClick={onClose}>
        关闭
      </button>
    </div>
  )
}

/** 单图图标：单个方框，与图集的双框叠影区分。 */
function SingleIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden focusable="false">
      <rect x="2.5" y="2.5" width="11" height="11" rx="1.8" />
    </svg>
  )
}

/** 图集（系列子图）图标：双框叠影，形状 + 颜色都不同，扫一眼能认。 */
function SeriesIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden focusable="false">
      <rect x="1.5" y="1.5" width="8.5" height="8.5" rx="1.8" />
      <rect x="6" y="6" width="8.5" height="8.5" rx="1.8" />
    </svg>
  )
}

function FigureCard({
  fig,
  members,
  index = 0,
  onOpen,
}: {
  fig: Figure
  members?: Figure[]
  index?: number
  onOpen: () => void
}) {
  const series = !!members && members.length > 1
  return (
    <button
      className={`card ${series ? 'series-card' : ''}`}
      style={{ '--i': index } as CSSProperties}
      onClick={onOpen}
      title={fig.caption || fig.label_raw || ''}
    >
      <div className="thumb">
        {fig.image_url ? (
          <img src={fig.image_url} alt={fig.label_raw ?? 'figure'} loading="lazy" />
        ) : (
          <span className="noimg">无图片</span>
        )}
        {series && (
          <span className="kindchip series">
            <SeriesIcon />
            {`图集 · ${members.length}`}
          </span>
        )}
        {!series && (
          <span className="kindchip single">
            <SingleIcon />
            单图
          </span>
        )}
        {fig.origin === 'manual' && <span className="kindchip manual">手动添加</span>}
        <span className="thumb-page">p.{fig.page}</span>
      </div>
      <div className="card-foot">
        <span className="label">
          {fig.label_raw ?? '无编号'}
          {fig.sublabel ? ` (${fig.sublabel})` : ''}
        </span>
        {fig.label_raw && typeof fig.label_confidence === 'number' && fig.label_confidence < 0.6 && (
          <span className="flag low" title="低置信编号，待人工/agent 确认">
            待确认
          </span>
        )}
        <span className="dots">
          {fig.review_status === 'ok' && (
            <span className="dot ok" title="定位已确认（点卡片可改判）" aria-label="定位已确认" />
          )}
          {fig.review_status === 'wrong' && (
            <span className="dot bad" title="定位已判错" aria-label="定位已判错" />
          )}
          {fig.label_source === 'ocr_consensus' && (
            <span className="dot ocr" title="两套 OCR 读取一致" aria-label="两套 OCR 读取一致" />
          )}
          {fig.label_source === 'rule_repair' && (
            <span className="dot repair" title="图号原文有噪声，规则层修复过" aria-label="规则层修复过" />
          )}
        </span>
      </div>
      <div className="card-caption">{fig.caption || '（无 caption）'}</div>
    </button>
  )
}

/** 系列图集浮层：背景虚化 + 只有子图卡片可交互；Esc / 点背景收起。 */
function SeriesOverlay({
  figures,
  onClose,
  onPick,
}: {
  figures: Figure[]
  onClose: () => void
  onPick: (fig: Figure) => void
}) {
  const panelRef = useRef<HTMLElement>(null)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    panelRef.current?.focus()
    document.body.classList.add('overlay-open')
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.classList.remove('overlay-open')
    }
  }, [onClose])

  const title = figures.find((f) => f.label_raw)?.label_raw ?? '无编号'
  const ordered = [...figures].sort((a, b) => (a.sublabel ?? '').localeCompare(b.sublabel ?? ''))

  return (
    <div className="series-backdrop" role="presentation" onPointerDown={onClose}>
      <section
        className="series-panel"
        role="dialog"
        aria-modal="true"
        aria-label={`系列图 ${title}`}
        tabIndex={-1}
        ref={panelRef}
        onPointerDown={(e) => e.stopPropagation()}
      >
        <header className="series-head">
          <span className="series-title">
            {title} <span className="muted">· 系列 {figures.length} 张</span>
          </span>
          <span className="series-hint">点任一张看详情 · Esc 或点空白处收起</span>
          <button className="close" onClick={onClose} aria-label="收起系列">
            ✕
          </button>
        </header>
        <div className="series-grid">
          {ordered.map((f) => (
            <button
              key={f.figure_id}
              className="series-item"
              onClick={() => onPick(f)}
              title={f.caption || f.label_raw || ''}
            >
              <span className="series-img">
                {f.image_url ? (
                  <img src={f.image_url} alt={f.label_raw ?? 'sub figure'} loading="lazy" />
                ) : (
                  <span className="noimg">无图片</span>
                )}
                <span className="series-badge">
                  {(f.label_raw ?? '图') + (f.sublabel ? ` (${f.sublabel})` : '')}
                </span>
              </span>
              <span className="series-cap">{(f.caption || '（无 caption）').slice(0, 80)}</span>
              <span className="series-page">
                p.{f.page}
                {f.label_source === 'rule_inherit' ? ' · 子图' : ''}
                {f.origin === 'manual' ? ' · 手动' : ''}
              </span>
            </button>
          ))}
        </div>
      </section>
    </div>
  )
}

function Detail({
  fig,
  aiReady,
  onClose,
  onShowSource,
  onAddFigure,
  onDeleted,
  onChanged,
  onOpenSettings,
  onToast,
}: {
  fig: Figure
  aiReady: boolean
  onClose: () => void
  onShowSource: () => void
  onAddFigure?: () => void
  onDeleted?: () => void
  onChanged?: (fig: Figure) => void
  onOpenSettings?: () => void
  onToast?: (message: string) => void
}) {
  const [copied, setCopied] = useState(false)
  const [status, setStatus] = useState(fig.review_status)
  const [busy, setBusy] = useState(false)
  const [deleting, setDeleting] = useState(false)
  // 阶段 3：图号校核（改图号 / 从系列图拆出）
  const [editingLabel, setEditingLabel] = useState(false)
  const [labelDraft, setLabelDraft] = useState('')
  const [labelNotice, setLabelNotice] = useState<string | null>(null)
  const [labelBusy, setLabelBusy] = useState(false)
  // M3：AI 图纠错建议（只提议，采纳才改图）
  const [proposal, setProposal] = useState<FigureProposal | null>(null)
  const [proposalBusy, setProposalBusy] = useState(false)
  const [deciding, setDeciding] = useState(false)
  // 异步请求发出后，详情可能已经切到别的图（批 D8）：用它判断结果是否还属于当前这张图
  const figIdRef = useRef(fig.figure_id)

  useEffect(() => {
    figIdRef.current = fig.figure_id
  }, [fig.figure_id])

  useEffect(() => {
    let cancelled = false
    setProposal(null)
    if (!aiReady) return
    void listFigureProposals(fig.figure_id, 'pending')
      .then((rows) => {
        if (!cancelled) setProposal(rows[0] ?? null)
      })
      .catch(() => {
        /* 建议读取失败不影响详情主流程 */
      })
    return () => {
      cancelled = true
    }
  }, [fig.figure_id, aiReady])

  const mark = async (verdict: 'ok' | 'wrong') => {
    setBusy(true)
    try {
      const updated = await reviewFigure(fig.figure_id, verdict)
      setStatus(updated.review_status)
      // 让网格卡片 / 详情同步最新（onChanged 会 bump refreshKey 走静默刷新）
      onChanged?.(updated)
      onToast?.(verdict === 'ok' ? '已确认定位正确' : '已标记定位错误')
    } catch (err) {
      onToast?.(err instanceof Error ? `保存判定失败：${err.message}` : '保存判定失败')
    } finally {
      setBusy(false)
    }
  }

  const saveLabel = async () => {
    setLabelBusy(true)
    setLabelNotice(null)
    try {
      const updated = await updateFigureLabel(fig.figure_id, labelDraft)
      setLabelNotice('已保存，检索会立刻用新图号')
      setEditingLabel(false)
      onChanged?.(updated)
    } catch (err) {
      setLabelNotice(err instanceof Error ? `保存失败：${err.message}` : '保存失败')
    } finally {
      setLabelBusy(false)
    }
  }

  const splitGroup = async () => {
    setLabelBusy(true)
    setLabelNotice(null)
    try {
      const updated = await groupFigure(fig.figure_id, 'split')
      setLabelNotice('已从系列图拆出')
      onChanged?.(updated)
    } catch (err) {
      setLabelNotice(err instanceof Error ? `拆出失败：${err.message}` : '拆出失败')
    } finally {
      setLabelBusy(false)
    }
  }

  const removeFigure = async () => {
    if (!window.confirm(`删除「${fig.label_raw ?? '无编号'}」这张图？会从检索/图集/计数里移除。`)) {
      return
    }
    setDeleting(true)
    setLabelNotice(null)
    try {
      await deleteFigure(fig.figure_id)
      onDeleted?.()
    } catch (err) {
      setLabelNotice(err instanceof Error ? `删除失败：${err.message}` : '删除失败')
    } finally {
      setDeleting(false)
    }
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const runAiReview = async () => {
    if (!aiReady) {
      onOpenSettings?.()
      return
    }
    const targetId = fig.figure_id
    setProposalBusy(true)
    try {
      const created = await aiReviewFigure(targetId)
      // 返回时详情可能已经切到别的图：旧结果丢弃（批 D8）
      if (figIdRef.current !== targetId) return
      setProposal(created)
      onToast?.('AI 建议已生成，请核对后采纳或忽略')
    } catch (err) {
      if (figIdRef.current !== targetId) return
      onToast?.(err instanceof Error ? `AI 纠错失败：${err.message}` : 'AI 纠错失败')
    } finally {
      if (figIdRef.current === targetId) setProposalBusy(false)
    }
  }

  const decideAi = async (action: 'accept' | 'reject') => {
    if (!proposal || deciding) return
    setDeciding(true)
    try {
      const result = await decideProposal(
        proposal.proposal_id,
        action,
        action === 'reject' ? '人工忽略' : undefined,
      )
      setProposal(result.proposal)
      if (action === 'accept') {
        onToast?.(Object.keys(result.applied).length ? '已采纳，图数据已更新' : '已采纳（无需改动）')
        if (result.figure) onChanged?.(result.figure)
      } else {
        onToast?.('已忽略这条建议')
      }
    } catch (err) {
      onToast?.(err instanceof Error ? `处理失败：${err.message}` : '处理失败')
    } finally {
      setDeciding(false)
    }
  }

  return (
    <aside className="detail">
      <div className="detail-head">
        <span className="label big">{fig.label_raw ?? '无编号'}</span>
        <button className="close" onClick={onClose} aria-label="关闭">
          ✕
        </button>
      </div>

      {fig.image_url ? (
        <img className="detail-img" src={fig.image_url} alt={fig.label_raw ?? 'figure'} />
      ) : (
        <div className="detail-img placeholder">这张图没有渲染结果</div>
      )}

      <div className="detail-body">
        <button className="primary wide" onClick={onShowSource}>
          在原文中查看（第 {fig.page} 页）
        </button>

        <div className="review">
          <span className={`chip review ${status}`}>{REVIEW_LABEL[status] ?? status}</span>
          <span className="spacer" />
          <button className="ghost" disabled={busy} onClick={() => void mark('ok')}>
            定位正确
          </button>
          <button className="ghost" disabled={busy} onClick={() => void mark('wrong')}>
            定位错了
          </button>
        </div>
        <p className="tip small">
          判定会落库，作为解析器回归的 golden set；定位错了可在原文里拖框修正。
        </p>

        <div className="label-review">
          <div className="label-review-row">
            <span className="muted">图号</span>
            {editingLabel ? (
              <input
                className="label-input"
                value={labelDraft}
                onChange={(e) => setLabelDraft(e.target.value)}
                placeholder="图 4.2.1 / Fig. 3（留空 = 确认无编号）"
                spellCheck={false}
                maxLength={200}
                autoFocus
              />
            ) : (
              <code>{fig.label_raw ?? '（无编号）'}</code>
            )}
            {editingLabel ? (
              <>
                <button className="ghost" disabled={labelBusy} onClick={() => void saveLabel()}>
                  {labelBusy ? '保存中…' : '保存'}
                </button>
                <button
                  className="ghost"
                  disabled={labelBusy}
                  onClick={() => {
                    setEditingLabel(false)
                    setLabelNotice(null)
                  }}
                >
                  取消
                </button>
              </>
            ) : (
              <>
                <button
                  className="ghost"
                  onClick={() => {
                    setLabelDraft(fig.label_raw ?? '')
                    setEditingLabel(true)
                    setLabelNotice(null)
                  }}
                >
                  改图号
                </button>
                {fig.group_key && (
                  <button className="ghost" disabled={labelBusy} onClick={() => void splitGroup()}>
                    从系列图拆出
                  </button>
                )}
              </>
            )}
          </div>
          {labelNotice && <p className="tip small">{labelNotice}</p>}
        </div>

        <div className="ai-figure-review">
          <div className="ai-review-head">
            <span className="muted">AI 纠错</span>
            <button
              className="ghost small"
              disabled={proposalBusy || deciding}
              onClick={() => void runAiReview()}
              title={
                aiReady
                  ? '让模型检查图号 / 图注 / 裁框；只产出建议，采纳后才改数据'
                  : '先配置模型 API'
              }
            >
              {proposalBusy ? 'AI 检查中…' : proposal ? '重新检查' : 'AI 检查这张图'}
            </button>
            {proposal && (
              <>
                <span className={`proposal-mode ${proposal.mode}`}>
                  {proposal.mode === 'vision' ? '视觉' : '文字'}
                </span>
                <span className={`proposal-conf ${proposal.confidence >= 0.9 ? 'high' : 'mid'}`}>
                  {Math.round(proposal.confidence * 100)}%
                </span>
              </>
            )}
          </div>
          {!aiReady && (
            <p className="tip small">配置模型 API 后可让 AI 检查图号 / 图注 / 裁框。</p>
          )}
          {proposal && proposal.status === 'pending' && (
            <div className="ai-proposal-inline">
              {proposal.proposed.suggested_label !== null &&
                proposal.proposed.suggested_label !== undefined && (
                  <p className="ai-proposal-line">
                    图号：<code>{proposal.proposed.current_label ?? '（无编号）'}</code>
                    <span className="proposal-arrow">→</span>
                    <code className="proposal-new">
                      {proposal.proposed.suggested_label === ''
                        ? '（无编号）'
                        : proposal.proposed.suggested_label}
                    </code>
                  </p>
                )}
              {proposal.proposed.cleaned_caption && (
                <p className="ai-proposal-line ai-proposal-caption">
                  图注：{proposal.proposed.cleaned_caption}
                </p>
              )}
              {CROP_ISSUE_LABEL[proposal.proposed.crop_issue ?? ''] && (
                <p className="ai-proposal-line">
                  裁框：{CROP_ISSUE_LABEL[proposal.proposed.crop_issue ?? '']}
                  <span className="muted">（采纳只标为「定位错误」，框请在原文里拖）</span>
                </p>
              )}
              {(proposal.proposed.summary || proposal.proposed.reason) && (
                <p className="ai-proposal-reason">
                  {proposal.proposed.summary || proposal.proposed.reason}
                </p>
              )}
              <div className="ai-proposal-actions">
                <button
                  className="primary small"
                  disabled={deciding}
                  onClick={() => void decideAi('accept')}
                >
                  {deciding ? '处理中…' : '采纳建议'}
                </button>
                <button
                  className="ghost small"
                  disabled={deciding}
                  onClick={() => void decideAi('reject')}
                >
                  忽略
                </button>
              </div>
            </div>
          )}
          {proposal && proposal.status === 'no_change' && (
            <p className="tip small">
              AI 检查通过：图号 / 图注 / 裁框均无明显问题（置信{' '}
              {Math.round(proposal.confidence * 100)}%）。
            </p>
          )}
          {proposal && (proposal.status === 'accepted' || proposal.status === 'rejected') && (
            <p className="tip small">
              这条建议已{proposal.status === 'accepted' ? '采纳' : '忽略'}。
            </p>
          )}
        </div>

        <div className="fig-actions">
          {onAddFigure && (
            <button className="ghost" onClick={onAddFigure} title="解析器漏掉的图，可以在原文里框选补录">
              ＋ 在原文里框选加图
            </button>
          )}
          <button
            className="ghost danger"
            disabled={deleting}
            onClick={() => void removeFigure()}
            title="解析器误报（不是图/框错）时删掉；软删除，重解析不会复活"
          >
            {deleting ? '删除中…' : '删除这张图'}
          </button>
        </div>

        <p className="caption">{fig.caption || '（无 caption）'}</p>

        <dl>
          <dt>文献</dt>
          <dd>{fig.paper_title}</dd>
          <dt>页码</dt>
          <dd>p.{fig.page}</dd>
          <dt>编号来源</dt>
          <dd>
            {LABEL_SOURCE_LABEL[fig.label_source ?? ''] ?? '—'}
            {typeof fig.label_confidence === 'number' &&
              ` · 置信度 ${Math.round(fig.label_confidence * 100)}%`}
            {fig.group_key && ' · 系列图'}
            {fig.origin === 'manual' && ' · 手动添加'}
          </dd>
          <dt>引用键</dt>
          <dd>
            <code>{fig.cite_key}</code>
            <button
              className="link"
              onClick={async () => {
                await copyText(`[${fig.cite_key}]`)
                setCopied(true)
                window.setTimeout(() => setCopied(false), 1500)
              }}
            >
              {copied ? '已复制' : '复制'}
            </button>
          </dd>
          <dt>许可</dt>
          <dd className={`lic ${fig.license_hint}`}>{LICENSE_LABEL[fig.license_hint] ?? fig.license_hint}</dd>
        </dl>

        <p className="tip">
          引用图时不要直接截图 —— 用「引用建议」或数据化重绘（P3）。当前只提供定位与出处。
        </p>
      </div>
    </aside>
  )
}

const LICENSE_LABEL: Record<string, string> = {
  reuse: '可直接复用（标注来源）',
  permission: '需向出版方申请许可',
  redraw: '建议数据化重绘',
  unknown: '许可未知，按需授权处理',
}

const REVIEW_LABEL: Record<string, string> = {
  pending: '未审核',
  ok: '已确认定位',
  wrong: '已判定位错误',
}

/** 主题三态的显示名（顶栏按钮 tooltip 用）。 */
const THEME_LABEL: Record<'auto' | 'light' | 'dark', string> = {
  auto: '跟随系统',
  light: '宣纸亮色',
  dark: '古墨暗色',
}

/** AI 图纠错里的裁框问题（只标错误，不自动改框）。 */
const CROP_ISSUE_LABEL: Record<string, string> = {
  cut_off: '图被切掉一部分',
  too_large: '框里混进了正文',
  too_small: '框太小 / 只有空白',
  not_figure: '这块不是图',
}

const LABEL_SOURCE_LABEL: Record<string, string> = {
  text: '原文图注直接提取',
  rule_repair: '规则层修复（噪声/OCR 错字）',
  rule_inherit: '系列子图继承父图号',
  rule_infer: '按编号序列推断（低置信）',
  ocr: 'OCR 识别',
  ocr_consensus: '两套 OCR 交叉一致',
  ocr_cross_conflict: '两套 OCR 不一致（待确认）',
  ocr_cross_unresolved: '两套 OCR 一致但编号无法归一（待确认）',
  review: '人工/agent 确认',
  none: '未识别到编号',
}

/** 知识库集合里的文献卡片（M1）：元数据来自 Zotero 只读镜像或上传。 */
/** 文献卡片里的摘要/备注截断（不引入日期库/工具库）。 */
function clip(text: string, limit: number): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > limit ? `${flat.slice(0, limit)}…` : flat
}

function PaperList({
  papers,
  workgroups,
  teamMode,
  onOpen,
  onToggleFavorite,
  onShareChanged,
}: {
  papers: Paper[]
  workgroups: Workgroup[]
  teamMode: boolean
  onOpen: (paper: Paper) => void
  onToggleFavorite?: (paper: Paper) => void
  onShareChanged?: () => void
}) {
  return (
    <div className="papergrid">
      {papers.map((paper, i) => (
        <div className="papercard" key={paper.id} style={{ '--i': i } as CSSProperties}>
          <button
            className="papercard-main"
            onClick={() => onOpen(paper)}
            title={paper.title}
          >
            <div className="papercard-title">{paper.title}</div>
            <div className="papercard-meta">
              {[paper.year, paper.venue].filter(Boolean).join(' · ') || '未著录'}
            </div>
            {paper.abstract?.trim() && (
              <div className="papercard-abstract">{clip(paper.abstract, 120)}</div>
            )}
            {(paper.tags_json?.length ?? 0) > 0 && (
              <div className="papercard-tags">
                {(paper.tags_json ?? []).slice(0, 5).map((tag) => (
                  <span className="tag" key={tag}>
                    {tag}
                  </span>
                ))}
              </div>
            )}
            {paper.note?.trim() && (
              <div className="papercard-note" title={paper.note}>
                ✎ {clip(paper.note, 80)}
              </div>
            )}
            <div className="papercard-foot">
              <span>
                {paper.figure_count} 图
                {paper.table_count > 0 && ` · ${paper.table_count} 表`}
              </span>
              <span className="papercard-flags">
                {paper.has_version === false && <span className="tag">仅元数据</span>}
                {paper.favorite && <span className="tag fav">★ 收藏</span>}
                {teamMode && paper.owner_username && (
                  <span className="tag team" title="由这位成员共享到工作组">
                    由 {paper.owner_username} 共享
                  </span>
                )}
                <span className={`src ${paper.metadata_source ?? 'upload'}`}>
                  {paper.metadata_source === 'zotero'
                    ? 'Zotero'
                    : paper.metadata_source === 'arxiv'
                      ? 'arXiv'
                      : '本地'}
                </span>
              </span>
            </div>
          </button>
          {onToggleFavorite && (
            <button
              className={`papercard-star ${paper.favorite ? 'on' : ''}`}
              onClick={() => onToggleFavorite(paper)}
              title={paper.favorite ? '移出收藏夹' : '加入收藏夹'}
              aria-label={paper.favorite ? '移出收藏夹' : '加入收藏夹'}
            >
              {paper.favorite ? '★' : '☆'}
            </button>
          )}
          {onShareChanged && (
            <ShareMenu paper={paper} workgroups={workgroups} onChanged={onShareChanged} />
          )}
        </div>
      ))}
    </div>
  )
}

function Empty({ query, mode, onPick }: { query: string; mode: string | null; onPick: (q: string) => void }) {
  return (
    <div className="empty">
      {query && mode === 'provenance' ? (
        <>
          <h3>没有命中这个图号</h3>
          <p>
            确定性查找，没有命中。预印本与正式版图号可能不同；试试 Fig. 3 / Figure 3 两种写法。
          </p>
        </>
      ) : (
        <>
          <h3>还没有图表</h3>
          <p>试试直接输入图号，或者从这些例子开始：</p>
        </>
      )}
      <div className="examples">
        {EXAMPLES.map((example) => (
          <button key={example} className="chip example" onClick={() => onPick(example)}>
            {example}
          </button>
        ))}
      </div>
    </div>
  )
}
