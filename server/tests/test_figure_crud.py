# SPDX-License-Identifier: AGPL-3.0-or-later
"""用户增图 / 删图接口的测试（见 routers/figures.py 的 create/delete）。

覆盖：
1. 手动框选加图：从 PDF 页裁图 → 入库（origin=manual）→ 能在按文献列表和图片接口里取到；
2. 删图是软删除：详情 404、列表/计数不再出现；
3. 参数校验：框太小、页码越界、非法图号都 422。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.config import settings
from litbench.db import get_session
from litbench.main import app
from litbench.models import Base, Paper, PaperVersion

from pdf_fixture import make_pdf


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    (tmp_path / "pdfs").mkdir()
    (tmp_path / "pdfs" / "ok.pdf").write_bytes(make_pdf())

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        paper = Paper(title="手动增图测试", status="parsed")
        s.add(paper)
        await s.flush()
        s.add(
            PaperVersion(
                paper_id=paper.id,
                kind="preprint",
                file_sha256="f" * 64,
                file_path="pdfs/ok.pdf",
                page_count=1,
            )
        )
        await s.commit()
        paper_id = paper.id

    async def _session():
        async with Session() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        ac.paper_id = paper_id  # type: ignore[attr-defined]
        yield ac
    app.dependency_overrides.clear()
    await engine.dispose()


async def test_create_manual_figure_and_image(client) -> None:
    r = await client.post(
        "/api/figures",
        json={
            "paper_id": client.paper_id,
            "page": 1,
            "bbox": [100, 100, 300, 250],
            "label": "图 4-50a",
            "caption": "(a) 手动补的图",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["origin"] == "manual"
    assert body["label_norm"] == "fig:4-50a"
    assert body["sublabel"] == "a"
    assert body["image_url"]

    r = await client.get(body["image_url"])
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/")

    # 出现在「按文献列图」和检索里
    r = await client.get(f"/api/papers/{client.paper_id}/figures")
    assert any(item["figure_id"] == body["figure_id"] for item in r.json())
    r = await client.get("/api/figures", params={"q": "图 4-50a"})
    assert any(item["figure_id"] == body["figure_id"] for item in r.json()["items"])


async def test_delete_is_soft_and_hidden_everywhere(client) -> None:
    r = await client.post(
        "/api/figures",
        json={"paper_id": client.paper_id, "page": 1, "bbox": [100, 100, 300, 250]},
    )
    assert r.status_code == 201, r.text
    fid = r.json()["figure_id"]

    r = await client.delete(f"/api/figures/{fid}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True

    assert (await client.get(f"/api/figures/{fid}")).status_code == 404
    assert (await client.get(f"{'/api/figures/'}{fid}/image")).status_code == 404
    r = await client.get(f"/api/papers/{client.paper_id}/figures")
    assert all(item["figure_id"] != fid for item in r.json())
    r = await client.get("/api/papers")
    assert r.json()[0]["figure_count"] == 0
    assert (await client.delete(f"/api/figures/{fid}")).status_code == 404


async def test_create_validation(client) -> None:
    base = {"paper_id": client.paper_id, "page": 1, "bbox": [100, 100, 300, 250]}
    r = await client.post("/api/figures", json={**base, "bbox": [100, 100, 104, 250]})
    assert r.status_code == 422  # 短边 < 8pt

    r = await client.post("/api/figures", json={**base, "page": 3})
    assert r.status_code == 422  # 页码越界

    r = await client.post("/api/figures", json={**base, "label": "hello world"})
    assert r.status_code == 422  # 不像图号

    r = await client.post("/api/figures", json={**base, "bbox": [300, 250, 100, 100]})
    assert r.status_code == 422  # 框反了
