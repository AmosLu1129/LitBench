# SPDX-License-Identifier: AGPL-3.0-or-later
"""重解析时的「用户数据不丢」回归：

1. 用户手动加的图（origin=manual）在重解析后还在；
2. 用户删掉的图（软删除）重解析后仍是删除状态，不会复活。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.adapters.base import ParsedDocument, ParsedFigure
from litbench.config import settings
from litbench.models import Base, Figure, Paper, PaperVersion
from litbench.workers.run import persist


def one_figure_doc() -> ParsedDocument:
    return ParsedDocument(
        page_count=1,
        engine="pdfium",
        engine_version="test",
        blocks=[],
        figures=[
            ParsedFigure(
                page=1,
                bbox=[10, 10, 120, 120],
                label_raw="图 1",
                caption="图 1 测试图",
                image_bytes=None,
            )
        ],
    )


@pytest.fixture
async def session(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'persist.db').as_posix()}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        paper = Paper(title="重解析保留测试", status="parsed")
        s.add(paper)
        await s.flush()
        version = PaperVersion(
            paper_id=paper.id,
            kind="preprint",
            file_sha256="a" * 64,
            file_path="pdfs/x.pdf",
            page_count=1,
        )
        s.add(version)
        await s.commit()
        yield s, version
    await engine.dispose()


async def test_manual_figure_survives_reparse(session) -> None:
    s, version = session
    await persist(s, version, one_figure_doc())
    await s.commit()

    s.add(
        Figure(
            paper_version_id=version.id,
            page=1,
            bbox_json=[200, 200, 320, 320],
            label_raw="图 2",
            label_norm="fig:2",
            caption="用户框选添加的图",
            cite_key="man-test01",
            origin="manual",
            image_path="images/man-test01.png",
            label_source="review",
            label_confidence=1.0,
        )
    )
    await s.commit()

    # 模拟同版本重解析
    await persist(s, version, one_figure_doc())
    await s.commit()

    rows = (await s.execute(select(Figure).where(Figure.paper_version_id == version.id))).scalars().all()
    manual = [r for r in rows if r.cite_key == "man-test01"]
    assert len(manual) == 1
    assert manual[0].origin == "manual"
    assert manual[0].label_raw == "图 2"
    assert any(r.cite_key != "man-test01" for r in rows)


async def test_deleted_figure_stays_deleted_after_reparse(session) -> None:
    s, version = session
    await persist(s, version, one_figure_doc())
    await s.commit()

    parsed = (
        await s.execute(select(Figure).where(Figure.paper_version_id == version.id))
    ).scalars().first()
    parsed.deleted_at = datetime.now(timezone.utc)
    await s.commit()
    deleted_key = parsed.cite_key

    await persist(s, version, one_figure_doc())
    await s.commit()

    rows = (
        await s.execute(select(Figure).where(Figure.paper_version_id == version.id))
    ).scalars().all()
    again = [r for r in rows if r.cite_key == deleted_key]
    assert len(again) == 1
    assert again[0].deleted_at is not None
