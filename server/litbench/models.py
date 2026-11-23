# SPDX-License-Identifier: AGPL-3.0-or-later
"""表结构。

只落地 P1 需要的 6 张表；写作侧（claim / project / outline / draft /
cite_anchor）从 M5 起按需加——2026-11-13 先加 ``claim``（可引用论断 + 证据），
项目/大纲/草稿仍等真实需求，不提前建空表。

两个刻意的设计：
1. 图挂在 ``paper_version`` 上，不挂在 ``paper`` 上。arXiv v1 的 Fig.3 和正式版
   Fig.3 常常不是同一张图，引用必须指向你实际读到的那一版。
2. ``label_raw`` 与 ``label_norm`` 都存。前者用于生成论文里的「Fig. 3」，
   后者用于检索；只存一个都会在某个环节出问题。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    """统一的约束命名规范。

    没有命名规范时，Postgres 会自动给约束起 ``figure_cite_key_key`` 这种名字，
    后续 autogenerate 会拿它和模型里的匿名约束比较，产生一堆假 diff。
    所以第一天就定死。
    """

    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class Paper(Base):
    """一篇文献（不含具体文件）。版本差异见 PaperVersion。"""

    __tablename__ = "paper"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    doi: Mapped[str | None] = mapped_column(String(255), index=True)
    arxiv_id: Mapped[str | None] = mapped_column(String(32), index=True)
    cite_key: Mapped[str | None] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    title_norm: Mapped[str] = mapped_column(String(512), default="", index=True)
    authors_json: Mapped[list] = mapped_column(JSON, default=list)
    year: Mapped[int | None] = mapped_column(Integer, index=True)
    venue: Mapped[str | None] = mapped_column(String(255))
    # 著录字段（2026-11-13）：生成 references 条目要用；Zotero 有就随导入带进来，
    # 没有就留空——格式器会省略缺失段并给出「缺字段」提示，不瞎编。
    volume: Mapped[str | None] = mapped_column(String(32))
    issue: Mapped[str | None] = mapped_column(String(32))
    pages: Mapped[str | None] = mapped_column(String(64))
    # 条目类型（迁移 0015，2026-11-15）：references 据此选 [J]/[C]/[D]/[M] 等分支。
    # journal|conference|preprint|thesis|book|report|other；旧数据默认 journal。
    # 手工上传的学位论文没有 Zotero 类型，可在工作台 PATCH 改成 thesis。
    entry_type: Mapped[str] = mapped_column(
        String(16), default="journal", server_default="journal"
    )
    # 学位论文著录字段（迁移 0015）：Zotero 的 university/thesisType/place；
    # 缺失时格式器省略并提示，不瞎编（GB/T 的「出版地: 学校」要用）。
    thesis_school: Mapped[str | None] = mapped_column(String(255))
    thesis_degree: Mapped[str | None] = mapped_column(String(255))
    thesis_place: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="new")  # new|parsed|failed

    # --- 知识库（M1）：元数据与 Zotero 只读镜像 ---------------------------------
    # Zotero 是「文献库主人」：这边只做只读导入（D1 决策），不回写 Zotero。
    abstract: Mapped[str | None] = mapped_column(Text)
    tags_json: Mapped[list] = mapped_column(JSON, default=list)
    reading_status: Mapped[str] = mapped_column(String(16), default="unread")  # unread|reading|done|shelved
    # 工作台（2026-11-12 UI 改版）：用户自己的备注与收藏。
    # note 完全属于用户，不进检索/摘要 prompt；favorite 用于「收藏夹」这个虚拟视图。
    note: Mapped[str | None] = mapped_column(Text)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    zotero_item_key: Mapped[str | None] = mapped_column(String(32), unique=True)
    metadata_source: Mapped[str] = mapped_column(String(16), default="upload")  # upload|zotero|arxiv
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 数据归属（M7 第二步，2026-11-15）：私有库只看 owner=我 的文献；
    # 旧数据为 NULL = legacy，仍按「全局库」可见（迁移 0014）。
    owner_user_id: Mapped[str | None] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    versions: Mapped[list["PaperVersion"]] = relationship(
        back_populates="paper", cascade="all, delete-orphan"
    )


class PaperVersion(Base):
    """一份具体的 PDF 文件。同一篇文献可以有 preprint / published / correction。"""

    __tablename__ = "paper_version"
    __table_args__ = (UniqueConstraint("file_sha256", name="uq_version_sha256"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    paper_id: Mapped[str] = mapped_column(ForeignKey("paper.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="uploaded")
    venue: Mapped[str | None] = mapped_column(String(255))
    year: Mapped[int | None] = mapped_column(Integer)
    doi: Mapped[str | None] = mapped_column(String(255))
    arxiv_id: Mapped[str | None] = mapped_column(String(32))
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    file_path: Mapped[str] = mapped_column(Text)
    page_count: Mapped[int | None] = mapped_column(Integer)
    parser: Mapped[str | None] = mapped_column(String(32))
    parser_version: Mapped[str | None] = mapped_column(String(32))
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 文字层质量（0–1，越高越可信）与判定依据；低分时 OCR 结果优先（见 docs/14）
    text_quality: Mapped[float | None] = mapped_column(Float)
    text_quality_flags_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    paper: Mapped[Paper] = relationship(back_populates="versions")
    blocks: Mapped[list["Block"]] = relationship(
        back_populates="version", cascade="all, delete-orphan"
    )
    figures: Mapped[list["Figure"]] = relationship(
        back_populates="version", cascade="all, delete-orphan"
    )


class Block(Base):
    """版面块，唯一真值。所有 AI 结论最终都要能落到某几个 block 上。"""

    __tablename__ = "block"
    __table_args__ = (Index("ix_block_version_page_order", "paper_version_id", "page", "order_idx"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    paper_version_id: Mapped[str] = mapped_column(
        ForeignKey("paper_version.id", ondelete="CASCADE"), index=True
    )
    page: Mapped[int] = mapped_column(Integer)
    bbox_json: Mapped[list] = mapped_column(JSON, default=list)  # [x0, y0, x1, y1]
    type: Mapped[str] = mapped_column(String(16))  # text|title|section|table|figure|caption|formula|ref
    text: Mapped[str] = mapped_column(Text, default="")
    order_idx: Mapped[int] = mapped_column(Integer, default=0)
    parent_id: Mapped[str | None] = mapped_column(String(32), index=True)
    engine: Mapped[str | None] = mapped_column(String(32))
    conf: Mapped[float | None] = mapped_column(Float)

    version: Mapped[PaperVersion] = relationship(back_populates="blocks")


class Figure(Base):
    """图。一等公民——不用从 paper 派生，独立可查、可打标签、可引用。"""

    __tablename__ = "figure"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    paper_version_id: Mapped[str] = mapped_column(
        ForeignKey("paper_version.id", ondelete="CASCADE"), index=True
    )
    block_id: Mapped[str | None] = mapped_column(String(32), index=True)

    page: Mapped[int] = mapped_column(Integer)
    bbox_json: Mapped[list] = mapped_column(JSON, default=list)
    image_path: Mapped[str | None] = mapped_column(Text)
    thumb_path: Mapped[str | None] = mapped_column(Text)

    label_raw: Mapped[str | None] = mapped_column(String(64))  # 原文写法：Fig. 3
    label_norm: Mapped[str | None] = mapped_column(String(16), index=True)  # fig:3
    label_num: Mapped[int | None] = mapped_column(Integer)
    sublabel: Mapped[str | None] = mapped_column(String(8))  # a / b / c

    caption: Mapped[str] = mapped_column(Text, default="")
    # 图注来源：parser=解析器原文；agent=AI 清洗后自动/人工采纳；human=人工编辑过。
    # M3b 自动接受器据此保证「用户改过的图注永不覆盖」。
    caption_source: Mapped[str] = mapped_column(
        String(16), default="parser", server_default="parser"
    )
    figure_type: Mapped[str | None] = mapped_column(String(16), index=True)
    license_hint: Mapped[str] = mapped_column(String(16), default="unknown")
    cite_key: Mapped[str] = mapped_column(String(64), unique=True)  # P0042v1-F3

    # 图号校核（labeling.py 规则层）：编号从哪来、有多可信、属于哪个子图组。
    # 低置信（< LOW_CONFIDENCE）只作候选，等 review 确认；不自动用于引用。
    label_source: Mapped[str | None] = mapped_column(String(16), index=True)
    label_confidence: Mapped[float | None] = mapped_column(Float)
    group_key: Mapped[str | None] = mapped_column(String(64), index=True)

    # 图的来源：parser = 解析器产出；manual = 用户在原文里框选手动添加。
    # 重解析时 manual 图要保留（见 workers/run.py 的快照/回填），否则用户加的图会被重新解析冲掉。
    origin: Mapped[str] = mapped_column(String(16), default="parser", server_default="parser")
    # 软删除：用户删掉的图不打物理行，重解析时按 cite_key 重新压回去；
    # 所有列表/检索/计数都过滤 deleted_at is null。
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    # 纠错通道：人工或 agent 对这张图定位的判定。
    # 它是「误报可接受」这个策略成立的前提 —— 错了要有人/有 agent 能说它错了，
    # 而且判定要沉淀下来（之后可以直接当 golden set 用）。
    review_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    review_note: Mapped[str | None] = mapped_column(Text)
    bbox_override_json: Mapped[list | None] = mapped_column(JSON)
    reviewed_by: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    version: Mapped[PaperVersion] = relationship(back_populates="figures")


class FigureProposal(Base):
    """AI 对一张「图提取」结果的纠错建议（M3）。

    只管「提议」，不直接改图：用户采纳后才写正式字段（图号/图注/复核状态），
    拒绝就留一条记录——被拒的提议也是数据，能看出模型哪类判断不可靠。

    ``proposed_json`` 形如::

        {
          "label_ok": false,
          "suggested_label": "图 4-50",
          "caption_issue": "ocr_noise",
          "cleaned_caption": "...",
          "crop_issue": "none",
          "mode": "vision" | "text",
          "summary": "一句话说明"
        }
    """

    __tablename__ = "figure_proposal"
    __table_args__ = (Index("ix_figure_proposal_status", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    figure_id: Mapped[str] = mapped_column(
        ForeignKey("figure.id", ondelete="CASCADE"), index=True
    )
    paper_version_id: Mapped[str] = mapped_column(
        ForeignKey("paper_version.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), default="figure_review")
    proposed_json: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence_json: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|accepted|rejected|superseded
    provider: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    prompt_version: Mapped[str] = mapped_column(String(32), default="")
    mode: Mapped[str] = mapped_column(String(16), default="text")  # vision|text
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decision_reason: Mapped[str | None] = mapped_column(Text)

    # --- M3 正式化：可回滚 + 自动接受标记 -----------------------------------
    # 采纳前被修改字段的快照（label_* / caption* / review_*）。回滚按它恢复，
    # 也是 audit_log 之外的第二份保险：proposal 自己就能说清「改了什么」。
    before_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    auto_accepted: Mapped[bool] = mapped_column(default=False, server_default="0")
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rolled_back_by: Mapped[str | None] = mapped_column(String(64))


class OcrProposal(Base):
    """OCR 与大模型校对不一致的页级提案（轻量 OCR 工作流）。

    OCR 结果先落成 Block（用户可直接阅读）；只有视觉模型认为有出入时才创建本提案。
    用户可接受 OCR、接受模型修订（替换该页 OCR 文字块）或拒绝；不自动改正文。
    """

    __tablename__ = "ocr_proposal"
    __table_args__ = (Index("ix_ocr_proposal_status_created_at", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    paper_version_id: Mapped[str] = mapped_column(
        ForeignKey("paper_version.id", ondelete="CASCADE"), index=True
    )
    page: Mapped[int] = mapped_column(Integer)
    ocr_text: Mapped[str] = mapped_column(Text, default="")
    model_text: Mapped[str] = mapped_column(Text, default="")
    diff_json: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(
        String(20), default="pending"
    )  # pending|accepted_ocr|accepted_model|rejected
    provider: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(64))


class AuditLog(Base):
    """正式字段变更审计（M3）：谁、对什么对象、改前/改后、证据、备注。

    只记录**已发生的正式写操作**（采纳/回滚/人工改图号等），不记录每次模型调用。
    不存文献正文：before/after 只留被修改字段（图号、图注、复核标记），摘要正文这类
    大文本用 hash/长度代替。
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_target", "target_type", "target_id"),
        Index("ix_audit_log_action_created", "action", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    actor: Mapped[str] = mapped_column(String(64), default="human")
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(32), default="figure")
    target_id: Mapped[str] = mapped_column(String(32), default="")
    before_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    evidence_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class AgentTask(Base):
    """agent 任务 + 租约（M3）：内置 worker 或外部 agent 领取、心跳、重试、超时回收。

    为什么不用现有 ``job`` 表：job 是解析流水线（毫秒级幂等、无租约概念）；
    agent 任务要「谁在跑、租约到什么时候、失败重试几次」，还要求可跨外部 agent。
    """

    __tablename__ = "agent_task"
    __table_args__ = (
        Index("ix_agent_task_status_created", "status", "priority", "created_at"),
        Index("ix_agent_task_target", "target_type", "target_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(64), index=True)  # figure_review|auto_accept_sweep
    target_type: Mapped[str] = mapped_column(String(32), default="figure")
    target_id: Mapped[str] = mapped_column(String(32), default="")
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)

    status: Mapped[str] = mapped_column(
        String(16), default="queued"
    )  # queued|running|done|failed|canceled
    priority: Mapped[int] = mapped_column(Integer, default=0)
    # 幂等键：同一张图的 review 任务重复排只留一条（外部 agent 重试也安全）
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True, index=True)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    actor: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text)
    cost: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TableBlock(Base):
    """表。之所以不并进 Block：结构化表要单独存 cell 结构，且经常需要二次 OCR。"""

    __tablename__ = "tab"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    paper_version_id: Mapped[str] = mapped_column(
        ForeignKey("paper_version.id", ondelete="CASCADE"), index=True
    )
    block_id: Mapped[str | None] = mapped_column(String(32), index=True)
    page: Mapped[int] = mapped_column(Integer)
    label_raw: Mapped[str | None] = mapped_column(String(64))
    label_norm: Mapped[str | None] = mapped_column(String(16), index=True)
    html: Mapped[str | None] = mapped_column(Text)
    cells_json: Mapped[list] = mapped_column(JSON, default=list)
    caption: Mapped[str] = mapped_column(Text, default="")
    quality: Mapped[str] = mapped_column(String(16), default="structured")  # structured|image


class Job(Base):
    """解析 / 抽取 / 导出任务。worker 幂等消费，可断点续跑。"""

    __tablename__ = "job"
    __table_args__ = (Index("ix_job_status_created", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32))  # parse|extract|export
    paper_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("paper_version.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Collection(Base):
    """知识库目录树节点（M1）。

    ``source=manual`` 是用户手建的；``source=zotero`` 是 Zotero 集合的只读镜像
    （`zotero_key` 保证重复导入幂等，不靠名字匹配——Zotero 里允许重名集合）。
    """

    __tablename__ = "collection"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255))
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("collection.id", ondelete="CASCADE"), index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual|zotero
    zotero_key: Mapped[str | None] = mapped_column(String(32), unique=True)
    # 目录也归个人：团队库 v1 是平铺文献列表，不做团队目录（docs/17 §8.1）
    owner_user_id: Mapped[str | None] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PaperCollection(Base):
    """文献 ↔ 知识库目录（多对多）。一篇文献可以同时属于多个集合。"""

    __tablename__ = "paper_collection"
    __table_args__ = (PrimaryKeyConstraint("paper_id", "collection_id"),)

    paper_id: Mapped[str] = mapped_column(
        ForeignKey("paper.id", ondelete="CASCADE"), index=True
    )
    collection_id: Mapped[str] = mapped_column(
        ForeignKey("collection.id", ondelete="CASCADE"), index=True
    )


class ImportRun(Base):
    """一次 Zotero / JSON 导入的审计记录（M1）。给 UI 显示「上次导入时间/结果」。"""

    __tablename__ = "import_run"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(16))  # json|sqlite|api
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class Summary(Base):
    """章节级摘要（M2）。

    设计要点（docs/15）：
    * 摘要挂在 ``paper_version`` 上——v1/v2 的章节文本不同，摘要不能跨版本复用；
    * **证据是硬约束**：``evidence_block_ids_json`` 必须非空且全部指向本版本的 block，
      校验不过不落库（服务端在 ``summaries.py`` 里强制）；
    * ``source=human_edited`` 是人工改过的文本，自动生成永不覆盖；``human_locked`` 同理；
    * 每次生成/编辑前把旧版本压进 ``summary_revision``，可回滚。
    """

    __tablename__ = "summary"
    __table_args__ = (
        UniqueConstraint("paper_version_id", "section_key", name="uq_summary_version_section"),
        Index("ix_summary_version_status", "paper_version_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    paper_version_id: Mapped[str] = mapped_column(
        ForeignKey("paper_version.id", ondelete="CASCADE"), index=True
    )
    # 章节标识：由「标题 + 起始位置」算出的稳定哈希（同一版本内幂等）
    section_key: Mapped[str] = mapped_column(String(64))
    section_title: Mapped[str] = mapped_column(String(255), default="")
    level: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[str] = mapped_column(Text, default="")
    key_points_json: Mapped[list] = mapped_column(JSON, default=list)
    evidence_block_ids_json: Mapped[list] = mapped_column(JSON, default=list)

    # 模型自评置信度（0–1）：自动接受（D3）按它过阈值，事后也能拿来做阈值校准。
    # 老数据/人工编辑后允许为空。
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    source: Mapped[str] = mapped_column(String(16), default="agent")  # agent|human_edited
    provider: Mapped[str] = mapped_column(String(16), default="mock")
    model: Mapped[str] = mapped_column(String(64), default="")
    prompt_version: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft|published

    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)

    # 自动接受（D3）：服务端按证据/置信策略自动发布时置 True，便于审计与回滚定位
    auto_accepted: Mapped[bool] = mapped_column(default=False)
    # 人工锁定：用户编辑或显式锁定后，自动生成不再覆盖（除非显式 force）
    human_locked: Mapped[bool] = mapped_column(default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    edited_by: Mapped[str | None] = mapped_column(String(64))
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    revisions: Mapped[list["SummaryRevision"]] = relationship(
        back_populates="summary", cascade="all, delete-orphan"
    )


class SummaryRevision(Base):
    """摘要的历史版本：生成/编辑/回滚前各压一份，可回滚、可审计。"""

    __tablename__ = "summary_revision"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    summary_id: Mapped[str] = mapped_column(
        ForeignKey("summary.id", ondelete="CASCADE"), index=True
    )
    content: Mapped[str] = mapped_column(Text, default="")
    key_points_json: Mapped[list] = mapped_column(JSON, default=list)
    evidence_block_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="agent")
    provider: Mapped[str] = mapped_column(String(16), default="mock")
    model: Mapped[str] = mapped_column(String(64), default="")
    prompt_version: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16), default="draft")
    reason: Mapped[str] = mapped_column(String(32), default="generated")  # generated|edited|regenerated|restored
    edited_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    summary: Mapped[Summary] = relationship(back_populates="revisions")


class Claim(Base):
    """写作侧素材卡片（M5）：一句可引用的论断 + 它赖以成立的证据。

    设计约束（docs/15 §4 M5）：

    * **证据硬约束**：``evidence_json`` 至少一条，且每条都要能在库里解析到
      （block / figure / table / summary），解析失败不落库——写作侧不能比阅读侧松；
    * 一条 claim 只归属一篇 paper（跨文献综合句留给后续的「综合卡」），
      导出时按 paper 的 ``cite_key`` 生成 ``\\cite{}``；
    * ``status``：draft（还在写）/ approved（已认可，导出默认含全部但 UI 可筛）；
    * ``source``：human 手写 / agent 由 MCP 提案落地，便于审计。
    """

    __tablename__ = "claim"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    paper_id: Mapped[str] = mapped_column(
        ForeignKey("paper.id", ondelete="CASCADE"), index=True
    )
    # 证据属于哪个版本：写作引用必须指向你实际读到的那一版（同 figure/block 的设计）
    paper_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("paper_version.id", ondelete="SET NULL"), index=True, nullable=True
    )
    text: Mapped[str] = mapped_column(Text, default="")
    # 导出时按这个分组（「引言」「方法」「讨论」…）；空串归入「正文」
    section: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft|approved
    source: Mapped[str] = mapped_column(String(16), default="human")  # human|agent
    evidence_json: Mapped[list] = mapped_column(JSON, default=list)
    # 重解析后有证据映射不回来时置位：卡片保留，但提示用户证据已失效需重选
    evidence_stale: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    tags_json: Mapped[list] = mapped_column(JSON, default=list)
    order_idx: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str | None] = mapped_column(String(64))
    # 写作卡片归个人（docs/17 §8.1）；团队共享写作卡留给后续
    owner_user_id: Mapped[str | None] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

class ClaimRevision(Base):
    """claim 的历史版本（2026-11-15 小修）：每次真正改内容前压一份，可回滚。

    只记录**用户可见的编辑字段**（文字/章节/状态/标签/证据/排序/证据版本），
    不记录 owner、时间戳等元数据；恢复会把快照写回 claim 本身，
    恢复动作本身也会先压一份当前状态（reason=restored），所以历史链不会断。
    """

    __tablename__ = "claim_revision"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    claim_id: Mapped[str] = mapped_column(
        ForeignKey("claim.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column(Text, default="")
    section: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="draft")
    source: Mapped[str] = mapped_column(String(16), default="human")
    evidence_json: Mapped[list] = mapped_column(JSON, default=list)
    tags_json: Mapped[list] = mapped_column(JSON, default=list)
    order_idx: Mapped[int] = mapped_column(Integer, default=0)
    # 证据属于哪一版（快照时一起留，恢复后 claim 与证据仍指向同一版）
    paper_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str] = mapped_column(String(16), default="edited")  # edited|restored
    edited_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class AppUser(Base):
    """账号（2026-11-15，团队共享第一步）：本地账号 + 管理员/成员两种角色。

    为什么先只做账号、不直接上工作组：工作组/共享都依赖「我是谁」；
    身份与数据归属分开落库，后面加 workgroup / share 表时不用改这里的语义。
    密码只存 scrypt 哈希（auth.py），明文永不落库、不进日志。
    """

    __tablename__ = "app_user"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64), default="")
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(16), default="member")  # admin|member
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserSession(Base):
    """登录会话（服务端 session，不签 JWT）。

    自部署、单进程/小团队场景下，服务端 session 更容易「立刻踢下线」；
    浏览器只拿到随机 token，库里只存 sha256，过期时间可查。
    """

    __tablename__ = "user_session"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), index=True
    )
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# 工作组与共享（M7 第二步，迁移 0014）
#
# 规则（用户 2026-11-15 拍板，docs/17 §8.1）：每人的私有库只归个人；
# 「团队库」不是另一套数据，只是**别人主动共享到工作组**的文献视图。
# 因此没有「团队目录 / 团队笔记」，共享是文献级，图/表/摘要随文献可见性。
# ---------------------------------------------------------------------------
class Workgroup(Base):
    """工作组：成员之间共享文献的最小单位。"""

    __tablename__ = "workgroup"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WorkgroupMember(Base):
    """工作组成员；``role=owner`` 可以加人/移除成员，``member`` 只能看共享内容。"""

    __tablename__ = "workgroup_member"
    __table_args__ = (PrimaryKeyConstraint("workgroup_id", "user_id"),)

    workgroup_id: Mapped[str] = mapped_column(
        ForeignKey("workgroup.id", ondelete="CASCADE")
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16), default="member")  # owner|member
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PaperShare(Base):
    """文献 → 工作组的共享关系（联合主键：一篇文献在同一个组只共享一次）。

    共享人只用于显示「由谁共享」；删除共享后对方立即看不到（查询实时 join）。
    """

    __tablename__ = "paper_share"
    __table_args__ = (PrimaryKeyConstraint("paper_id", "workgroup_id"),)

    paper_id: Mapped[str] = mapped_column(
        ForeignKey("paper.id", ondelete="CASCADE")
    )
    workgroup_id: Mapped[str] = mapped_column(
        ForeignKey("workgroup.id", ondelete="CASCADE"), index=True
    )
    shared_by: Mapped[str | None] = mapped_column(String(32), index=True)
    shared_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserPaperState(Base):
    """用户对一篇文献的私有状态：笔记 / 收藏 / 阅读状态（按用户隔离）。

    认证打开后，``paper.note/favorite/reading_status`` 只剩 legacy/单机语义；
    所有用户的读写都走这张表，避免别人的私有笔记互相覆盖。
    """

    __tablename__ = "user_paper_state"
    __table_args__ = (PrimaryKeyConstraint("user_id", "paper_id"),)

    user_id: Mapped[str] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE")
    )
    paper_id: Mapped[str] = mapped_column(
        ForeignKey("paper.id", ondelete="CASCADE"), index=True
    )
    note: Mapped[str | None] = mapped_column(Text)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    reading_status: Mapped[str] = mapped_column(String(16), default="unread")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
