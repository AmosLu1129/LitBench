# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTTP 出入参。与 ORM 模型分开，避免表结构泄漏到 API 契约里。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field


def _ensure_utc(value: datetime) -> datetime:
    """把 naive 时间补成 UTC；aware 时间原样保留。

    SQLite 的 ``DateTime(timezone=True)`` 读回来是 naive 的：直接序列化会输出
    没有时区后缀的字符串，前端按本地时区解释就整体偏移（东八区少 8 小时）。
    对外响应必须带明确时区。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# 所有对外响应里的 datetime 都用它序列化（和日志/DB 存储无关，只影响 API 契约）。
UTCDateTime = Annotated[datetime, AfterValidator(_ensure_utc)]


class FigureOut(BaseModel):
    figure_id: str
    cite_key: str
    paper_id: str
    paper_title: str
    year: int | None = None
    version_id: str
    version_kind: str
    page: int
    label_raw: str | None = None
    label_norm: str | None = None
    sublabel: str | None = None
    caption: str = ""
    figure_type: str | None = None
    license_hint: str = "unknown"
    bbox: list = Field(default_factory=list)
    image_url: str | None = None
    # 图号校核：编号从哪来、有多可信、属于哪个子图组（见 docs/14）
    label_source: str | None = None
    label_confidence: float | None = None
    group_key: str | None = None
    # parser = 解析器产出；manual = 用户在原文里框选添加
    origin: str = "parser"
    # 纠错通道
    review_status: str = "pending"
    review_note: str | None = None
    bbox_override: list | None = None


class ReviewIn(BaseModel):
    """人工或 agent 对一张图定位的判定。

    人类点和 agent 调走同一条通道 —— 这是「误报可接受」策略成立的前提。
    """

    verdict: Literal["ok", "wrong"]
    note: str | None = Field(None, max_length=2000)
    bbox: list[float] | None = Field(None, description="修正框 [x0,y0,x1,y1]，PDF 点、左上原点")
    reviewer: str | None = Field(None, max_length=64, description="human | agent | 具体名字")


class LabelIn(BaseModel):
    """人工/agent 改图号（阶段 3 校核入口）。

    ``label`` 是原文写法（「图 4.2.1」/「Fig. 3」）；空字符串表示「确认这张图没有编号」。
    无法归一成 fig:x 的原文（如图 4.e.1）也允许保存，label_norm 置空，保留给人工检索/引用。
    """

    label: str = Field("", max_length=200)
    reviewer: str | None = Field(None, max_length=64, description="human | agent")


class GroupIn(BaseModel):
    """系列子图的手工归并/拆开。"""

    action: Literal["split", "merge"]
    member_ids: list[str] = Field(default_factory=list, max_length=20)


class FigureCreateIn(BaseModel):
    """用户在原文里框选添加一张图（补解析器漏掉的图）。

    ``paper_id`` 指向文献，服务端取它最新的 paper_version，从 PDF 对应页裁图入库。
    ``label`` 可选（不填就是一张无编号图，进待确认队列）。
    """

    paper_id: str
    page: int = Field(ge=1, description="1 起")
    bbox: list[float] = Field(min_length=4, max_length=4, description="[x0,y0,x1,y1]，PDF 点、左上原点")
    label: str = Field("", max_length=200)
    caption: str = Field("", max_length=2000)


class FigureSearchOut(BaseModel):
    """检索结果。

    ``mode`` 说明走的哪条路：
      - provenance —— 出处检索，结果确定性匹配，不排序
      - semantic   —— 语义检索（M9.1 起是 BM25 词面排序，向量召回待接）
      - browse     —— 空查询，按时间浏览

    ``engine`` 说明实际引擎：``bm25`` / ``like``（仅评估基线）/ ``provenance`` / ``browse``。
    ``degraded`` 保留给旧基线；正常 API 语义检索为 False。
    """

    mode: str
    degraded: bool = False
    engine: str = "provenance"
    hint: str | None = None
    # M9.1b-lite：本次是否用 chat 模型把中文查询改写成英文术语（词面辅助，不是向量）
    query_expanded: bool = False
    expanded_keywords: list[str] = Field(default_factory=list)
    expansion_provider: str | None = None
    expansion_model: str | None = None
    # 匹配总数（加 limit/offset 之前算），不是当前页条数；翻页/「共 N 条」都用它
    total: int = Field(description="匹配总数（不受 limit/offset 影响）")
    items: list[FigureOut]


class PaperOut(BaseModel):
    id: str
    title: str
    # 作者原始结构（Zotero / 手动补全）：[{"firstName","lastName"} | {"name"}]
    authors_json: list = Field(default_factory=list)
    doi: str | None = None
    arxiv_id: str | None = None
    cite_key: str | None = None
    year: int | None = None
    venue: str | None = None
    # 著录字段（2026-11-13）：生成 references 条目用；缺失不影响阅读
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    # 条目类型（迁移 0015）：journal|conference|preprint|thesis|book|report|other；
    # 学位论文的学校/学位/出版地也在这里返回，前端「学位论文」导出与 References 用。
    entry_type: str = "journal"
    thesis_school: str | None = None
    thesis_degree: str | None = None
    thesis_place: str | None = None
    status: str
    figure_count: int = 0
    table_count: int = 0
    created_at: UTCDateTime | None = None
    # 知识库（M1）：元数据 + Zotero 只读镜像
    abstract: str | None = None
    tags_json: list = Field(default_factory=list)
    reading_status: str = "unread"
    metadata_source: str = "upload"
    zotero_item_key: str | None = None
    # 是否已有 PDF（paper_version）：Zotero 只读导入的文献只有元数据时为 false，
    # 前端据此禁用「增图/摘要」并引导先上传 PDF。
    has_version: bool = False
    # 工作台（2026-11-12 UI 改版）：用户备注与收藏
    note: str | None = None
    favorite: bool = False
    # 该文献所属的手动/Zotero 分组（知识库操作台用来显示勾选状态）
    collection_ids: list[str] = Field(default_factory=list)
    # 数据归属（M7 第二步）：owner 为空 = legacy 全局数据（迁移前的老库）；
    # can_edit 由 token 角色 + owner 判定，前端据此隐藏共享/编辑入口。
    owner_user_id: str | None = None
    owner_username: str | None = None
    can_edit: bool = True
    shared_workgroup_ids: list[str] = Field(default_factory=list)


class PaperPatchIn(BaseModel):
    """工作台里对文献的轻量编辑：备注 / 收藏 / 阅读状态；以及 references 用的卷期页。

    不碰 Zotero 镜像来的标题/作者/DOI（D1 只读）；volume/issue/pages 是「手动补全」，
    用于生成格式条目，Zotero 再次导入时若上游有值会覆盖。
    """

    note: str | None = Field(None, max_length=20000)
    favorite: bool | None = None
    reading_status: str | None = Field(None, max_length=16)
    volume: str | None = Field(None, max_length=32)
    issue: str | None = Field(None, max_length=32)
    pages: str | None = Field(None, max_length=64)
    # 条目类型与学位论文著录字段（迁移 0015）：手工上传的学位论文没有 Zotero
    # 类型，靠这里补；Zotero 再次导入时以上游 itemType 为准。
    entry_type: Literal[
        "journal", "conference", "preprint", "thesis", "book", "report", "other"
    ] | None = None
    thesis_school: str | None = Field(None, max_length=255)
    thesis_degree: str | None = Field(None, max_length=255)
    thesis_place: str | None = Field(None, max_length=255)


class MetadataLookupIn(BaseModel):
    """元数据补全查询（M8）：auto 优先用 paper 已有 DOI/arXiv id。"""

    mode: Literal["auto", "doi", "arxiv", "search"] = "auto"
    doi: str | None = Field(None, max_length=255)
    arxiv_id: str | None = Field(None, max_length=64)
    query: str | None = Field(None, max_length=300)


class MetadataFieldOut(BaseModel):
    """一个候选字段的 diff：current → suggested；conflict = 当前已有不同值。"""

    field: str
    label: str
    current: Any = None
    current_text: str = ""
    suggested: Any = None
    suggested_text: str = ""
    empty: bool = False
    conflict: bool = False


class MetadataCandidateOut(BaseModel):
    source: str = "crossref"
    doi: str | None = None
    arxiv_id: str | None = None
    title: str = ""
    year: int | None = None
    venue: str | None = None
    authors: str = ""
    score: float | None = None


class MetadataLookupOut(BaseModel):
    paper_id: str
    mode: str = "auto"
    source: str = ""  # crossref|arxiv
    identifier: str = ""
    fields: list[MetadataFieldOut] = Field(default_factory=list)
    candidates: list[MetadataCandidateOut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class MetadataApplyIn(BaseModel):
    """用户确认后的字段写入；默认只填空白字段，覆盖已有值要 overwrite=true。"""

    fields: dict[str, Any] = Field(..., min_length=1)
    overwrite: bool = False


class PaperTextBlockOut(BaseModel):
    """正文速览里的一段：点它可跳回原文对应页/位置。"""

    block_id: str
    page: int
    type: str
    text: str
    bbox: list = Field(default_factory=list)


class PaperTextSectionOut(BaseModel):
    key: str
    title: str
    level: int
    kind: str = "heading"  # heading = 作者章节；page = 按页伪章节
    page_start: int
    page_end: int
    blocks: list[PaperTextBlockOut] = Field(default_factory=list)


class PaperTextOut(BaseModel):
    """文献正文速览：按章节组织的正文块（只读，不落库）。"""

    paper_id: str
    version_id: str
    page_count: int | None = None
    block_count: int = 0
    sections: list[PaperTextSectionOut] = Field(default_factory=list)


class CollectionOut(BaseModel):
    """知识库目录树节点。``paper_count`` 只算直接挂在它下面的文献。"""

    id: str
    name: str
    parent_id: str | None = None
    sort_order: int = 0
    source: str = "manual"
    zotero_key: str | None = None
    paper_count: int = 0


class CollectionIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    parent_id: str | None = None


class CollectionPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    parent_id: str | None = None


class CollectionPaperIn(BaseModel):
    paper_id: str


class ImportIn(BaseModel):
    """Zotero 只读导入（docs/15 D1）。path 用于 json/sqlite 源。"""

    source: Literal["auto", "json", "sqlite", "api"] = "auto"
    path: str | None = Field(None, description="json 文件或 zotero.sqlite 路径")
    api_base: str | None = Field(None, description="Zotero local API，默认配置里的 localhost")


class ImportRunOut(BaseModel):
    id: str
    source: str
    started_at: UTCDateTime | None = None
    finished_at: UTCDateTime | None = None
    stats: dict = Field(default_factory=dict)
    error: str | None = None


class PaperCreated(BaseModel):
    paper_id: str
    version_id: str
    job_id: str
    sha256: str
    deduplicated: bool = False
    page_count: int | None = None


class TableOut(BaseModel):
    """表格。Docling 增强档才有（轻量档 pdfium 抽不到表结构）。"""

    table_id: str
    paper_id: str
    version_id: str
    page: int
    label_raw: str | None = None
    label_norm: str | None = None
    caption: str = ""
    quality: str = "structured"  # structured|image
    html: str | None = None


class TableLatexOut(BaseModel):
    """表格 → LaTeX（M9.3a/M9.3c）：片段 + 可粘贴浮动体 + 缺字段/换行提示。"""

    table_id: str
    paper_id: str
    caption: str = ""
    label: str = ""
    label_key: str = ""
    tabular: str = ""
    code: str = ""
    columns: int = 0
    rows: int = 0
    warnings: list[str] = Field(default_factory=list)
    # M9.3c：宽表是否已改成 p 列自动换行，以及实际列格式（ll… 或 @{}p{…}…@{}）
    wrapped: bool = False
    column_spec: str = ""


class JobOut(BaseModel):
    id: str
    kind: str
    status: str
    progress: float
    error: str | None = None
    attempts: int
    paper_version_id: str | None = None
    created_at: UTCDateTime | None = None
    # 解析后端。空 = 用全局默认（pdfium）；"docling" = 按需升级后的增强档。
    stage: str | None = None
    escalated_from: str | None = None
    escalated_to: str | None = None
    escalate_reason: str | None = None


# ---------------------------------------------------------------------------
# M2 章节摘要
# ---------------------------------------------------------------------------
class SummaryBriefOut(BaseModel):
    """章节树里挂在某一节上的摘要概要（点开再取全文）。"""

    summary_id: str
    status: str
    source: str
    auto_accepted: bool = False
    human_locked: bool = False
    content_chars: int = 0


class SectionOut(BaseModel):
    key: str
    title: str
    level: int
    kind: str = "heading"  # heading = 作者章节；page = 切不出章节时的按页伪节
    page_start: int
    page_end: int
    block_count: int
    char_count: int
    figure_count: int = 0
    summary: SummaryBriefOut | None = None


class SectionsOut(BaseModel):
    paper_id: str
    version_id: str
    page_count: int | None = None
    sections: list[SectionOut]


class EvidenceBlockOut(BaseModel):
    block_id: str
    page: int
    type: str
    text: str
    bbox: list = Field(default_factory=list)


class SummaryOut(BaseModel):
    summary_id: str
    paper_version_id: str
    section_key: str
    section_title: str
    level: int = 1
    content: str = ""
    key_points: list = Field(default_factory=list)
    evidence_block_ids: list = Field(default_factory=list)
    source: str = "agent"
    provider: str = "mock"
    model: str = ""
    prompt_version: str = ""
    status: str = "draft"
    auto_accepted: bool = False
    human_locked: bool = False
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    confidence: float | None = None
    created_at: UTCDateTime | None = None
    updated_at: UTCDateTime | None = None
    edited_by: str | None = None
    edited_at: UTCDateTime | None = None


class SummaryDetailOut(SummaryOut):
    evidence: list[EvidenceBlockOut] = Field(default_factory=list)


class SummaryGenerateIn(BaseModel):
    section_keys: list[str] | None = Field(
        None, description="只生成这些章节；不传 = 整篇所有章节"
    )
    force: bool = Field(
        False, description="true = 连人工锁定/已发布章节也重新生成（覆盖前会留 revision）"
    )


class SummaryGenerateOut(BaseModel):
    paper_id: str
    version_id: str
    provider: str
    model: str
    prompt_version: str
    sections_total: int
    requested: int
    created: int
    updated: int
    skipped_existing: int
    skipped_locked: int
    skipped_empty: int = 0
    failed: int
    # 本次生成实际花掉的 token/费用（含「模型返回了但没过证据/JSON 校验」的调用）；
    # 连不上/超时这类没拿到响应的失败无法计价，不计入。
    tokens_in_total: int = 0
    tokens_out_total: int = 0
    cost_total: float = 0.0
    results: list[dict] = Field(default_factory=list)


class SummaryEditIn(BaseModel):
    content: str = Field(..., min_length=1, max_length=8000)
    key_points: list[str] | None = Field(None, max_length=8)
    actor: str = Field("human", max_length=64)


class SummaryRevisionOut(BaseModel):
    revision_id: str
    summary_id: str
    content: str = ""
    key_points: list = Field(default_factory=list)
    evidence_block_ids: list = Field(default_factory=list)
    source: str = "agent"
    provider: str = ""
    model: str = ""
    prompt_version: str = ""
    confidence: float | None = None
    status: str = "draft"
    reason: str = "generated"
    edited_by: str | None = None
    created_at: UTCDateTime | None = None


# ---------------------------------------------------------------------------
# 整篇速览（AI 一键总结文献）
# ---------------------------------------------------------------------------
class PaperSummaryGenerateIn(BaseModel):
    force: bool = Field(False, description="true = 连人工编辑过的整篇速览也重新生成（会先留 revision）")


class PaperSummaryGenerateOut(BaseModel):
    paper_id: str
    version_id: str
    status: str  # created | updated
    summary_id: str
    provider: str
    model: str
    prompt_version: str
    confidence: float = 0.0
    auto_accepted: bool = False
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    latency_ms: int = 0


# ---------------------------------------------------------------------------
# 图提取 AI 纠错：建议（proposal）与决策
# ---------------------------------------------------------------------------
class FigureProposalOut(BaseModel):
    proposal_id: str
    figure_id: str
    paper_version_id: str
    kind: str = "figure_review"
    proposed: dict = Field(default_factory=dict)
    evidence: list = Field(default_factory=list)
    confidence: float = 0.0
    status: str = "pending"  # pending|accepted|rejected|superseded|no_change|rolled_back
    provider: str = ""
    model: str = ""
    prompt_version: str = ""
    mode: str = "text"  # vision|text
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    latency_ms: int = 0
    created_at: UTCDateTime | None = None
    decided_at: UTCDateTime | None = None
    decided_by: str | None = None
    decision_reason: str | None = None
    before: dict | None = None
    auto_accepted: bool = False
    rolled_back_at: UTCDateTime | None = None
    rolled_back_by: str | None = None
    figure: dict | None = None
    paper: dict | None = None


class ProposalDecideIn(BaseModel):
    action: Literal["accept", "reject"]
    actor: str = Field("human", max_length=64)
    reason: str | None = Field(None, max_length=2000)


class ProposalDecideOut(BaseModel):
    proposal: FigureProposalOut
    applied: dict = Field(default_factory=dict)
    figure: FigureOut | None = None


class ProposalRollbackIn(BaseModel):
    actor: str = Field("human", max_length=64)


class ProposalBatchIn(BaseModel):
    proposal_ids: list[str] = Field(..., min_length=1, max_length=200)
    action: Literal["accept", "reject"]
    actor: str = Field("human", max_length=64)
    reason: str | None = Field(None, max_length=2000)


class ProposalBatchOut(BaseModel):
    requested: int
    accepted: int = 0
    rejected: int = 0
    failed: list[dict] = Field(default_factory=list)


class OcrProposalOut(BaseModel):
    proposal_id: str
    paper_version_id: str
    page: int
    ocr_text: str = ""
    model_text: str = ""
    differences: list = Field(default_factory=list)
    status: str = "pending"  # pending|accepted_ocr|accepted_model|rejected
    provider: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    latency_ms: int = 0
    created_at: UTCDateTime | None = None
    decided_at: UTCDateTime | None = None
    decided_by: str | None = None


class OcrProposalDecideIn(BaseModel):
    action: Literal["accept_ocr", "accept_model", "reject"]
    reason: str | None = Field(None, max_length=2000)


class AutoAcceptIn(BaseModel):
    limit: int = Field(50, ge=1, le=500)
    paper_id: str | None = None


class AutoAcceptOut(BaseModel):
    scanned: int
    accepted: int
    skipped: int
    items: list = Field(default_factory=list)
    skipped_items: list = Field(default_factory=list)


# ---------------------------------------------------------------------------
# M3 正式化：agent 任务租约 + 审计
# ---------------------------------------------------------------------------
class AgentTaskOut(BaseModel):
    id: str
    kind: str
    target_type: str = "figure"
    target_id: str = ""
    payload: dict = Field(default_factory=dict)
    status: str = "queued"
    priority: int = 0
    idempotency_key: str | None = None
    attempts: int = 0
    max_attempts: int = 3
    actor: str | None = None
    lease_until: UTCDateTime | None = None
    heartbeat_at: UTCDateTime | None = None
    result: dict | None = None
    error: str | None = None
    cost: float = 0.0
    created_at: UTCDateTime | None = None
    started_at: UTCDateTime | None = None
    finished_at: UTCDateTime | None = None


class TaskCreateIn(BaseModel):
    kind: Literal["figure_review", "auto_accept_sweep"] = "figure_review"
    target_type: str = Field("figure", max_length=32)
    target_id: str = Field("", max_length=32)
    payload: dict = Field(default_factory=dict)
    priority: int = 0
    max_attempts: int | None = Field(None, ge=1, le=20)
    idempotency_key: str | None = Field(None, max_length=128)


class TaskClaimIn(BaseModel):
    actor: str = Field("worker", max_length=64)
    lease_seconds: int | None = Field(None, ge=10, le=3600)


class TaskHeartbeatIn(BaseModel):
    actor: str = Field("worker", max_length=64)
    lease_seconds: int | None = Field(None, ge=10, le=3600)


class TaskCompleteIn(BaseModel):
    actor: str = Field("worker", max_length=64)
    result: dict = Field(default_factory=dict)
    cost: float = 0.0


class TaskFailIn(BaseModel):
    actor: str = Field("worker", max_length=64)
    error: str = Field(..., max_length=2000)
    retry: bool = True


class TaskEnqueueFiguresIn(BaseModel):
    """把一批图的 AI 复核排成任务（M3a 的批量入口）。"""

    paper_id: str | None = None
    figure_ids: list[str] | None = Field(None, max_length=200)
    label_state: Literal["unlabeled", "low_confidence", "unreviewed_all"] = "unreviewed_all"
    limit: int = Field(50, ge=1, le=200)


class TaskEnqueueOut(BaseModel):
    created: int
    skipped: int
    task_ids: list[str] = Field(default_factory=list)


class TaskEnqueueBboxIn(BaseModel):
    """把一批图排成 M3c 框复核任务。"""

    paper_id: str | None = None
    figure_ids: list[str] | None = Field(None, max_length=200)
    limit: int = Field(10, ge=1, le=200)


class AuditLogOut(BaseModel):
    id: str
    actor: str
    action: str
    target_type: str
    target_id: str
    before: dict | None = None
    after: dict | None = None
    evidence: list | None = None
    note: str | None = None
    created_at: UTCDateTime | None = None


# ---------------------------------------------------------------------------
# 设置：模型 API（BYO key，M2b）
# ---------------------------------------------------------------------------
class LlmProtocolOut(BaseModel):
    protocol: str
    label: str
    path: str
    hint: str


class LlmSettingsOut(BaseModel):
    """当前生效的模型配置。**永不包含 API Key 原文**，只给掩码。"""

    configured: bool
    source: str  # ui | env | mock
    protocol: str | None = None
    protocol_label: str | None = None
    protocol_path: str | None = None
    base_url: str | None = None
    model: str | None = None
    has_api_key: bool = False
    api_key_masked: str | None = None
    endpoint: str | None = None
    timeout_s: float | None = None
    updated_at: str | None = None
    last_test_ok: bool = False
    last_tested_at: str | None = None
    supported_protocols: list[LlmProtocolOut] = Field(default_factory=list)
    note: str = ""


class LlmSettingsIn(BaseModel):
    """保存模型 API。``api_key`` 留空 = 保留已存的 key（不把原文再传回来）。"""

    protocol: str = Field(..., min_length=1, max_length=64)
    base_url: str = Field(..., min_length=1, max_length=500)
    model: str = Field(..., min_length=1, max_length=200)
    api_key: str | None = Field(None, max_length=500)
    timeout_s: float | None = None


class LlmTestOut(BaseModel):
    ok: bool
    error: str | None = None
    protocol: str
    model: str
    endpoint: str
    latency_ms: int | None = None
    reply: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None


# ---------------------------------------------------------------------------
# M5 写作侧：claim 卡片 / 对比矩阵 / 导出 / 引用校验
# ---------------------------------------------------------------------------
class ClaimEvidenceIn(BaseModel):
    """一条证据：type 决定去查哪张表；id 必须是本库真实存在的对象。"""

    type: Literal["block", "figure", "table", "summary"]
    id: str = Field(..., min_length=1, max_length=64)


class ClaimOut(BaseModel):
    claim_id: str
    paper_id: str
    paper_title: str | None = None
    cite_key: str | None = None
    paper_version_id: str | None = None
    text: str = ""
    section: str = "正文"
    status: str = "draft"
    source: str = "human"
    evidence: list = Field(default_factory=list)
    evidence_stale: bool = False
    tags: list = Field(default_factory=list)
    order_idx: int = 0
    created_by: str | None = None
    created_at: UTCDateTime | None = None
    updated_at: UTCDateTime | None = None


class ClaimRevisionOut(BaseModel):
    """claim 的历史版本（2026-11-15 小修）：恢复前先压一份，可回滚。"""

    revision_id: str
    claim_id: str
    text: str = ""
    section: str = "正文"
    status: str = "draft"
    source: str = "human"
    evidence: list = Field(default_factory=list)
    tags: list = Field(default_factory=list)
    order_idx: int = 0
    paper_version_id: str | None = None
    reason: str = "edited"  # edited|restored
    edited_by: str | None = None
    created_at: UTCDateTime | None = None


class ClaimCreateIn(BaseModel):
    paper_id: str = Field(..., min_length=1, max_length=64)
    text: str = Field(..., min_length=2, max_length=2000)
    section: str = Field("", max_length=255)
    evidence: list[ClaimEvidenceIn] = Field(..., min_length=1, max_length=20)
    tags: list[str] = Field(default_factory=list, max_length=10)
    status: Literal["draft", "approved"] = "draft"
    source: Literal["human", "agent"] = "human"
    order_idx: int | None = None


class ClaimUpdateIn(BaseModel):
    text: str | None = Field(None, min_length=2, max_length=2000)
    section: str | None = Field(None, max_length=255)
    evidence: list[ClaimEvidenceIn] | None = Field(None, min_length=1, max_length=20)
    tags: list[str] | None = Field(None, max_length=10)
    status: Literal["draft", "approved"] | None = None
    order_idx: int | None = None


class MatrixCellOut(BaseModel):
    paper_id: str
    text: str = ""


class MatrixRowOut(BaseModel):
    key: str
    label: str
    values: list[MatrixCellOut] = Field(default_factory=list)


class MatrixPaperOut(BaseModel):
    paper_id: str
    title: str = ""
    cite_key: str | None = None
    year: int | None = None
    venue: str | None = None
    authors: list[str] = Field(default_factory=list)
    has_version: bool = False
    summary_id: str | None = None
    figure_count: int = 0
    table_count: int = 0
    claim_count: int = 0


class MatrixOut(BaseModel):
    paper_ids: list[str]
    papers: list[MatrixPaperOut]
    rows: list[MatrixRowOut]
    note: str = ""


class MatrixDimensionOut(BaseModel):
    """矩阵可选维度（GET /api/matrix/dimensions）。"""

    key: str
    label: str
    description: str = ""
    default: bool = False


class MatrixDimensionsOut(BaseModel):
    dimensions: list[MatrixDimensionOut] = Field(default_factory=list)
    note: str = ""


class WritingExportIn(BaseModel):
    format: Literal["latex", "bibtex", "markdown"] = "latex"
    paper_ids: list[str] | None = None
    claim_ids: list[str] | None = None
    title: str = Field("", max_length=200)
    section_order: list[str] | None = None
    include_drafts: bool = True
    with_evidence_comments: bool = False


class WritingExportOut(BaseModel):
    format: str
    filename: str
    content: str
    extra_files: dict[str, str] = Field(default_factory=dict)
    claim_count: int = 0
    paper_count: int = 0


class WritingDocExportIn(BaseModel):
    """Word/PDF 导出参数（M8）：正文与 `/writing/export` 同一批 claim。"""

    format: Literal["docx", "pdf"] = "docx"
    paper_ids: list[str] | None = None
    claim_ids: list[str] | None = None
    title: str = Field("", max_length=200)
    section_order: list[str] | None = None
    include_drafts: bool = True
    # 参考文献样式；只支持内置 CSL 的 3 种（gbt7714 / apa7 / ieee）
    csl_style: str = Field("gbt7714", min_length=1, max_length=32)


class DocExportCapabilitiesOut(BaseModel):
    docx: bool = False
    pdf: bool = False
    pandoc: bool = False
    typst: bool = False
    note: str = ""


class OutlineIn(BaseModel):
    """写作大纲参数（M8）：与导出同一批 claim，按 section 归章。"""

    paper_ids: list[str] | None = None
    claim_ids: list[str] | None = None
    include_drafts: bool = True
    section_order: list[str] | None = None


class OutlineEvidenceOut(BaseModel):
    type: str
    id: str = ""
    label: str = ""
    page: int | None = None
    snippet: str = ""


class OutlineIssueOut(BaseModel):
    kind: str  # figure|table|citation
    severity: str = "warning"
    message: str


class OutlineClaimOut(BaseModel):
    claim_id: str
    text: str = ""
    status: str = "draft"
    paper_id: str = ""
    paper_title: str | None = None
    cite_key: str | None = None
    order_idx: int = 0
    evidence: list[OutlineEvidenceOut] = Field(default_factory=list)
    figure_labels: list[str] = Field(default_factory=list)
    table_labels: list[str] = Field(default_factory=list)
    issues: list[OutlineIssueOut] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class OutlineSectionOut(BaseModel):
    section: str
    canonical: str = ""
    claim_count: int = 0
    claims: list[OutlineClaimOut] = Field(default_factory=list)


class OutlineOut(BaseModel):
    sections: list[OutlineSectionOut] = Field(default_factory=list)
    total_claims: int = 0
    issue_count: int = 0
    section_order: list[str] = Field(default_factory=list)
    markdown: str = ""
    filename: str = "outline.md"
    warnings: list[str] = Field(default_factory=list)


class CitationCheckIn(BaseModel):
    text: str = Field(..., max_length=20000)


class CitationItemOut(BaseModel):
    key: str
    status: str  # resolved|unknown
    paper_id: str | None = None
    title: str | None = None
    cite_key: str | None = None
    suggestion: str | None = None


class CitationCheckOut(BaseModel):
    total: int
    resolved: int
    unknown: int
    citations: list[CitationItemOut] = Field(default_factory=list)
    message: str = ""


# ---------------------------------------------------------------------------
# 学位论文 LaTeX 包（M8，2026-11-15）：骨架导出 + 引用交叉检查
# ---------------------------------------------------------------------------
class ThesisExportIn(BaseModel):
    """学位论文骨架导出参数。

    正文只来自 claim 卡片；``paper_ids`` 是「参考文献范围」（不写就取 claim
    关联的文献）。章节顺序不给就用默认：摘要 → 绪论 → 相关工作 → 方法 →
    实验与结果 → 结论 → 参考文献；claim 的 section 会按别名归到这些章。
    """

    title: str = Field("", max_length=200)
    author: str = Field("", max_length=100)
    school: str = Field("", max_length=200)
    degree: str = Field("", max_length=100)
    advisor: str = Field("", max_length=100)
    year: int | None = Field(None, ge=1900, le=2100)
    paper_ids: list[str] | None = None
    claim_ids: list[str] | None = None
    chapter_order: list[str] | None = None
    include_drafts: bool = True


class ThesisExportOut(BaseModel):
    format: str = "latex"
    filename: str
    content: str
    extra_files: dict[str, str] = Field(default_factory=dict)
    claim_count: int = 0
    paper_count: int = 0
    chapter_count: int = 0
    warnings: list[str] = Field(default_factory=list)


class ThesisCheckIn(BaseModel):
    text: str = Field(..., max_length=200000)
    # 只检查这些文献范围内「未被引用」的条目；不传 = 当前可见的全部文献
    paper_ids: list[str] | None = None


class ThesisLabelCheckOut(BaseModel):
    key: str
    status: str  # resolved|unknown
    kind: str  # figure|table
    object_id: str | None = None
    label: str | None = None
    suggestion: str | None = None


class ThesisCheckOut(BaseModel):
    total: int = 0
    resolved: int = 0
    unknown: int = 0
    citations: list[CitationItemOut] = Field(default_factory=list)
    figures: list[ThesisLabelCheckOut] = Field(default_factory=list)
    tables: list[ThesisLabelCheckOut] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    unused: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    message: str = ""


# ---------------------------------------------------------------------------
# M5：外部 agent 直接投递提案（MCP proposal.create 的落点）
# ---------------------------------------------------------------------------
class ProposalCreateIn(BaseModel):
    """外部 agent 的提案入参。

    只允许两种 kind（与内置 AI 纠错一致）；``proposed`` 的字段校验由
    ``figure_review`` 的同一套校验器做，避免两条链各写一份规则。
    """

    kind: Literal["figure_review"] = "figure_review"
    figure_id: str = Field(..., min_length=1, max_length=64)
    proposed: dict = Field(default_factory=dict)
    evidence_block_ids: list[str] = Field(default_factory=list, max_length=50)
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    actor: str = Field("agent", max_length=64)
    note: str | None = Field(None, max_length=2000)

# ---------------------------------------------------------------------------
# 参考文献条目（2026-11-13）：按格式预设一键生成 references
# ---------------------------------------------------------------------------
class ReferenceStyleOut(BaseModel):
    key: str
    label: str
    language: str = "zh"  # zh|en|neutral
    kind: str = "text"  # text|bibtex|ris
    description: str = ""
    sample: str = ""
    # 该格式是否有官方 CSL 样式（走 pandoc --citeproc 的 CSL 引擎）
    csl_available: bool = False


class ReferenceStylesOut(BaseModel):
    styles: list[ReferenceStyleOut] = Field(default_factory=list)
    note: str = ""
    # CSL 引擎（pandoc）当前是否可用；不可用时模板引擎照常
    csl_engine_available: bool = False
    csl_note: str = ""


class ReferenceFormatIn(BaseModel):
    paper_ids: list[str] = Field(..., min_length=1, max_length=200)
    style: str = Field("gbt7714", min_length=1, max_length=32)
    numbered: bool = True
    template: str | None = Field(
        None,
        max_length=500,
        description="仅 style=custom 时使用；支持 {authors} {title} {journal} {year} 等占位符",
    )
    # template = 13 种手写模板（默认）；csl = pandoc --citeproc 渲染官方 CSL 样式
    engine: Literal["template", "csl"] = "template"


class ReferenceItemOut(BaseModel):
    paper_id: str
    cite_key: str | None = None
    title: str = ""
    entry: str = ""
    warnings: list[str] = Field(default_factory=list)


class ReferenceFormatOut(BaseModel):
    style: str
    label: str
    paper_count: int = 0
    content: str = ""
    items: list[ReferenceItemOut] = Field(default_factory=list)
    engine: str = "template"  # template|csl
    note: str = ""


class CslJsonIn(BaseModel):
    paper_ids: list[str] = Field(..., min_length=1, max_length=200)


class CslJsonOut(BaseModel):
    """CSL JSON 导出（pandoc / Zotero / 后续 docx 的通用输入）。"""

    count: int = 0
    items: list[dict[str, Any]] = Field(default_factory=list)

# ---------------------------------------------------------------------------
# 团队共享（2026-11-15）：账号 / 登录
# ---------------------------------------------------------------------------
class UserOut(BaseModel):
    id: str
    username: str
    display_name: str = ""
    role: str = "member"
    is_active: bool = True
    created_at: UTCDateTime | None = None
    last_login_at: UTCDateTime | None = None


class AuthStatusOut(BaseModel):
    auth_enabled: bool
    initialized: bool
    user: UserOut | None = None


class LoginIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=200)


class SetupIn(BaseModel):
    username: str = Field("admin", min_length=1, max_length=64)
    password: str = Field(..., min_length=8, max_length=200)
    display_name: str = Field("", max_length=64)


class UserCreateIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=8, max_length=200)
    display_name: str = Field("", max_length=64)
    role: Literal["admin", "member"] = "member"


class UserPatchIn(BaseModel):
    is_active: bool | None = None
    role: Literal["admin", "member"] | None = None
    password: str | None = Field(None, min_length=8, max_length=200)


# ---------------------------------------------------------------------------
# 团队共享（2026-11-15）：工作组 / 成员 / 文献共享
# ---------------------------------------------------------------------------
class WorkgroupIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(None, max_length=2000)


class WorkgroupPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = Field(None, max_length=2000)


class WorkgroupMemberIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)


class WorkgroupMemberOut(BaseModel):
    user_id: str
    username: str
    display_name: str = ""
    role: str = "member"  # owner|member
    joined_at: UTCDateTime | None = None


class WorkgroupOut(BaseModel):
    id: str
    name: str
    description: str | None = None
    created_by: str | None = None
    created_by_name: str | None = None
    role: str = "member"  # 我在这个组里的角色
    member_count: int = 0
    paper_count: int = 0
    created_at: UTCDateTime | None = None


class WorkgroupDetailOut(WorkgroupOut):
    members: list[WorkgroupMemberOut] = Field(default_factory=list)


class PaperShareIn(BaseModel):
    workgroup_id: str = Field(..., min_length=1, max_length=32)


class PaperShareOut(BaseModel):
    paper_id: str
    workgroup_id: str
    workgroup_name: str
    shared_by: str | None = None
    shared_by_name: str | None = None
    shared_at: UTCDateTime | None = None


class PaperShareResultOut(BaseModel):
    share: PaperShareOut
    created: bool = True

class CitationReviewIn(BaseModel):
    """引用核验的模型复核请求（M9.4）：claim + 同文献的证据块。"""

    claim_text: str = Field(..., min_length=1, max_length=4000)
    paper_id: str = Field(..., min_length=1, max_length=32)
    block_ids: list[str] = Field(default_factory=list, min_length=1, max_length=10)


class CitationReviewOut(BaseModel):
    """模型复核结果：verdict + 依据 + 实际调用的 provider/model。"""

    verdict: Literal["supported", "insufficient"]
    confidence: float = 0.0
    reason: str = ""
    provider: str = ""
    model: str = ""
    block_ids: list[str] = Field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    prompt_version: str = ""
