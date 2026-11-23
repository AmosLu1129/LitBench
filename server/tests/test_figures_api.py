# SPDX-License-Identifier: AGPL-3.0-or-later
"""接口层测试。用 SQLite + aiosqlite 顶掉 Postgres，不需要起任何外部服务。

    cd server && python -m pytest -q
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.config import settings
from litbench.db import get_session
from litbench.labels import normalize_title
from litbench.main import app
from litbench.models import Base, Figure, Paper, PaperVersion

DOI = "10.1016/j.fuel.2019.115000"


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", tmp_path)

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
        poolclass=NullPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as s:
        paper = Paper(
            title="Coal pyrolysis kinetics",
            doi=DOI,
            year=2019,
            authors_json=["Wang", "Li"],
            venue="Fuel",
            status="parsed",
        )
        paper.title_norm = normalize_title(paper.title)
        s.add(paper)
        await s.flush()

        version = PaperVersion(
            paper_id=paper.id,
            kind="published",
            file_sha256="a" * 64,
            file_path="/tmp/paper.pdf",
        )
        s.add(version)
        await s.flush()

        s.add_all(
            [
                Figure(
                    paper_version_id=version.id,
                    page=4,
                    label_raw="Fig. 3",
                    label_norm="fig:3",
                    label_num=3,
                    caption="TG curves at 10 K/min",
                    figure_type="chart",
                    license_hint="redraw",
                    cite_key="p1-v1-fig3",
                ),
                Figure(
                    paper_version_id=version.id,
                    page=5,
                    label_raw="Fig. 4",
                    label_norm="fig:4",
                    label_num=4,
                    caption="XRD patterns of chars",
                    figure_type="chart",
                    license_hint="redraw",
                    cite_key="p1-v1-fig4",
                ),
            ]
        )
        await s.commit()

    async def _session():
        async with Session() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
    await engine.dispose()


async def test_health(client) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_provenance_by_doi_and_label(client) -> None:
    r = await client.get("/api/figures", params={"paper": DOI, "label": "Fig. 3"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "provenance"
    assert body["degraded"] is False
    assert body["total"] == 1
    item = body["items"][0]
    assert item["cite_key"] == "p1-v1-fig3"
    assert item["label_raw"] == "Fig. 3"


async def test_provenance_from_free_query(client) -> None:
    """用户直接输 "Wang 2019 Fig. 3"，一步到位。"""
    r = await client.get("/api/figures", params={"q": "Wang 2019 Fig. 3"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "provenance"
    assert body["total"] == 1
    assert body["items"][0]["label_norm"] == "fig:3"


async def test_provenance_paper_only_returns_all_figures(client) -> None:
    r = await client.get("/api/figures", params={"paper": DOI})
    body = r.json()
    assert body["mode"] == "provenance"
    assert body["total"] == 2


async def test_missing_label_returns_empty_with_hint(client) -> None:
    """确定性检索找不到就是 0 条——绝不退化成「给你几个相似的」。"""
    r = await client.get("/api/figures", params={"paper": DOI, "label": "Fig. 9"})
    body = r.json()
    assert body["mode"] == "provenance"
    assert body["total"] == 0
    assert "版本" in (body["hint"] or "")


async def test_semantic_uses_bm25(client) -> None:
    """M9.1：语义路径从 ILIKE 换成 BM25，不再是 degraded。"""
    r = await client.get("/api/figures", params={"q": "TG curves"})
    body = r.json()
    assert body["mode"] == "semantic"
    assert body["degraded"] is False
    assert body["engine"] == "bm25"
    assert body["total"] == 1
    assert body["items"][0]["caption"] == "TG curves at 10 K/min"

    # 多词查询：两个词都在 XRD 图注里，应排在第一
    ranked = (await client.get("/api/figures", params={"q": "XRD patterns chars"})).json()
    assert ranked["engine"] == "bm25"
    assert ranked["items"][0]["label_norm"] == "fig:4"

    # 子串片段（旧 LIKE 能命中的）继续命中，避免升级后回退
    partial = (await client.get("/api/figures", params={"q": "XRD patt"})).json()
    assert partial["total"] == 1
    assert partial["items"][0]["label_norm"] == "fig:4"

    miss = (await client.get("/api/figures", params={"q": "zzzz-no-such-term"})).json()
    assert miss["total"] == 0
    assert miss["engine"] == "bm25"
    assert "BM25" in (miss["hint"] or "")


async def test_upload_and_dedupe(client, tmp_path) -> None:
    pdf = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"

    r1 = await client.post("/api/papers", files={"file": ("a.pdf", pdf, "application/pdf")})
    assert r1.status_code == 201, r1.text
    b1 = r1.json()
    assert b1["deduplicated"] is False
    assert len(b1["sha256"]) == 64
    assert (tmp_path / "pdfs" / b1["sha256"][:2] / f"{b1['sha256']}.pdf").exists()

    r2 = await client.post("/api/papers", files={"file": ("b.pdf", pdf, "application/pdf")})
    assert r2.status_code == 201, r2.text
    b2 = r2.json()
    assert b2["deduplicated"] is True
    assert b2["paper_id"] == b1["paper_id"]


async def test_paper_list_has_counts(client) -> None:
    r = await client.get("/api/papers")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert items[0]["figure_count"] == 2
