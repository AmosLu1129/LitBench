# SPDX-License-Identifier: AGPL-3.0-or-later
"""工作台改版（2026-11-12）后端测试：备注 / 收藏 / 正文速览。

守三条：
1. PATCH 只动 note/favorite/reading_status，不改 Zotero 镜像元数据；
2. 「收藏夹」是 favorite 过滤（不是特殊集合），列表/详情/筛选一致；
3. 正文速览按章节组织、只返回有文字的块，且 404 语义清楚（没有解析版本）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.config import settings
from litbench.db import get_session
from litbench.main import app
from litbench.models import Base, Block, Collection, Figure, Paper, PaperCollection, PaperVersion


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'ws.db').as_posix()}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with Session() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        ac.session_factory = Session  # type: ignore[attr-defined]
        yield ac
    app.dependency_overrides.clear()
    await engine.dispose()


async def _seed(client, *, with_version: bool = True) -> dict:
    Session = client.session_factory
    async with Session() as s:
        paper = Paper(
            title="测试文献：正文速览",
            title_norm="测试文献正文速览",
            status="parsed",
            abstract="这是一条摘要。",
            metadata_source="upload",
        )
        s.add(paper)
        await s.flush()
        version = None
        if with_version:
            version = PaperVersion(
                paper_id=paper.id,
                kind="uploaded",
                file_sha256=uuid.uuid4().hex * 2,
                file_path="pdfs/ws.pdf",
                page_count=3,
                parser="docling",
                parsed_at=datetime.now(timezone.utc),
            )
            s.add(version)
            await s.flush()
            raw = [
                (1, 0, "section", "1. 引言"),
                (1, 1, "text", "这是引言第一段，讲研究背景。"),
                (2, 2, "section", "2. 方法"),
                (2, 3, "text", "这是方法第一段。"),
                (2, 4, "caption", "图 2-1 方法示意"),
                (3, 5, "text", "   "),
            ]
            for page, order, btype, text in raw:
                s.add(
                    Block(
                        paper_version_id=version.id,
                        page=page,
                        order_idx=order,
                        bbox_json=[10.0, 20.0, 300.0, 40.0],
                        type=btype,
                        text=text,
                    )
                )
        await s.commit()
        return {"paper_id": paper.id, "version_id": version.id if version else None}


async def test_patch_note_and_favorite(client) -> None:
    seed = await _seed(client)
    pid = seed["paper_id"]
    r = await client.patch(
        f"/api/papers/{pid}",
        json={"note": "重点看第 3 节的实验设置", "favorite": True, "reading_status": "reading"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["note"] == "重点看第 3 节的实验设置"
    assert data["favorite"] is True
    assert data["reading_status"] == "reading"

    detail = (await client.get(f"/api/papers/{pid}")).json()
    assert detail["note"] == "重点看第 3 节的实验设置"
    assert detail["favorite"] is True

    # 只传 favorite 不影响 note
    r2 = await client.patch(f"/api/papers/{pid}", json={"favorite": False})
    assert r2.status_code == 200
    assert r2.json()["note"] == "重点看第 3 节的实验设置"
    assert r2.json()["favorite"] is False

    bad = await client.patch(f"/api/papers/{pid}", json={"reading_status": "nope"})
    assert bad.status_code == 422


async def test_favorite_filter(client) -> None:
    a = await _seed(client)
    b = await _seed(client)
    await client.patch(f"/api/papers/{a['paper_id']}", json={"favorite": True})

    fav = (await client.get("/api/papers", params={"favorite": "true"})).json()
    assert [p["id"] for p in fav] == [a["paper_id"]]
    non = (await client.get("/api/papers", params={"favorite": "false"})).json()
    assert [p["id"] for p in non] == [b["paper_id"]]
    all_papers = (await client.get("/api/papers")).json()
    assert len(all_papers) == 2


async def test_paper_text_by_sections(client) -> None:
    seed = await _seed(client)
    text = (await client.get(f"/api/papers/{seed['paper_id']}/text")).json()
    assert text["version_id"] == seed["version_id"]
    assert text["block_count"] == 3  # 空白块被过滤（引言 1 + 方法正文/图注 2）
    titles = [s["title"] for s in text["sections"]]
    assert titles == ["1. 引言", "2. 方法"]
    first = text["sections"][0]
    assert first["blocks"][0]["text"] == "这是引言第一段，讲研究背景。"
    assert first["blocks"][0]["bbox"] == [10.0, 20.0, 300.0, 40.0]
    assert text["sections"][1]["blocks"][-1]["type"] == "caption"


async def test_paper_text_without_version_is_404(client) -> None:
    seed = await _seed(client, with_version=False)
    r = await client.get(f"/api/papers/{seed['paper_id']}/text")
    assert r.status_code == 404
    assert "先上传" in r.json()["detail"]


async def test_figures_filter_by_favorite_and_collection(client) -> None:
    """「全部文献 / 收藏夹 / 知识树」共用的图表列表：按 favorite / collection 过滤。"""
    Session = client.session_factory
    async with Session() as s:
        fav = Paper(title="收藏论文", title_norm="收藏论文", status="parsed", favorite=True)
        plain = Paper(title="普通论文", title_norm="普通论文", status="parsed")
        s.add_all([fav, plain])
        await s.flush()
        fav_ver = PaperVersion(
            paper_id=fav.id,
            kind="uploaded",
            file_sha256=uuid.uuid4().hex * 2,
            file_path="pdfs/fav.pdf",
            page_count=1,
            parser="pdfium",
            parsed_at=datetime.now(timezone.utc),
        )
        plain_ver = PaperVersion(
            paper_id=plain.id,
            kind="uploaded",
            file_sha256=uuid.uuid4().hex * 2,
            file_path="pdfs/plain.pdf",
            page_count=1,
            parser="pdfium",
            parsed_at=datetime.now(timezone.utc),
        )
        s.add_all([fav_ver, plain_ver])
        await s.flush()
        fav_fig = Figure(
            paper_version_id=fav_ver.id,
            page=1,
            bbox_json=[0, 0, 10, 10],
            label_raw="图 1-1",
            label_norm="fig:1-1",
            caption="收藏论文的图",
            cite_key="fav-v1-f1",
        )
        plain_fig = Figure(
            paper_version_id=plain_ver.id,
            page=1,
            bbox_json=[0, 0, 10, 10],
            label_raw="图 2-1",
            label_norm="fig:2-1",
            caption="普通论文的图",
            cite_key="plain-v1-f1",
        )
        s.add_all([fav_fig, plain_fig])
        col = Collection(name="分组 A", source="manual")
        s.add(col)
        await s.flush()
        s.add(PaperCollection(paper_id=plain.id, collection_id=col.id))
        await s.commit()
        fav_fig_id, plain_fig_id, col_id = fav_fig.id, plain_fig.id, col.id

    by_fav = (await client.get("/api/figures", params={"favorite": "true"})).json()
    assert [i["figure_id"] for i in by_fav["items"]] == [fav_fig_id]
    assert by_fav["total"] == 1

    by_col = (await client.get("/api/figures", params={"collection_id": col_id})).json()
    assert [i["figure_id"] for i in by_col["items"]] == [plain_fig_id]

    # 收藏夹 + 分组 = 交集（收藏论文不在这个分组里 → 空）
    empty = (
        await client.get("/api/figures", params={"favorite": "true", "collection_id": col_id})
    ).json()
    assert empty["items"] == [] and empty["total"] == 0
