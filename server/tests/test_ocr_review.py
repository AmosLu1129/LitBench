# SPDX-License-Identifier: AGPL-3.0-or-later
"""OCR 校对提案 API：列表 / 接受模型修订 / 幂等保护。"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.db import get_session
from litbench.main import app
from litbench.models import Base, Block, OcrProposal, Paper, PaperVersion


@pytest.fixture
async def client(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'ocr.db').as_posix()}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with Session() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        ac.session_factory = Session  # type: ignore[attr-defined]
        yield ac
    app.dependency_overrides.clear()
    await engine.dispose()


async def _seed(client) -> tuple[str, str, str]:
    paper_id = "a" * 32
    version_id = "b" * 32
    proposal_id = "c" * 32
    Session = client.session_factory  # type: ignore[attr-defined]
    async with Session() as session:
        session.add(
            Paper(id=paper_id, title="扫描报告", title_norm="扫描报告", cite_key="local:ocr-test")
        )
        session.add(
            PaperVersion(
                id=version_id,
                paper_id=paper_id,
                file_sha256="f" * 64,
                file_path="pdfs/ocr-test.pdf",
                page_count=2,
            )
        )
        session.add_all(
            [
                Block(
                    paper_version_id=version_id,
                    page=1,
                    bbox_json=[10, 20, 100, 30],
                    type="text",
                    text="OCR 第一行",
                    order_idx=0,
                    engine="liteocr",
                ),
                Block(
                    paper_version_id=version_id,
                    page=1,
                    bbox_json=[10, 35, 120, 45],
                    type="text",
                    text="OCR 第二行",
                    order_idx=1,
                    engine="liteocr",
                ),
                Block(
                    paper_version_id=version_id,
                    page=1,
                    bbox_json=[10, 50, 120, 60],
                    type="caption",
                    text="图 1 实验装置",
                    order_idx=2,
                    engine="pdfium",
                ),
            ]
        )
        session.add(
            OcrProposal(
                id=proposal_id,
                paper_version_id=version_id,
                page=1,
                ocr_text="OCR 第一行\nOCR 第二行",
                model_text="OCR 第一行\nOCR 第二行（模型修订）",
                diff_json=[{"ocr": "OCR 第二行", "model": "OCR 第二行（模型修订）"}],
                status="pending",
                provider="fake",
                model="fake-vision",
            )
        )
        await session.commit()
    return paper_id, version_id, proposal_id


async def test_list_and_accept_model_replaces_page_ocr_blocks(client) -> None:
    paper_id, version_id, proposal_id = await _seed(client)
    listed = await client.get(f"/api/papers/{paper_id}/ocr-proposals")
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["page"] == 1
    assert rows[0]["differences"]

    decided = await client.post(
        f"/api/ocr-proposals/{proposal_id}/decide", json={"action": "accept_model"}
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "accepted_model"

    Session = client.session_factory  # type: ignore[attr-defined]
    async with Session() as session:
        blocks = (
            await session.execute(
                select(Block).where(Block.paper_version_id == version_id).order_by(Block.order_idx)
            )
        ).scalars().all()
        # pdfium 的图注块保留；两行 OCR 被一条模型修订块替换
        by_engine = {block.engine: block for block in blocks}
        assert set(by_engine) == {"pdfium", "llm_cross_check"}
        assert by_engine["llm_cross_check"].text == "OCR 第一行\nOCR 第二行（模型修订）"

    again = await client.post(
        f"/api/ocr-proposals/{proposal_id}/decide", json={"action": "accept_ocr"}
    )
    assert again.status_code == 409


async def test_accept_ocr_keeps_existing_blocks(client) -> None:
    paper_id, version_id, proposal_id = await _seed(client)
    response = await client.post(
        f"/api/ocr-proposals/{proposal_id}/decide", json={"action": "accept_ocr"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted_ocr"
    Session = client.session_factory  # type: ignore[attr-defined]
    async with Session() as session:
        blocks = (
            await session.execute(
                select(Block).where(
                    Block.paper_version_id == version_id, Block.engine == "liteocr"
                )
            )
        ).scalars().all()
        assert len(blocks) == 2
