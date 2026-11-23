# SPDX-License-Identifier: AGPL-3.0-or-later
"""修正框 → 缩略图重裁的测试。

这是「拖框修正」闭环的后端一半：用户画了一个对的框，overlay 立刻对；
如果卡片上还是原来那张裁错的图，用户会以为没生效。所以 review 带 bbox 时要
**按新框重裁一次缩略图**，并且 image_url 要带版本号（否则浏览器 24h 缓存拿旧图）。
"""

from __future__ import annotations

import io

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.config import settings
from litbench.db import get_session
from litbench.main import app
from litbench.models import Base, Figure, Paper, PaperVersion
from pdf_fixture import make_pdf

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# make_pdf 里那个矢量矩形：PDF 原生 y=520..650（页高 792）→ 左上原点 142..272
FIX_BOX = [100.0, 142.0, 400.0, 272.0]


@pytest.fixture
async def client(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    (storage / "pdfs").mkdir(parents=True)
    (storage / "pdfs" / "p.pdf").write_bytes(make_pdf())
    monkeypatch.setattr(settings, "storage_dir", storage)

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as s:
        paper = Paper(title="Thumb paper", status="parsed")
        s.add(paper)
        await s.flush()
        version = PaperVersion(
            paper_id=paper.id,
            kind="preprint",
            file_sha256="b" * 64,
            file_path="pdfs/p.pdf",
        )
        s.add(version)
        await s.flush()
        fig = Figure(
            paper_version_id=version.id,
            page=1,
            cite_key="thumb-v1-fig1",
            label_raw="Fig. 1",
            label_norm="fig:1",
            bbox_json=[0.0, 0.0, 10.0, 10.0],  # 解析给了一个明显错的框
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
        ac.storage = storage  # type: ignore[attr-defined]
        ac.session_factory = Session  # type: ignore[attr-defined]
        yield ac
    app.dependency_overrides.clear()
    await engine.dispose()


async def test_review_with_bbox_rerenders_thumbnail(client) -> None:
    r = await client.post(
        f"/api/figures/{client.fig_id}/review",
        json={"verdict": "wrong", "bbox": FIX_BOX, "note": "拖框修正", "reviewer": "human"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["bbox_override"] == FIX_BOX
    # 缓存版本号：不带的话浏览器会一直显示 24h 缓存里的旧图
    assert body["image_url"], "重裁后必须有 image_url"
    assert "?v=" in body["image_url"]

    image_path = client.storage / "images" / "thumb-v1-fig1.png"
    assert image_path.is_file(), "修正后应该重新裁一张缩略图"
    data = image_path.read_bytes()
    assert data.startswith(PNG_MAGIC)

    # 300x130 pt @150dpi ≈ 625x271 px
    image = Image.open(io.BytesIO(data))
    assert abs(image.width - 625) <= 4, image.size
    assert abs(image.height - 271) <= 4, image.size
    # 修正框里要有墨迹：空白说明裁到了页面的另一半（坐标原点错）
    assert image.convert("L").getextrema()[0] < 100, "重裁出来的缩略图是空白"

    # 库里的路径必须是相对 storage 根的（换挂载点不用改库）
    async with client.session_factory() as s:  # type: ignore[attr-defined]
        fig = (await s.execute(select(Figure))).scalars().one()
    assert fig.image_path == "images/thumb-v1-fig1.png"

    # 接口能拿到同一张图
    img = await client.get(f"/api/figures/{client.fig_id}/image")
    assert img.status_code == 200
    assert img.content == data


async def test_review_without_bbox_keeps_thumbnail(client) -> None:
    """只判「确认 / 判错」不该动缩略图（重裁只在给了新框时发生）。"""
    r = await client.post(
        f"/api/figures/{client.fig_id}/review", json={"verdict": "ok", "reviewer": "human"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["image_url"] is None or "?v=" in body["image_url"]
    assert not (client.storage / "images" / "thumb-v1-fig1.png").exists()


async def test_rerender_failure_does_not_break_review(client, monkeypatch) -> None:
    """PDF 丢了也不能让纠错请求失败——修正框本身仍然要落库。"""
    (client.storage / "pdfs" / "p.pdf").unlink()

    r = await client.post(
        f"/api/figures/{client.fig_id}/review",
        json={"verdict": "wrong", "bbox": FIX_BOX, "reviewer": "human"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bbox_override"] == FIX_BOX
    assert body["image_url"] is None
