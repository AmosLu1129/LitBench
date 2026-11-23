# SPDX-License-Identifier: AGPL-3.0-or-later
"""worker 自动链路：无文字层 → 轻量 OCR → 视觉校验 → 提案/结果。

不加载真实模型：parser、页面 OCR、视觉校验都替换为确定性结果。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.adapters.base import ParsedBlock, ParsedDocument
from litbench.config import settings
from litbench.lite_ocr import LiteOcrLine, LiteOcrPage
from litbench.models import Base, Block, Job, OcrProposal, Paper, PaperVersion
from litbench.ocr_cross_validate import OcrCrossCheck
from litbench.workers import run as worker


class _FakeParser:
    name = "pdfium"

    async def parse(self, _pdf_path):
        return ParsedDocument(
            page_count=1,
            engine="pdfium",
            engine_version="fake",
            blocks=[],
            figures=[],
            tables=[],
            text_layer=False,
        )


@pytest.fixture
async def session_factory(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    (storage / "pdfs").mkdir(parents=True)
    pdf = storage / "pdfs" / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% fake\n")
    monkeypatch.setattr(settings, "storage_dir", storage)
    monkeypatch.setattr(settings, "parser_backend", "pdfium")
    monkeypatch.setattr(settings, "lite_ocr_enabled", True)
    monkeypatch.setattr(settings, "ocr_cross_validate", True)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'worker.db').as_posix()}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_worker_lite_ocr_creates_proposal_on_difference(
    session_factory, monkeypatch
) -> None:
    Session = session_factory
    async with Session() as session:
        session.add(Paper(id="a" * 32, title="扫描报告", title_norm="扫描报告"))
        session.add(
            PaperVersion(
                id="b" * 32,
                paper_id="a" * 32,
                file_sha256="c" * 64,
                file_path="pdfs/scan.pdf",
                page_count=1,
            )
        )
        session.add(Job(id="d" * 32, kind="parse", paper_version_id="b" * 32, status="running"))
        await session.commit()

    monkeypatch.setattr(worker, "get_parser", lambda *a, **kw: _FakeParser())
    monkeypatch.setattr(worker.lite_ocr, "lite_ocr_available", lambda: True)
    monkeypatch.setattr(
        worker.lite_ocr,
        "ocr_missing_pages",
        lambda *a, **kw: [
            LiteOcrPage(
                page=1,
                width=595,
                height=842,
                lines=[LiteOcrLine(text="OCR 读数", bbox=[10, 20, 100, 30], confidence=0.9)],
            )
        ],
    )
    monkeypatch.setattr(
        worker.ocr_cross_validate,
        "cross_check_pages",
        lambda *a, **kw: _async_return(
            [
                (
                    1,
                    OcrCrossCheck(
                        same=False,
                        model_text="OCR 读数（模型修订）",
                        differences=[{"ocr": "OCR 读数", "model": "OCR 读数（模型修订）"}],
                        provider="fake",
                        model="fake-vision",
                    ),
                )
            ]
        ),
    )

    async with Session() as session:
        job = await session.get(Job, "d" * 32)
        await worker.handle(session, job)

    async with Session() as session:
        blocks = (
            await session.execute(select(Block).where(Block.paper_version_id == "b" * 32))
        ).scalars().all()
        assert [block.text for block in blocks] == ["OCR 读数"]
        assert blocks[0].engine == "liteocr"
        proposals = (
            await session.execute(
                select(OcrProposal).where(OcrProposal.paper_version_id == "b" * 32)
            )
        ).scalars().all()
        assert len(proposals) == 1
        assert proposals[0].ocr_text == "OCR 读数"
        assert proposals[0].model_text == "OCR 读数（模型修订）"
        assert proposals[0].status == "pending"


async def _async_return(value):
    return value
