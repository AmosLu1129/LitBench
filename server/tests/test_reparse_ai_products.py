# SPDX-License-Identifier: AGPL-3.0-or-later
"""重解析（先删后插）不能丢 AI 产物：摘要证据 / 历史 / 图纠错提案要重锚到新 id。

覆盖三件事：

1. 同内容重解析：summary（evidence、section_key）、summary_revision、figure_proposal
   全部指向新行（旧行 id 是另一批 UUID）；
2. 旧证据在新解析结果里按文本找不到：agent 摘要被删除且不报错，人工锁定的保留；
3. SQLite 连接确实开了 ``PRAGMA foreign_keys``（不然 dev 里 ondelete=CASCADE 全是摆设，
   ``persist`` 删 figure 前必须先处理 proposal 这条修复也就没法验证）。

测试库刻意开 FK：这样「先删 proposal 再删 figure」的时序错了会直接 IntegrityError，
而不是悄悄通过。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench import db as db_mod
from litbench.adapters.base import ParsedBlock, ParsedDocument, ParsedFigure
from litbench.auto_accept import apply_proposal, rollback_proposal
from litbench.config import settings
from litbench.models import (
    Base,
    Block,
    Figure,
    FigureProposal,
    Paper,
    PaperVersion,
    Summary,
    SummaryRevision,
)
from litbench.sections import build_sections
from litbench.workers.run import persist


def _fk_on_engine(url: str):
    """测试引擎复刻生产 SQLite 的 FK 语义（db.py 的 connect 事件在测试里不生效）。"""
    engine = create_async_engine(url, poolclass=NullPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _pragma(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def _doc_a() -> ParsedDocument:
    """一版有明确章节和图号的解析结果（docling 风格：标题块 + 正文块）。"""
    raw = [
        (1, 0, "section", "第 7 章 相变传热"),
        (1, 1, "text", "本章介绍相变传热的基本规律与工程计算方法，重点讨论凝结与沸腾。"),
        (1, 2, "text", "相变过程伴随潜热释放，是工程换热设备设计的核心问题之一。"),
        (2, 3, "section", "7.1 凝结传热的模式"),
        (2, 4, "text", "凝结分为珠状凝结与膜状凝结两种基本形式，膜状凝结是工程设计的依据。"),
        (2, 5, "text", "膜状凝结换热系数由 Nusselt 理论给出，是工程设计的常用基准。"),
    ]
    return ParsedDocument(
        page_count=2,
        engine="docling",
        engine_version="test",
        blocks=[
            ParsedBlock(
                page=page,
                bbox=[10.0, 10.0, 500.0, 40.0],
                type=btype,
                text=content,
                order_idx=order,
            )
            for page, order, btype, content in raw
        ],
        figures=[
            ParsedFigure(
                page=2,
                bbox=[10.0, 300.0, 200.0, 500.0],
                label_raw="图 7-1",
                caption="图 7-1 凝结模式示意",
                figure_type="diagram",
            )
        ],
        tables=[],
        text_layer=True,
    )


def _doc_b() -> ParsedDocument:
    """文本与 doc_a 完全不重合：旧摘要的证据按 (page, text) 一个都映射不过去。"""
    raw = [
        (1, 0, "section", "第 7 章 相变传热"),
        (1, 1, "text", "这是一段与旧解析结果完全不同的正文，用来制造证据块失配的场景。"),
        (1, 2, "text", "重解析后的文字与旧摘要证据毫无重合，任何旧 id 都不应该被接回来。"),
    ]
    return ParsedDocument(
        page_count=1,
        engine="docling",
        engine_version="test",
        blocks=[
            ParsedBlock(
                page=page,
                bbox=[10.0, 10.0, 500.0, 40.0],
                type=btype,
                text=content,
                order_idx=order,
            )
            for page, order, btype, content in raw
        ],
        figures=[],
        tables=[],
        text_layer=True,
    )


@pytest.fixture
async def session(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    engine = _fk_on_engine(f"sqlite+aiosqlite:///{(tmp_path / 'reparse.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        paper = Paper(title="重解析 AI 产物测试", status="parsed")
        s.add(paper)
        await s.flush()
        version = PaperVersion(
            paper_id=paper.id,
            kind="preprint",
            file_sha256="b" * 64,
            file_path="pdfs/reparse.pdf",
            page_count=2,
        )
        s.add(version)
        await s.commit()
        yield s, version
    await engine.dispose()


async def _rows(db, model, version_id):
    stmt = select(model).where(model.paper_version_id == version_id)
    return (await db.execute(stmt)).scalars().all()


async def _blocks(db, version_id):
    stmt = (
        select(Block)
        .where(Block.paper_version_id == version_id)
        .order_by(Block.page, Block.order_idx)
    )
    return (await db.execute(stmt)).scalars().all()


async def test_reparse_reanchors_summary_revision_and_proposal(session) -> None:
    s, version = session
    await persist(s, version, _doc_a())
    await s.commit()

    # --- 旧解析结果：第一代 block / figure id --------------------------------
    old_blocks = await _blocks(s, version.id)
    old_figures = await _rows(s, Figure, version.id)
    assert len(old_figures) == 1
    old_figure = old_figures[0]
    old_block_ids = {row.id for row in old_blocks}

    old_sections = build_sections(
        old_blocks,
        page_count=version.page_count,
        figure_pages=[old_figure.page],
        parser=version.parser,
    )
    section = old_sections[0]
    old_evidence = list(section.block_ids[:2])
    assert old_evidence

    summary = Summary(
        paper_version_id=version.id,
        section_key=section.key,
        section_title=section.title,
        level=section.level,
        content="重解析前写下的摘要正文。",
        key_points_json=["要点一", "要点二"],
        evidence_block_ids_json=old_evidence,
        source="agent",
        provider="fake",
        model="model-a",
        prompt_version="v1",
        status="published",
        tokens_in=11,
        tokens_out=22,
        cost=0.001,
        confidence=0.92,
        auto_accepted=True,
        human_locked=False,
    )
    s.add(summary)
    await s.flush()
    revision = SummaryRevision(
        summary_id=summary.id,
        content="更早的历史内容。",
        key_points_json=["旧要点"],
        evidence_block_ids_json=list(old_evidence),
        confidence=0.8,
        source="agent",
        provider="fake",
        model="model-a",
        prompt_version="v1",
        status="draft",
        reason="generated",
    )
    s.add(revision)
    proposal = FigureProposal(
        figure_id=old_figure.id,
        paper_version_id=version.id,
        kind="figure_review",
        proposed_json={"suggested_label": "图 7-1", "summary": "定位正确"},
        evidence_json=[{"type": "candidate_label", "value": "图 7-1"}],
        confidence=0.77,
        status="accepted",
        provider="fake",
        model="model-b",
        prompt_version="v2",
        mode="vision",
        tokens_in=100,
        tokens_out=30,
        cost=0.002,
        latency_ms=1200,
        decided_at=datetime.now(timezone.utc),
        decided_by="human",
        decision_reason="人工采纳",
    )
    s.add(proposal)
    await s.commit()

    old_summary_id = summary.id
    old_revision_id = revision.id
    old_proposal_id = proposal.id
    old_figure_id = old_figure.id
    old_cite_key = old_figure.cite_key

    # --- 同内容重解析：先删后插，新行全是新 UUID -----------------------------
    await persist(s, version, _doc_a())
    await s.commit()

    new_blocks = await _blocks(s, version.id)
    new_block_ids = {row.id for row in new_blocks}
    assert new_block_ids.isdisjoint(old_block_ids), "重解析应产生全新 block id"
    new_figures = await _rows(s, Figure, version.id)
    assert len(new_figures) == 1
    new_figure = new_figures[0]
    assert new_figure.id != old_figure_id, "重解析应产生全新 figure id"
    assert new_figure.cite_key == old_cite_key  # cite_key 是重解析的稳定身份

    # 摘要：证据全部指向新 block，章节键 = 新章节树里同一节的 key
    saved_summary = await s.get(Summary, old_summary_id)
    assert saved_summary is not None
    assert saved_summary.evidence_block_ids_json
    assert set(saved_summary.evidence_block_ids_json) <= new_block_ids
    assert set(saved_summary.evidence_block_ids_json).isdisjoint(old_block_ids)
    assert saved_summary.content == "重解析前写下的摘要正文。"
    assert saved_summary.key_points_json == ["要点一", "要点二"]
    assert saved_summary.human_locked is False

    new_sections = build_sections(
        new_blocks,
        page_count=version.page_count,
        figure_pages=[new_figure.page],
        parser=version.parser,
    )
    matched = [
        item
        for item in new_sections
        if item.title == section.title and item.level == section.level
    ]
    assert matched, "重解析后应能按标题找回同一节"
    assert saved_summary.section_key == matched[0].key
    assert saved_summary.section_title == matched[0].title

    # 历史版本：证据同样重映射
    saved_revision = await s.get(SummaryRevision, old_revision_id)
    assert saved_revision is not None
    assert set(saved_revision.evidence_block_ids_json) <= new_block_ids
    assert saved_revision.content == "更早的历史内容。"

    # 提案：按 cite_key 重建一条，指向新 figure，状态/决策/元数据不变
    proposals = await _rows(s, FigureProposal, version.id)
    assert len(proposals) == 1
    saved_proposal = proposals[0]
    assert saved_proposal.id != old_proposal_id
    assert saved_proposal.figure_id == new_figure.id
    assert saved_proposal.figure_id != old_figure_id
    assert saved_proposal.status == "accepted"
    assert saved_proposal.decided_by == "human"
    assert saved_proposal.decision_reason == "人工采纳"
    assert saved_proposal.proposed_json == {"suggested_label": "图 7-1", "summary": "定位正确"}
    assert saved_proposal.tokens_in == 100
    assert saved_proposal.tokens_out == 30
    assert saved_proposal.mode == "vision"
    assert saved_proposal.prompt_version == "v2"


async def test_unmatched_agent_summary_dropped_locked_summary_kept(session) -> None:
    s, version = session
    await persist(s, version, _doc_a())
    await s.commit()

    old_blocks = await _blocks(s, version.id)
    old_figures = await _rows(s, Figure, version.id)
    old_sections = build_sections(
        old_blocks,
        page_count=version.page_count,
        figure_pages=[row.page for row in old_figures],
        parser=version.parser,
    )
    assert len(old_sections) >= 2, "需要两节来放两条摘要（section_key 唯一）"
    evidence_agent = list(old_sections[0].block_ids[:1])
    evidence_locked = list(old_sections[1].block_ids[:1])
    old_figure = old_figures[0]
    assert evidence_agent and evidence_locked

    agent_summary = Summary(
        paper_version_id=version.id,
        section_key=old_sections[0].key,
        section_title=old_sections[0].title,
        level=old_sections[0].level,
        content="证据会失配、应当被删除的 agent 摘要。",
        key_points_json=[],
        evidence_block_ids_json=evidence_agent,
        source="agent",
        provider="fake",
        model="model-a",
        prompt_version="v1",
        status="published",
        human_locked=False,
    )
    locked_summary = Summary(
        paper_version_id=version.id,
        section_key=old_sections[1].key,
        section_title=old_sections[1].title,
        level=old_sections[1].level,
        content="人工改过的摘要，重解析绝不能丢。",
        key_points_json=["人工要点"],
        evidence_block_ids_json=evidence_locked,
        source="human_edited",
        provider="fake",
        model="model-a",
        prompt_version="v1",
        status="published",
        human_locked=True,
        edited_by="human",
    )
    s.add_all([agent_summary, locked_summary])
    await s.flush()
    agent_summary_id = agent_summary.id
    locked_summary_id = locked_summary.id
    s.add_all(
        [
            SummaryRevision(
                summary_id=agent_summary_id,
                content="agent 摘要的历史。",
                key_points_json=[],
                evidence_block_ids_json=evidence_agent,
                source="agent",
                provider="fake",
                model="model-a",
                prompt_version="v1",
                status="draft",
                reason="generated",
            ),
            SummaryRevision(
                summary_id=locked_summary_id,
                content="人工摘要的历史。",
                key_points_json=["人工要点"],
                evidence_block_ids_json=evidence_locked,
                source="human_edited",
                provider="fake",
                model="model-a",
                prompt_version="v1",
                status="published",
                reason="edited",
            ),
        ]
    )
    await s.commit()

    # 图号在新解析结果里不存在时，提案明确失效（snapshot 里记着 cite_key，跳过即可）。
    s.add(
        FigureProposal(
            figure_id=old_figure.id,
            paper_version_id=version.id,
            kind="figure_review",
            proposed_json={"summary": "旧图号的提案"},
            evidence_json=[],
            status="pending",
            provider="fake",
            model="model-b",
            prompt_version="v2",
        )
    )
    await s.commit()

    # --- 重解析：文本完全不同，旧证据一个都映射不回来 -----------------------
    await persist(s, version, _doc_b())
    await s.commit()

    remaining = (await s.execute(select(Summary))).scalars().all()
    assert {row.id for row in remaining} == {locked_summary_id}, (
        "agent 摘要应被删除、人工摘要应保留"
    )
    locked = remaining[0]
    assert locked.human_locked is True
    assert locked.content == "人工改过的摘要，重解析绝不能丢。"
    assert locked.key_points_json == ["人工要点"]
    assert locked.evidence_block_ids_json == [], "映射不上的证据应剔除，不能留悬空 id"

    agent_revisions = (
        await s.execute(
            select(SummaryRevision).where(SummaryRevision.summary_id == agent_summary_id)
        )
    ).scalars().all()
    assert agent_revisions == [], "摘要被删后它的历史也要一起删"
    locked_revisions = (
        await s.execute(
            select(SummaryRevision).where(SummaryRevision.summary_id == locked_summary_id)
        )
    ).scalars().all()
    assert len(locked_revisions) == 1
    assert locked_revisions[0].evidence_block_ids_json == []

    proposals = await _rows(s, FigureProposal, version.id)
    assert proposals == [], "图号在新结果里失效时提案跳过，不能留一条悬空外键"


async def test_paper_level_summary_keeps_reserved_key(session) -> None:
    """整篇速览（section_key=__paper__）不对应章节树：只迁证据，不能被章节匹配打散。"""
    s, version = session
    await persist(s, version, _doc_a())
    await s.commit()

    old_blocks = await _blocks(s, version.id)
    assert old_blocks
    paper_summary = Summary(
        paper_version_id=version.id,
        section_key="__paper__",
        section_title="整篇速览",
        level=0,
        content="整篇速览正文。",
        key_points_json=["全文要点"],
        evidence_block_ids_json=[old_blocks[1].id, old_blocks[3].id],
        source="agent",
        provider="fake",
        model="model-a",
        prompt_version="v0",
        status="published",
        human_locked=False,
    )
    s.add(paper_summary)
    await s.commit()
    summary_id = paper_summary.id
    old_evidence = list(paper_summary.evidence_block_ids_json)
    assert len(old_evidence) == 2

    await persist(s, version, _doc_a())
    await s.commit()

    saved = await s.get(Summary, summary_id)
    assert saved is not None
    assert saved.section_key == "__paper__", "整篇速览不能被章节匹配打回某一节"
    assert saved.section_title == "整篇速览"
    assert saved.level == 0
    assert saved.content == "整篇速览正文。"
    assert saved.evidence_block_ids_json
    new_block_ids = {row.id for row in await _blocks(s, version.id)}
    assert set(saved.evidence_block_ids_json) <= new_block_ids
    assert set(saved.evidence_block_ids_json).isdisjoint(old_evidence)


async def test_accepted_proposal_rollback_survives_reparse(session) -> None:
    """F15：重解析不能丢 before_json / auto_accepted / rolled_back_*，否则回滚永远失败。"""
    s, version = session
    await persist(s, version, _doc_a())
    await s.commit()

    old_figure = (await _rows(s, Figure, version.id))[0]
    original_label = old_figure.label_raw
    proposal = FigureProposal(
        figure_id=old_figure.id,
        paper_version_id=version.id,
        kind="figure_review",
        proposed_json={"suggested_label": "图 7-2"},
        evidence_json=[{"type": "candidate_label", "value": "图 7-2"}],
        confidence=0.9,
        status="pending",
        provider="fake",
        model="model-b",
        prompt_version="v2",
    )
    s.add(proposal)
    await s.flush()
    apply_proposal(s, proposal, old_figure, actor="human")
    await s.commit()
    assert old_figure.label_raw == "图 7-2"
    assert proposal.before_json

    # 同内容重解析：提案重建到新 figure，回滚快照必须跟着迁过来
    await persist(s, version, _doc_a())
    await s.commit()
    new_figure = (await _rows(s, Figure, version.id))[0]
    proposals = await _rows(s, FigureProposal, version.id)
    assert len(proposals) == 1
    saved = proposals[0]
    assert saved.figure_id == new_figure.id
    assert saved.status == "accepted"
    assert saved.auto_accepted is False
    assert saved.before_json, "before_json 丢了，一键回滚就永远报「没有 before 快照」"

    rolled, fig, _applied = await rollback_proposal(s, saved.id, actor="human")
    await s.commit()
    assert fig.label_raw == original_label
    assert rolled.status == "rolled_back"
    assert rolled.rolled_back_by == "human"
    assert rolled.rolled_back_at is not None

    # 再重解析一次：已回滚的状态与审计字段也要原样保留
    await s.refresh(rolled)  # 以库里的时间表示为准（SQLite 不存时区）
    rolled_at = rolled.rolled_back_at
    await persist(s, version, _doc_a())
    await s.commit()
    proposals = await _rows(s, FigureProposal, version.id)
    assert len(proposals) == 1
    assert proposals[0].status == "rolled_back"
    assert proposals[0].rolled_back_by == "human"
    assert proposals[0].rolled_back_at == rolled_at
    assert proposals[0].before_json


async def test_sqlite_connection_enables_foreign_keys(tmp_path, monkeypatch) -> None:
    """db.py 的 connect 事件必须真的打开 foreign_keys，否则 ondelete=CASCADE 在 dev 失效。"""
    await db_mod.dispose_engine()
    monkeypatch.setattr(
        settings, "database_url", f"sqlite+aiosqlite:///{(tmp_path / 'fk.db').as_posix()}"
    )
    engine = db_mod.get_engine()
    try:
        async with engine.connect() as conn:
            value = (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
        assert value == 1
    finally:
        await db_mod.dispose_engine()
