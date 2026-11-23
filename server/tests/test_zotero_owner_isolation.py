# SPDX-License-Identifier: AGPL-3.0-or-later
"""Zotero 导入的 owner 隔离（SOTA 审查 F12）+ 上传限流（F10）。

zotero_key / zotero_item_key 是全局唯一键，但 upsert 只能命中调用者自己的行：
成员 B 上传同一份 Zotero 导出，不许改写成员 A 的私有文献/集合，也不许给 A 的
文献注入 PDF 版本。superuser 与 can_edit_paper 同口径，可维护全部（含 legacy）。
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import litbench.db as dbmod
import litbench.zotero as zotero_mod
from litbench.config import settings
from litbench.db import get_session
from litbench.main import app
from litbench.models import Base, Collection, Paper, PaperVersion
from litbench.zotero import ZAttachment, ZCollection, ZItem


@pytest.fixture
async def team_client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'zot.db').as_posix()}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    old_engine, old_sessionmaker = dbmod._engine, dbmod._sessionmaker
    dbmod._engine = engine
    dbmod._sessionmaker = Session

    async def _session():
        async with Session() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        ac.session_factory = Session  # type: ignore[attr-defined]
        ac.tmp_path = tmp_path  # type: ignore[attr-defined]
        yield ac
    app.dependency_overrides.clear()
    dbmod._engine, dbmod._sessionmaker = old_engine, old_sessionmaker
    await engine.dispose()


async def _setup_admin(client: AsyncClient) -> dict:
    r = await client.post(
        "/api/auth/setup",
        json={"username": "boss", "password": "admin-pass-123", "display_name": "组长"},
    )
    assert r.status_code == 200, r.text
    return r.json()["user"]


async def _create_member(client: AsyncClient, username: str) -> dict:
    r = await client.post(
        "/api/users",
        json={
            "username": username,
            "password": "member-pass-123",
            "display_name": username,
            "role": "member",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _login(client: AsyncClient, username: str, password: str) -> None:
    r = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


async def _logout(client: AsyncClient) -> None:
    await client.post("/api/auth/logout")


def _fixture(item1_title: str = "Alice 的文献", col1_name: str = "Alice 的集合") -> dict:
    return {
        "collections": [
            {"key": "COL1", "data": {"name": col1_name, "parentCollection": False}},
            {"key": "COL2", "data": {"name": "子集", "parentCollection": "COL1"}},
        ],
        "items": [
            {
                "key": "ITEM1",
                "data": {
                    "itemType": "journalArticle",
                    "title": item1_title,
                    "DOI": "10.1000/alice",
                    "collections": ["COL2"],
                },
            },
            {
                "key": "ITEM2",
                "data": {"itemType": "book", "title": "Alice 的书"},
            },
        ],
    }


async def _upload(client: AsyncClient, payload: dict) -> dict:
    r = await client.post(
        "/api/import/zotero/file",
        files={
            "file": ("zotero.json", json.dumps(payload, ensure_ascii=False), "application/json")
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["stats"]


async def test_member_import_skips_foreign_rows(team_client) -> None:
    await _setup_admin(team_client)
    alice = await _create_member(team_client, "alice")
    await _create_member(team_client, "bob")

    await _login(team_client, "alice", "member-pass-123")
    stats_a = await _upload(team_client, _fixture())
    assert stats_a["papers_created"] == 2
    assert stats_a["collections_created"] == 2
    await _logout(team_client)

    await _login(team_client, "bob", "member-pass-123")
    evil = _fixture(item1_title="被 B 改写的标题", col1_name="被 B 改写的集合")
    evil["items"].append(
        {"key": "ITEM3", "data": {"itemType": "journalArticle", "title": "Bob 自己的文献"}}
    )
    stats_b = await _upload(team_client, evil)
    assert stats_b["papers_skipped_foreign"] == 2
    assert stats_b["collections_skipped_foreign"] == 2
    assert stats_b["papers_created"] == 1  # 只有 ITEM3 是新的
    assert stats_b["papers_updated"] == 0
    assert stats_b["collections_updated"] == 0
    assert stats_b["links_added"] == 0
    assert stats_b["links_removed"] == 0

    async with team_client.session_factory() as session:
        papers = (await session.execute(select(Paper))).scalars().all()
        by_key = {p.zotero_item_key: p for p in papers}
        assert by_key["ITEM1"].title == "Alice 的文献"
        assert by_key["ITEM1"].owner_user_id == alice["id"]
        assert by_key["ITEM2"].title == "Alice 的书"
        assert by_key["ITEM3"].owner_user_id != alice["id"]
        cols = (await session.execute(select(Collection))).scalars().all()
        col_by_key = {c.zotero_key: c for c in cols}
        assert col_by_key["COL1"].name == "Alice 的集合"
        assert col_by_key["COL1"].owner_user_id == alice["id"]
        assert col_by_key["COL2"].parent_id == col_by_key["COL1"].id


async def test_doi_fallback_only_matches_own_papers(team_client) -> None:
    await _setup_admin(team_client)
    alice = await _create_member(team_client, "alice")
    await _create_member(team_client, "bob")
    async with team_client.session_factory() as session:
        paper = Paper(
            title="Alice 私有（无 Zotero key）",
            title_norm="alice 私有",
            doi="10.1000/shared-doi",
            owner_user_id=alice["id"],
        )
        session.add(paper)
        await session.commit()
        alice_paper_id = paper.id

    await _login(team_client, "bob", "member-pass-123")
    stats = await _upload(
        team_client,
        {
            "collections": [],
            "items": [
                {
                    "key": "NEWKEY",
                    "data": {
                        "itemType": "journalArticle",
                        "title": "Bob 的同 DOI 文献",
                        "DOI": "10.1000/shared-doi",
                    },
                }
            ],
        },
    )
    assert stats["papers_created"] == 1
    assert stats["papers_updated"] == 0

    async with team_client.session_factory() as session:
        alice_paper = await session.get(Paper, alice_paper_id)
        assert alice_paper is not None
        assert alice_paper.title == "Alice 私有（无 Zotero key）"
        assert alice_paper.zotero_item_key is None  # 没被劫持


async def test_foreign_paper_gets_no_pdf_version(team_client, monkeypatch) -> None:
    await _setup_admin(team_client)
    alice = await _create_member(team_client, "alice")
    await _create_member(team_client, "bob")
    async with team_client.session_factory() as session:
        paper = Paper(
            title="Alice 的文献",
            title_norm="alice",
            zotero_item_key="ITEM1",
            owner_user_id=alice["id"],
        )
        session.add(paper)
        await session.commit()
        alice_paper_id = paper.id

    pdf = team_client.tmp_path / "att.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake attachment")
    item = ZItem(
        key="ITEM1",
        title="B 想注入版本的标题",
        attachments=[ZAttachment(key="ATT1", file_path=str(pdf), content_type="application/pdf")],
    )

    async def fake_load_source(source, *, path=None, api_base=None):
        return "json", [ZCollection(key="COL1", name="B 的集合")], [item]

    monkeypatch.setattr(zotero_mod, "load_source", fake_load_source)
    await _login(team_client, "bob", "member-pass-123")
    stats = await _upload(team_client, {"collections": [], "items": []})
    assert stats["papers_skipped_foreign"] == 1
    assert stats["pdfs_imported"] == 0

    async with team_client.session_factory() as session:
        versions = (
            await session.execute(
                select(PaperVersion).where(PaperVersion.paper_id == alice_paper_id)
            )
        ).scalars().all()
        assert versions == []
        refreshed = await session.get(Paper, alice_paper_id)
        assert refreshed is not None
        assert refreshed.title == "Alice 的文献"


async def test_superuser_maintains_legacy_rows(team_client) -> None:
    await _setup_admin(team_client)
    async with team_client.session_factory() as session:
        session.add(
            Paper(
                title="旧标题",
                title_norm="旧标题",
                zotero_item_key="ITEM1",
                owner_user_id=None,
            )
        )
        session.add(Collection(name="旧集合", source="zotero", zotero_key="COL1"))
        await session.commit()

    await _login(team_client, "boss", "admin-pass-123")
    fixture = team_client.tmp_path / "zotero.json"
    fixture.write_text(
        json.dumps(_fixture(item1_title="新标题", col1_name="新集合"), ensure_ascii=False),
        encoding="utf-8",
    )
    r = await team_client.post(
        "/api/import/zotero", json={"source": "json", "path": str(fixture)}
    )
    assert r.status_code == 200, r.text
    stats = r.json()["stats"]
    assert stats["papers_updated"] == 1
    assert stats["papers_skipped_foreign"] == 0
    assert stats["collections_updated"] >= 1

    async with team_client.session_factory() as session:
        paper = (
            await session.execute(select(Paper).where(Paper.zotero_item_key == "ITEM1"))
        ).scalar_one()
        assert paper.title == "新标题"


async def test_upload_rejects_oversize_json(team_client) -> None:
    """F10：超过 50MB 的上传直接 413（边读边查，不先缓冲整文件）。"""
    await _setup_admin(team_client)
    await _create_member(team_client, "alice")
    await _login(team_client, "alice", "member-pass-123")
    big = b"x" * (51 * 1024 * 1024)
    r = await team_client.post(
        "/api/import/zotero/file",
        files={"file": ("big.json", big, "application/json")},
    )
    assert r.status_code == 413, r.text