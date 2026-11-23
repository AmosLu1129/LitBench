# SPDX-License-Identifier: AGPL-3.0-or-later
"""图片接口的测试。

重点不是「能返回图片」，而是**只允许返回 storage 里的图片**：
把库里的路径直接变成文件读取，是布局类应用最典型的路径穿越入口。
"""

from __future__ import annotations

import base64

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.config import settings
from litbench.db import get_session
from litbench.main import app
from litbench.models import Base, Figure, Paper, PaperVersion

# 1x1 透明 PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    images = tmp_path / "images"
    images.mkdir()
    (images / "ok.png").write_bytes(PNG)
    outside = tmp_path / "secret.png"
    outside.write_bytes(PNG)

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as s:
        paper = Paper(title="Image endpoint paper", status="parsed")
        s.add(paper)
        await s.flush()
        version = PaperVersion(
            paper_id=paper.id, kind="published", file_sha256="c" * 64, file_path="/tmp/x.pdf"
        )
        s.add(version)
        await s.flush()
        s.add_all(
            [
                Figure(
                    paper_version_id=version.id, page=1, cite_key="p-v1-fig1",
                    label_raw="Fig. 1", label_norm="fig:1", caption="ok figure",
                    image_path=str(images / "ok.png"),
                ),
                Figure(
                    paper_version_id=version.id, page=2, cite_key="p-v1-fig2",
                    label_raw="Fig. 2", label_norm="fig:2", caption="no image",
                    image_path=None,
                ),
                Figure(
                    paper_version_id=version.id, page=3, cite_key="p-v1-fig3",
                    label_raw="Fig. 3", label_norm="fig:3", caption="traversal attempt",
                    image_path=str(outside),
                ),
            ]
        )
        await s.commit()
        ids = {
            fig.cite_key: fig.id
            for fig in (await s.execute(select(Figure))).scalars()
        }

    async def _session():
        async with Session() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        ac.figure_ids = ids  # type: ignore[attr-defined]
        yield ac
    app.dependency_overrides.clear()
    await engine.dispose()


async def test_image_ok(client) -> None:
    r = await client.get(f"/api/figures/{client.figure_ids['p-v1-fig1']}/image")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == PNG


async def test_missing_image_404(client) -> None:
    r = await client.get(f"/api/figures/{client.figure_ids['p-v1-fig2']}/image")
    assert r.status_code == 404


async def test_path_outside_storage_is_rejected(client) -> None:
    """库里被塞了 storaget 之外的路径时必须拒绝，而不是照读。"""
    r = await client.get(f"/api/figures/{client.figure_ids['p-v1-fig3']}/image")
    assert r.status_code == 403


async def test_detail_has_image_url(client) -> None:
    r = await client.get(f"/api/figures/{client.figure_ids['p-v1-fig1']}")
    assert r.status_code == 200
    body = r.json()
    assert body["image_url"].endswith("/image")
    assert body["label_raw"] == "Fig. 1"


async def test_paper_figures_endpoint(client) -> None:
    r = await client.get("/api/papers")
    paper_id = r.json()[0]["id"]
    r2 = await client.get(f"/api/papers/{paper_id}/figures")
    assert r2.status_code == 200
    assert len(r2.json()) == 3
