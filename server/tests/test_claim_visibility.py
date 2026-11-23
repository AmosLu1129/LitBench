# SPDX-License-Identifier: AGPL-3.0-or-later
"""写作卡的读可见性（SOTA 审查 F1 / F2）。

F1：共享文献能进对比矩阵，但别人挂在上面的私有卡片不能跟着泄露——「我的论断」
行只放调用者自己的卡（+ 可见文献上的 NULL-owner 旧卡）。

F2：NULL-owner 旧卡不再对全员放行：读跟随文献可见性（私有文献上的旧卡别人
看不见，统一 404 而不是 403 暴露存在性）；写仍只有 owner / 管理员。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import litbench.db as dbmod
from litbench.config import settings
from litbench.db import get_session
from litbench.main import app
from litbench.models import Base, Claim, Figure, Paper, PaperVersion


@pytest.fixture
async def team_client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'claimvis.db').as_posix()}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    old_engine, old_sessionmaker = dbmod._engine, dbmod._sessionmaker
    dbmod._engine, dbmod._sessionmaker = engine, Session

    async def _session():
        async with Session() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        ac.session_factory = Session  # type: ignore[attr-defined]
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


async def _login(client: AsyncClient, username: str) -> None:
    r = await client.post(
        "/api/auth/login", json={"username": username, "password": "member-pass-123"}
    )
    assert r.status_code == 200, r.text


async def _logout(client: AsyncClient) -> None:
    await client.post("/api/auth/logout")


async def _seed_parsed_paper(session_factory, *, title: str, owner_id: str | None) -> dict:
    async with session_factory() as session:
        paper = Paper(title=title, title_norm=title.lower(), owner_user_id=owner_id)
        session.add(paper)
        await session.flush()
        version = PaperVersion(
            paper_id=paper.id,
            kind="uploaded",
            file_sha256=f"{paper.id:0<64}"[:64],
            file_path="pdfs/dummy.pdf",
            page_count=1,
        )
        session.add(version)
        await session.flush()
        figure = Figure(
            paper_version_id=version.id,
            page=1,
            bbox_json=[10, 10, 200, 200],
            caption=f"{title} 的图",
            cite_key=f"T-{paper.id[:8]}-F1",
        )
        session.add(figure)
        claim = Claim(
            paper_id=paper.id,
            paper_version_id=version.id,
            text=f"{title} 里的一条结论",
            section="正文",
            evidence_json=[{"type": "figure", "id": figure.id}],
            owner_user_id=owner_id,
        )
        session.add(claim)
        await session.commit()
        return {
            "paper_id": paper.id,
            "version_id": version.id,
            "figure_id": figure.id,
            "claim_id": claim.id,
        }


async def _add_legacy_claim(session_factory, paper_id: str, version_id: str, figure_id: str) -> str:
    async with session_factory() as session:
        claim = Claim(
            paper_id=paper_id,
            paper_version_id=version_id,
            text="迁移前没有 owner 的旧卡片。",
            section="正文",
            evidence_json=[{"type": "figure", "id": figure_id}],
            owner_user_id=None,
        )
        session.add(claim)
        await session.commit()
        return claim.id


def _matrix_claim_row(payload: dict, paper_id: str) -> str:
    row = next(r for r in payload["rows"] if r["key"] == "claims")
    return next(v["text"] for v in row["values"] if v["paper_id"] == paper_id)


async def test_matrix_on_shared_paper_hides_foreign_claims(team_client) -> None:
    """F1：B 能看共享文献的矩阵，但 A 的私有卡片不进 B 的「我的论断」行。"""
    await _setup_admin(team_client)
    alice = await _create_member(team_client, "alice")
    await _create_member(team_client, "bob")
    await _logout(team_client)
    a1 = await _seed_parsed_paper(
        team_client.session_factory, title="A 的共享文献", owner_id=alice["id"]
    )

    await _login(team_client, "alice")
    group = (await team_client.post("/api/workgroups", json={"name": "矩阵组"})).json()
    await team_client.post(f"/api/workgroups/{group['id']}/members", json={"username": "bob"})
    shared = await team_client.post(
        f"/api/papers/{a1['paper_id']}/share", json={"workgroup_id": group["id"]}
    )
    assert shared.status_code == 201, shared.text
    mine = await team_client.get("/api/matrix", params={"paper_ids": a1["paper_id"]})
    assert mine.status_code == 200, mine.text
    assert "A 的共享文献 里的一条结论" in _matrix_claim_row(mine.json(), a1["paper_id"])
    await _logout(team_client)

    await _login(team_client, "bob")
    matrix = await team_client.get("/api/matrix", params={"paper_ids": a1["paper_id"]})
    assert matrix.status_code == 200, matrix.text  # 文献本身可见
    payload = matrix.json()
    assert _matrix_claim_row(payload, a1["paper_id"]) == "（还没有写作卡片）"
    assert payload["papers"][0]["claim_count"] == 0

    # B 在共享文献上写自己的卡：矩阵只显示 B 自己的，A 的仍不出现
    created = await team_client.post(
        "/api/claims",
        json={
            "paper_id": a1["paper_id"],
            "text": "B 自己写的论断。",
            "evidence": [{"type": "figure", "id": a1["figure_id"]}],
        },
    )
    assert created.status_code == 201, created.text
    matrix = await team_client.get("/api/matrix", params={"paper_ids": a1["paper_id"]})
    text = _matrix_claim_row(matrix.json(), a1["paper_id"])
    assert "B 自己写的论断" in text
    assert "A 的共享文献 里的一条结论" not in text


async def test_legacy_claim_follows_paper_visibility(team_client) -> None:
    """F2：NULL-owner 旧卡在别人私有文献上时，列表/按 id 入口都看不见（404）。"""
    await _setup_admin(team_client)
    alice = await _create_member(team_client, "alice")
    await _create_member(team_client, "bob")
    await _logout(team_client)
    a1 = await _seed_parsed_paper(
        team_client.session_factory, title="A 的私有文献", owner_id=alice["id"]
    )
    legacy_id = await _add_legacy_claim(
        team_client.session_factory, a1["paper_id"], a1["version_id"], a1["figure_id"]
    )

    await _login(team_client, "bob")
    claims = (await team_client.get("/api/claims")).json()
    assert legacy_id not in [c["claim_id"] for c in claims]
    assert (await team_client.get(f"/api/claims/{legacy_id}")).status_code == 404
    # 写入口也先过读门：不能靠 403 确认别人私有文献上旧卡的存在
    assert (
        await team_client.patch(f"/api/claims/{legacy_id}", json={"text": "偷改"})
    ).status_code == 404
    assert (await team_client.delete(f"/api/claims/{legacy_id}")).status_code == 404
    assert (await team_client.get(f"/api/claims/{legacy_id}/revisions")).status_code == 404
    await _logout(team_client)

    # owner 本人可读（文献可见）；但旧卡无 owner，成员仍不能写（只有管理员能维护）
    await _login(team_client, "alice")
    assert (await team_client.get(f"/api/claims/{legacy_id}")).status_code == 200
    claims = (await team_client.get("/api/claims")).json()
    assert legacy_id in [c["claim_id"] for c in claims]
    assert (
        await team_client.patch(f"/api/claims/{legacy_id}", json={"text": "owner 改"})
    ).status_code == 403
    await _logout(team_client)

    # 管理员直通
    await team_client.post(
        "/api/auth/login", json={"username": "boss", "password": "admin-pass-123"}
    )
    assert (await team_client.get(f"/api/claims/{legacy_id}")).status_code == 200


async def test_legacy_claim_on_shared_paper_readable_not_writable(team_client) -> None:
    """F2：共享文献上的 NULL-owner 旧卡对工作组成员可读、不可写。"""
    await _setup_admin(team_client)
    alice = await _create_member(team_client, "alice")
    await _create_member(team_client, "bob")
    await _logout(team_client)
    a1 = await _seed_parsed_paper(
        team_client.session_factory, title="A 的共享文献", owner_id=alice["id"]
    )
    legacy_id = await _add_legacy_claim(
        team_client.session_factory, a1["paper_id"], a1["version_id"], a1["figure_id"]
    )

    await _login(team_client, "alice")
    group = (await team_client.post("/api/workgroups", json={"name": "旧卡组"})).json()
    await team_client.post(f"/api/workgroups/{group['id']}/members", json={"username": "bob"})
    await team_client.post(
        f"/api/papers/{a1['paper_id']}/share", json={"workgroup_id": group["id"]}
    )
    await _logout(team_client)

    await _login(team_client, "bob")
    assert (await team_client.get(f"/api/claims/{legacy_id}")).status_code == 200
    claims = (await team_client.get("/api/claims")).json()
    assert legacy_id in [c["claim_id"] for c in claims]
    assert (
        await team_client.patch(f"/api/claims/{legacy_id}", json={"text": "B 改"})
    ).status_code == 403
    # 矩阵里旧卡算「可见文献上的旧卡」，B 能看到
    matrix = await team_client.get("/api/matrix", params={"paper_ids": a1["paper_id"]})
    assert "迁移前没有 owner 的旧卡片" in _matrix_claim_row(matrix.json(), a1["paper_id"])