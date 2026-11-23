# SPDX-License-Identifier: AGPL-3.0-or-later
"""纠错通道的测试。

背景：解析器有误报是可接受的（多智能体协作里还有一道纠错门限），
但前提是**错了要能被指出来**，而且判定要落库 —— 之后可以当 golden set 用。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.db import get_session
from litbench.main import app
from litbench.models import Base, Figure, Paper, PaperVersion


@pytest.fixture
async def client(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as s:
        paper = Paper(title="Review paper", status="parsed")
        s.add(paper)
        await s.flush()
        version = PaperVersion(
            paper_id=paper.id, kind="preprint", file_sha256="f" * 64, file_path="/tmp/x.pdf"
        )
        s.add(version)
        await s.flush()
        fig = Figure(
            paper_version_id=version.id,
            page=3,
            cite_key="rv-v1-fig2",
            label_raw="Fig. 2",
            label_norm="fig:2",
            caption="mis-detected region",
            bbox_json=[10.0, 20.0, 30.0, 40.0],
        )
        s.add(fig)
        await s.commit()
        fig_id = fig.id

    async def _session():
        async with Session() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        ac.fig_id = fig_id  # type: ignore[attr-defined]
        yield ac
    app.dependency_overrides.clear()
    await engine.dispose()


async def test_default_review_status_is_pending(client) -> None:
    r = await client.get(f"/api/figures/{client.fig_id}")
    assert r.status_code == 200
    assert r.json()["review_status"] == "pending"


async def test_mark_ok(client) -> None:
    r = await client.post(
        f"/api/figures/{client.fig_id}/review",
        json={"verdict": "ok", "reviewer": "agent"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["review_status"] == "ok"
    assert body["bbox"] == [10.0, 20.0, 30.0, 40.0]


async def test_mark_wrong_with_bbox_override(client) -> None:
    r = await client.post(
        f"/api/figures/{client.fig_id}/review",
        json={"verdict": "wrong", "note": "框住了正文", "bbox": [1, 2, 300, 400], "reviewer": "human"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["review_status"] == "wrong"
    assert body["review_note"] == "框住了正文"
    assert body["bbox"] == [1.0, 2.0, 300.0, 400.0]
    assert body["bbox_override"] == [1.0, 2.0, 300.0, 400.0]


async def test_invalid_bbox_rejected(client) -> None:
    r = await client.post(
        f"/api/figures/{client.fig_id}/review",
        json={"verdict": "wrong", "bbox": [10, 10, 5, 5]},
    )
    assert r.status_code == 422


async def test_invalid_verdict_rejected(client) -> None:
    r = await client.post(
        f"/api/figures/{client.fig_id}/review",
        json={"verdict": "maybe"},
    )
    assert r.status_code == 422


async def test_filter_by_review_status(client) -> None:
    await client.post(f"/api/figures/{client.fig_id}/review", json={"verdict": "wrong"})
    r = await client.get("/api/figures", params={"review": "wrong"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["figure_id"] == client.fig_id

    r2 = await client.get("/api/figures", params={"review": "ok"})
    assert r2.json()["total"] == 0
