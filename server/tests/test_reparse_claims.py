# SPDX-License-Identifier: AGPL-3.0-or-later
"""重解析不能丢写作卡证据（SOTA 审查 F14）。

persist 是「先删后插」：block / figure / table 全换新 UUID。claim 的证据锚在旧 id 上：

1. 同内容重解析：block / figure / table / summary 证据全部重锚到新行，卡片不标 stale；
2. 证据在新解析结果里找不到：剔除失配证据并标 ``evidence_stale``，**卡片永不删除**
   （人工内容不丢）；部分失配时保留能映射的部分；
3. summary 证据指向的 agent 摘要因失配被删时，卡片同样标 stale 而不是悬空；
4. 用户重新提交证据后 stale 标记清除。
"""

from __future__ import annotations

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.adapters.base import ParsedBlock, ParsedDocument, ParsedFigure, ParsedTable
from litbench.config import settings
from litbench.models import (
    Base,
    Block,
    Claim,
    Figure,
    Paper,
    PaperVersion,
    Summary,
    TableBlock,
)
from litbench.workers.run import persist
from litbench.writing import update_claim


def _fk_on_engine(url: str):
    engine = create_async_engine(url, poolclass=NullPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _pragma(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


_SHARED_TEXT = "本章介绍相变传热的基本规律与工程计算方法，重点讨论凝结与沸腾。"


def _doc(*, with_figure: bool, extra_text: str | None = None) -> ParsedDocument:
    raw = [
        (1, 0, "section", "第 7 章 相变传热"),
        (1, 1, "text", _SHARED_TEXT),
        (2, 2, "text", extra_text or "膜状凝结换热系数由 Nusselt 理论给出，是工程设计的常用基准。"),
    ]
    return ParsedDocument(
        page_count=2,
        engine="docling",
        engine_version="test",
        blocks=[
            ParsedBlock(
                page=page, bbox=[10.0, 10.0, 500.0, 40.0], type=btype,
                text=content, order_idx=order,
            )
            for page, order, btype, content in raw
        ],
        figures=[
            ParsedFigure(
                page=2, bbox=[10.0, 300.0, 200.0, 500.0],
                label_raw="图 7-1", caption="图 7-1 凝结模式示意", figure_type="diagram",
            )
        ] if with_figure else [],
        tables=[
            ParsedTable(
                page=2, bbox=[10.0, 500.0, 400.0, 600.0],
                html="<table><tr><td>h=100</td></tr></table>",
                cells=[["h=100"]], caption="表 7-1 换热系数对比",
            )
        ] if with_figure else [],
        text_layer=True,
    )


def _doc_a() -> ParsedDocument:
    return _doc(with_figure=True)


def _doc_b() -> ParsedDocument:
    """文本 / 图 / 表与 doc_a 完全不重合：旧证据一个都映射不回来。"""
    return _doc(with_figure=False, extra_text="这是一段与旧解析结果完全不同的正文。")


@pytest.fixture
async def session(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    engine = _fk_on_engine(f"sqlite+aiosqlite:///{(tmp_path / 'reparse_claim.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        paper = Paper(title="重解析写作卡测试", status="parsed")
        s.add(paper)
        await s.flush()
        version = PaperVersion(
            paper_id=paper.id,
            kind="preprint",
            file_sha256="c" * 64,
            file_path="pdfs/reparse-claim.pdf",
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


def _block_entry(block: Block) -> dict:
    return {
        "type": "block", "id": block.id, "page": block.page,
        "label": f"p{block.page} {block.type}", "snippet": (block.text or "")[:120],
    }


def _figure_entry(figure: Figure) -> dict:
    return {
        "type": "figure", "id": figure.id, "page": figure.page,
        "label": figure.label_raw or "图（未编号）", "snippet": (figure.caption or "")[:120],
    }


def _table_entry(table: TableBlock) -> dict:
    return {
        "type": "table", "id": table.id, "page": table.page,
        "label": table.label_raw or "表（未编号）", "snippet": (table.caption or "")[:120],
    }


async def test_claim_evidence_reanchors_after_reparse(session) -> None:
    s, version = session
    await persist(s, version, _doc_a())
    await s.commit()

    old_blocks = await _blocks(s, version.id)
    old_figure = (await _rows(s, Figure, version.id))[0]
    old_table = (await _rows(s, TableBlock, version.id))[0]
    old_block_ids = {b.id for b in old_blocks}

    summary = Summary(
        paper_version_id=version.id,
        section_key="sec-1",
        section_title="第 7 章 相变传热",
        level=1,
        content="摘要正文。",
        key_points_json=[],
        evidence_block_ids_json=[old_blocks[1].id],
        source="agent",
        provider="fake",
        model="m",
        prompt_version="v1",
        status="published",
    )
    s.add(summary)
    await s.flush()

    claim = Claim(
        paper_id=version.paper_id,
        paper_version_id=version.id,
        text="膜状凝结是工程设计的依据，换热系数由 Nusselt 理论给出。",
        evidence_json=[
            _block_entry(old_blocks[1]),
            _figure_entry(old_figure),
            _table_entry(old_table),
            {"type": "summary", "id": summary.id, "page": None,
             "label": summary.section_title, "snippet": "摘要正文。"},
        ],
        source="human",
    )
    # legacy 卡：paper_version_id 为 NULL 也要被重锚覆盖
    legacy = Claim(
        paper_id=version.paper_id,
        paper_version_id=None,
        text="旧版写作卡，没有记录证据版本。",
        evidence_json=[_block_entry(old_blocks[2])],
        source="human",
    )
    s.add_all([claim, legacy])
    await s.commit()
    claim_id, legacy_id, summary_id = claim.id, legacy.id, summary.id

    await persist(s, version, _doc_a())
    await s.commit()

    new_blocks = await _blocks(s, version.id)
    new_block_ids = {b.id for b in new_blocks}
    assert new_block_ids.isdisjoint(old_block_ids)
    new_figure = (await _rows(s, Figure, version.id))[0]
    new_table = (await _rows(s, TableBlock, version.id))[0]
    assert new_figure.id != old_figure.id
    assert new_table.id != old_table.id

    saved = await s.get(Claim, claim_id)
    assert saved is not None
    assert saved.text == "膜状凝结是工程设计的依据，换热系数由 Nusselt 理论给出。"
    assert saved.evidence_stale is False
    by_type = {item["type"]: item for item in saved.evidence_json}
    assert set(by_type) == {"block", "figure", "table", "summary"}
    assert by_type["block"]["id"] in new_block_ids
    assert by_type["figure"]["id"] == new_figure.id
    assert by_type["table"]["id"] == new_table.id
    assert by_type["summary"]["id"] == summary_id  # summary id 重解析稳定

    saved_legacy = await s.get(Claim, legacy_id)
    assert saved_legacy is not None
    assert saved_legacy.evidence_stale is False
    assert saved_legacy.evidence_json[0]["id"] in new_block_ids


async def test_lost_evidence_marks_stale_and_keeps_claim(session) -> None:
    s, version = session
    await persist(s, version, _doc_a())
    await s.commit()

    old_blocks = await _blocks(s, version.id)
    old_figure = (await _rows(s, Figure, version.id))[0]
    full_loss = Claim(
        paper_id=version.paper_id,
        paper_version_id=version.id,
        text="人工写下的论断，重解析绝不能删。",
        evidence_json=[_block_entry(old_blocks[2]), _figure_entry(old_figure)],
        source="human",
    )
    partial = Claim(
        paper_id=version.paper_id,
        paper_version_id=version.id,
        text="部分证据能映射回来的论断。",
        evidence_json=[_block_entry(old_blocks[1]), _figure_entry(old_figure)],
        source="agent",
    )
    s.add_all([full_loss, partial])
    await s.commit()
    full_id, partial_id = full_loss.id, partial.id

    # doc_b 保留了 _SHARED_TEXT（partial 的第一条证据），其余全失配
    await persist(s, version, _doc_b())
    await s.commit()

    saved_full = await s.get(Claim, full_id)
    assert saved_full is not None, "证据全失配也不能删卡（人工内容不丢）"
    assert saved_full.text == "人工写下的论断，重解析绝不能删。"
    assert saved_full.evidence_json == []
    assert saved_full.evidence_stale is True

    saved_partial = await s.get(Claim, partial_id)
    assert saved_partial is not None
    assert saved_partial.evidence_stale is True
    assert len(saved_partial.evidence_json) == 1
    entry = saved_partial.evidence_json[0]
    assert entry["type"] == "block"
    new_block_ids = {b.id for b in await _blocks(s, version.id)}
    assert entry["id"] in new_block_ids


async def test_dropped_summary_evidence_marks_claim_stale(session) -> None:
    s, version = session
    await persist(s, version, _doc_a())
    await s.commit()

    old_blocks = await _blocks(s, version.id)
    summary = Summary(
        paper_version_id=version.id,
        section_key="sec-1",
        section_title="第 7 章 相变传热",
        level=1,
        content="证据会失配的 agent 摘要。",
        key_points_json=[],
        evidence_block_ids_json=[old_blocks[2].id],
        source="agent",
        provider="fake",
        model="m",
        prompt_version="v1",
        status="published",
        human_locked=False,
    )
    s.add(summary)
    await s.flush()
    claim = Claim(
        paper_id=version.paper_id,
        paper_version_id=version.id,
        text="引用摘要作为证据的论断。",
        evidence_json=[
            {"type": "summary", "id": summary.id, "page": None,
             "label": summary.section_title, "snippet": summary.content[:120]},
        ],
        source="human",
    )
    s.add(claim)
    await s.commit()
    claim_id, summary_id = claim.id, summary.id

    await persist(s, version, _doc_b())
    await s.commit()

    assert await s.get(Summary, summary_id) is None  # agent 摘要按既有规则被删
    saved = await s.get(Claim, claim_id)
    assert saved is not None
    assert saved.evidence_json == []
    assert saved.evidence_stale is True
    assert saved.text == "引用摘要作为证据的论断。"


async def test_update_claim_clears_stale_flag(session) -> None:
    s, version = session
    await persist(s, version, _doc_a())
    await s.commit()

    old_blocks = await _blocks(s, version.id)
    claim = Claim(
        paper_id=version.paper_id,
        paper_version_id=version.id,
        text="证据失效后由用户重新选证的论断。",
        evidence_json=[_block_entry(old_blocks[2])],
        source="human",
    )
    s.add(claim)
    await s.commit()
    claim_id = claim.id

    await persist(s, version, _doc_b())
    await s.commit()
    saved = await s.get(Claim, claim_id)
    assert saved is not None and saved.evidence_stale is True

    new_blocks = await _blocks(s, version.id)
    result = await update_claim(
        s, claim_id, evidence=[{"type": "block", "id": new_blocks[1].id}]
    )
    assert result["evidence_stale"] is False
    assert result["evidence"][0]["id"] == new_blocks[1].id