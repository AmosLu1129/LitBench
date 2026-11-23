# SPDX-License-Identifier: AGPL-3.0-or-later
"""工作组 / 文献共享 / 数据归属（M7 第二步，2026-11-15）。

验收按 docs/17 §10.1 的两个账号 A/B 走：

1. A 上传的文献 B 的私有库看不到；
2. A 共享到工作组后，B 在团队库看到，未共享的仍看不到；
3. B 能看共享文献的详情/图，但不能共享/改著录字段；
4. A 取消共享后 B 立即看不到；
5. A/B 的笔记/收藏/阅读状态互不影响（user_paper_state）。

另外守两条边界：认证关闭时团队接口 503（不悄悄放宽），
以及「共享文献挂 claim / 进目录」的越权返回 404/403。
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

from pdf_fixture import make_pdf


@pytest.fixture
async def team_client(tmp_path, monkeypatch):
    """认证打开 + 独立 sqlite + 临时 storage。"""
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'team.db').as_posix()}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    old_engine, old_sessionmaker = dbmod._engine, dbmod._sessionmaker
    dbmod._engine = engine
    dbmod._sessionmaker = Session

    async def _session():
        async with Session() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        ac.session_factory = Session  # type: ignore[attr-defined]
        yield ac
    app.dependency_overrides.clear()
    dbmod._engine, dbmod._sessionmaker = old_engine, old_sessionmaker
    await engine.dispose()


@pytest.fixture
async def open_client(tmp_path, monkeypatch):
    """认证关闭的客户端（验证团队接口不放宽）。"""
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'open.db').as_posix()}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    old_engine, old_sessionmaker = dbmod._engine, dbmod._sessionmaker
    dbmod._engine = engine
    dbmod._sessionmaker = Session

    async def _session():
        async with Session() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        ac.session_factory = Session  # type: ignore[attr-defined]
        yield ac
    app.dependency_overrides.clear()
    dbmod._engine, dbmod._sessionmaker = old_engine, old_sessionmaker
    await engine.dispose()


async def _setup_admin(client: AsyncClient) -> dict:
    response = await client.post(
        "/api/auth/setup",
        json={"username": "boss", "password": "admin-pass-123", "display_name": "组长"},
    )
    assert response.status_code == 200, response.text
    return response.json()["user"]


async def _create_member(client: AsyncClient, username: str) -> dict:
    response = await client.post(
        "/api/users",
        json={
            "username": username,
            "password": "member-pass-123",
            "display_name": username.upper(),
            "role": "member",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _login(client: AsyncClient, username: str, password: str) -> None:
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text


async def _logout(client: AsyncClient) -> None:
    await client.post("/api/auth/logout")


async def _seed_paper(session_factory, *, title: str, owner_id: str | None) -> Paper:
    async with session_factory() as session:
        paper = Paper(title=title, title_norm=title.lower(), owner_user_id=owner_id)
        session.add(paper)
        await session.commit()
        await session.refresh(paper)
        return paper


async def _seed_parsed_paper(session_factory, *, title: str, owner_id: str | None) -> dict:
    """带版本 + 一张图 + 一条 claim 的文献，方便测图/claim 的可见性。"""
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
            evidence_json=[
                {"type": "figure", "id": figure.id, "cite_key": figure.cite_key}
            ],
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


# ---------------------------------------------------------------------------
# 认证关闭：团队接口必须拒绝，而不是悄悄按全局库放行
# ---------------------------------------------------------------------------
async def test_team_requires_auth(open_client) -> None:
    assert (await open_client.get("/api/workgroups")).status_code == 503
    assert (await open_client.post("/api/workgroups", json={"name": "x"})).status_code == 503


# ---------------------------------------------------------------------------
# 工作组 / 成员权限
# ---------------------------------------------------------------------------
async def test_workgroup_membership_and_permissions(team_client) -> None:
    admin = await _setup_admin(team_client)
    alice = await _create_member(team_client, "alice")
    bob = await _create_member(team_client, "bob")
    await _logout(team_client)

    # A 建组，创建者自动成为 owner
    await _login(team_client, "alice", "member-pass-123")
    created = await team_client.post(
        "/api/workgroups", json={"name": "热流组", "description": "传热/流体"}
    )
    assert created.status_code == 201, created.text
    group = created.json()
    assert group["role"] == "owner"
    assert group["member_count"] == 1
    group_id = group["id"]

    # A 按用户名把 B 加进来
    added = await team_client.post(
        f"/api/workgroups/{group_id}/members", json={"username": "bob"}
    )
    assert added.status_code == 201, added.text
    assert added.json()["role"] == "member"

    # B 能看到组、能看成员名单，但不能加人
    await _logout(team_client)
    await _login(team_client, "bob", "member-pass-123")
    groups = (await team_client.get("/api/workgroups")).json()
    assert [item["id"] for item in groups] == [group_id]
    assert groups[0]["role"] == "member"
    detail = await team_client.get(f"/api/workgroups/{group_id}")
    assert detail.status_code == 200
    assert {m["username"] for m in detail.json()["members"]} == {"alice", "bob"}
    forbidden = await team_client.post(
        f"/api/workgroups/{group_id}/members", json={"username": "boss"}
    )
    assert forbidden.status_code == 403

    # B 自己退出；组里只剩 A
    left = await team_client.delete(f"/api/workgroups/{group_id}/members/{bob['id']}")
    assert left.status_code == 200
    assert (await team_client.get("/api/workgroups")).json() == []

    # 最后一个 owner 不能退出/被移除
    await _logout(team_client)
    await _login(team_client, "alice", "member-pass-123")
    last_owner = await team_client.delete(
        f"/api/workgroups/{group_id}/members/{alice['id']}"
    )
    assert last_owner.status_code == 422

    # 管理员可以管理任何组
    await _logout(team_client)
    await _login(team_client, "boss", "admin-pass-123")
    assert (await team_client.get(f"/api/workgroups/{group_id}")).status_code == 200
    assert (await team_client.patch(
        f"/api/workgroups/{group_id}", json={"name": "热流组（改名）"}
    )).status_code == 200
    assert admin["role"] == "admin"


# ---------------------------------------------------------------------------
# A/B 私有库隔离 + 共享/取消共享 + 只读
# ---------------------------------------------------------------------------
async def test_private_library_and_shared_paper_readonly(team_client) -> None:
    await _setup_admin(team_client)
    alice = await _create_member(team_client, "alice")
    bob = await _create_member(team_client, "bob")
    await _logout(team_client)

    a1 = await _seed_parsed_paper(
        team_client.session_factory, title="A 的传热论文", owner_id=alice["id"]
    )
    a2 = await _seed_paper(team_client.session_factory, title="A 未共享论文", owner_id=alice["id"])
    b1 = await _seed_parsed_paper(
        team_client.session_factory, title="B 自己的论文", owner_id=bob["id"]
    )

    # 私有库：各看各的
    await _login(team_client, "alice", "member-pass-123")
    mine = (await team_client.get("/api/papers")).json()
    assert {p["id"] for p in mine} == {a1["paper_id"], a2.id}
    assert (await team_client.get(f"/api/papers/{b1['paper_id']}")).status_code == 404
    await _logout(team_client)

    await _login(team_client, "bob", "member-pass-123")
    mine = (await team_client.get("/api/papers")).json()
    assert {p["id"] for p in mine} == {b1["paper_id"]}
    assert (await team_client.get(f"/api/papers/{a1['paper_id']}")).status_code == 404
    # 未共享的图也看不见
    assert (await team_client.get(f"/api/figures/{a1['figure_id']}")).status_code == 404
    assert (await team_client.get(f"/api/figures/{a1['figure_id']}/image")).status_code == 404
    await _logout(team_client)

    # A 建组 + 加 B + 只共享第一篇
    await _login(team_client, "alice", "member-pass-123")
    group = (
        await team_client.post("/api/workgroups", json={"name": "共享测试组"})
    ).json()
    await team_client.post(
        f"/api/workgroups/{group['id']}/members", json={"username": "bob"}
    )
    shared = await team_client.post(
        f"/api/papers/{a1['paper_id']}/share", json={"workgroup_id": group["id"]}
    )
    assert shared.status_code == 201, shared.text
    assert shared.json()["created"] is True
    # 重复共享是幂等的
    again = await team_client.post(
        f"/api/papers/{a1['paper_id']}/share", json={"workgroup_id": group["id"]}
    )
    assert again.status_code == 201
    assert again.json()["created"] is False
    await _logout(team_client)

    # B：团队库只有共享的那篇；未共享的仍看不见
    await _login(team_client, "bob", "member-pass-123")
    team = (await team_client.get(f"/api/papers?workgroup_id={group['id']}")).json()
    assert [p["id"] for p in team] == [a1["paper_id"]]
    assert team[0]["owner_username"] == "ALICE"
    assert team[0]["can_edit"] is False
    assert (await team_client.get(f"/api/papers/{a2.id}")).status_code == 404

    # 共享文献的详情/正文/图/表可以读
    detail = await team_client.get(f"/api/papers/{a1['paper_id']}")
    assert detail.status_code == 200
    assert (await team_client.get(f"/api/papers/{a1['paper_id']}/text")).status_code == 200
    assert (await team_client.get(f"/api/figures/{a1['figure_id']}")).status_code == 200
    assert (
        await team_client.get(f"/api/papers/{a1['paper_id']}/figures")
    ).status_code == 200

    # 但 B 不能共享/取消共享/改著录字段
    assert (
        await team_client.post(
            f"/api/papers/{a1['paper_id']}/share", json={"workgroup_id": group["id"]}
        )
    ).status_code == 403
    assert (
        await team_client.delete(
            f"/api/papers/{a1['paper_id']}/share/{group['id']}"
        )
    ).status_code == 403
    assert (
        await team_client.patch(
            f"/api/papers/{a1['paper_id']}", json={"volume": "12"}
        )
    ).status_code == 403

    # 笔记/收藏/阅读状态按用户隔离
    assert (
        await team_client.patch(
            f"/api/papers/{a1['paper_id']}",
            json={"note": "B 的笔记", "favorite": True, "reading_status": "reading"},
        )
    ).status_code == 200
    bob_view = (await team_client.get(f"/api/papers/{a1['paper_id']}")).json()
    assert bob_view["note"] == "B 的笔记"
    assert bob_view["favorite"] is True
    # B 的收藏只在「团队库 + 收藏夹」里出现，不混进 A 的私有库
    assert (
        await team_client.get(f"/api/papers/{a1['paper_id']}")
    ).json()["reading_status"] == "reading"
    team_fav = (
        await team_client.get(
            f"/api/papers?workgroup_id={group['id']}&favorite=true"
        )
    ).json()
    assert [p["id"] for p in team_fav] == [a1["paper_id"]]
    await _logout(team_client)

    await _login(team_client, "alice", "member-pass-123")
    alice_view = (await team_client.get(f"/api/papers/{a1['paper_id']}")).json()
    assert alice_view["note"] is None
    assert alice_view["favorite"] is False
    alice_fav = (await team_client.get("/api/papers?favorite=true")).json()
    assert alice_fav == []

    # A 取消共享 → B 立即看不到
    removed = await team_client.delete(
        f"/api/papers/{a1['paper_id']}/share/{group['id']}"
    )
    assert removed.status_code == 200
    assert removed.json()["removed"] is True
    await _logout(team_client)

    await _login(team_client, "bob", "member-pass-123")
    assert (await team_client.get(f"/api/papers/{a1['paper_id']}")).status_code == 404
    assert (await team_client.get(f"/api/figures/{a1['figure_id']}")).status_code == 404
    assert (
        await team_client.get(f"/api/papers?workgroup_id={group['id']}")
    ).json() == []
    # 退出组后连团队库都进不去
    await team_client.delete(f"/api/workgroups/{group['id']}/members/{bob['id']}")
    assert (
        await team_client.get(f"/api/papers?workgroup_id={group['id']}")
    ).status_code == 404


# ---------------------------------------------------------------------------
# 上传归属 + 同一 PDF 跨用户 409
# ---------------------------------------------------------------------------
async def test_upload_sets_owner_and_cross_user_conflict(team_client) -> None:
    await _setup_admin(team_client)
    alice = await _create_member(team_client, "alice")
    await _create_member(team_client, "bob")
    await _logout(team_client)

    pdf = make_pdf()
    await _login(team_client, "alice", "member-pass-123")
    uploaded = await team_client.post(
        "/api/papers",
        files={"file": ("paper.pdf", pdf, "application/pdf")},
        data={"title": "A 上传的论文"},
    )
    assert uploaded.status_code == 201, uploaded.text
    paper_id = uploaded.json()["paper_id"]
    async with team_client.session_factory() as session:
        paper = await session.get(Paper, paper_id)
        assert paper is not None
        assert paper.owner_user_id == alice["id"]
    await _logout(team_client)

    # B 的私有库看不到；重复上传同一 PDF 返回 409（不回显别人的文献）
    await _login(team_client, "bob", "member-pass-123")
    assert (await team_client.get("/api/papers")).json() == []
    conflict = await team_client.post(
        "/api/papers",
        files={"file": ("paper.pdf", pdf, "application/pdf")},
        data={"title": "B 也传一份"},
    )
    assert conflict.status_code == 409
    assert "其他用户" in conflict.json()["detail"]


# ---------------------------------------------------------------------------
# 目录 / 写作卡片 / 导出 / references 的归属隔离
# ---------------------------------------------------------------------------
async def test_collections_claims_exports_are_private(team_client) -> None:
    await _setup_admin(team_client)
    alice = await _create_member(team_client, "alice")
    bob = await _create_member(team_client, "bob")
    await _logout(team_client)

    a1 = await _seed_parsed_paper(
        team_client.session_factory, title="A 的文献", owner_id=alice["id"]
    )

    await _login(team_client, "alice", "member-pass-123")
    collection = (
        await team_client.post("/api/collections", json={"name": "A 的目录"})
    ).json()
    assert collection["name"] == "A 的目录"
    claims = (await team_client.get("/api/claims")).json()
    assert [c["claim_id"] for c in claims] == [a1["claim_id"]]
    export = await team_client.post(
        "/api/writing/export", json={"format": "markdown", "include_drafts": True}
    )
    assert export.status_code == 200, export.text
    assert a1["claim_id"] in export.json()["content"] or "A 的文献" in export.json()["content"]
    refs = await team_client.post(
        "/api/references/format", json={"paper_ids": [a1["paper_id"]], "style": "apa7"}
    )
    assert refs.status_code == 200, refs.text
    await _logout(team_client)

    await _login(team_client, "bob", "member-pass-123")
    assert (await team_client.get("/api/collections")).json() == []
    assert (await team_client.get("/api/claims")).json() == []
    # 看不见的 claim 统一 404（不确认存在性）；可见但只读的才 403
    assert (await team_client.patch(
        f"/api/claims/{a1['claim_id']}", json={"text": "偷改"}
    )).status_code == 404
    assert (await team_client.get(f"/api/claims/{a1['claim_id']}")).status_code == 404
    assert (
        await team_client.get(
            "/api/matrix", params={"paper_ids": a1["paper_id"]}
        )
    ).status_code == 404
    assert (
        await team_client.post(
            "/api/references/format",
            json={"paper_ids": [a1["paper_id"]], "style": "apa7"},
        )
    ).status_code == 404
    # 别人的目录：看不见也改不了
    assert (
        await team_client.patch(
            f"/api/collections/{collection['id']}", json={"name": "偷改"}
        )
    ).status_code == 404
    # 不能把别人的文献挂到别人的目录里
    assert (
        await team_client.post(
            f"/api/collections/{collection['id']}/papers",
            json={"paper_id": a1["paper_id"]},
        )
    ).status_code == 404
    # 导出看不见的论文也拒绝
    assert (
        await team_client.post(
            "/api/writing/export",
            json={"format": "bibtex", "paper_ids": [a1["paper_id"]]},
        )
    ).status_code == 404


# ---------------------------------------------------------------------------
# 摘要 / 提案：看不见的文献不能从「按 id」的入口绕过
# ---------------------------------------------------------------------------
async def test_summary_and_proposal_visibility(team_client) -> None:
    await _setup_admin(team_client)
    alice = await _create_member(team_client, "alice")
    bob = await _create_member(team_client, "bob")
    await _logout(team_client)
    a1 = await _seed_parsed_paper(
        team_client.session_factory, title="A 的带摘要文献", owner_id=alice["id"]
    )

    # 直接构造一条图纠错提案（pending），验证「按 id」入口也做可见性
    async with team_client.session_factory() as session:
        from litbench.models import FigureProposal

        proposal = FigureProposal(
            figure_id=a1["figure_id"],
            paper_version_id=a1["version_id"],
            kind="figure_review",
            proposed_json={"needs_change": False},
            evidence_json=[],
            confidence=0.9,
            status="pending",
        )
        session.add(proposal)
        await session.commit()
        await session.refresh(proposal)
        proposal_id = proposal.id

    await _login(team_client, "bob", "member-pass-123")
    assert (await team_client.get("/api/proposals")).json() == []
    assert (await team_client.get(f"/api/proposals/{proposal_id}")).status_code == 404
    assert (
        await team_client.post(
            f"/api/proposals/{proposal_id}/decide", json={"action": "reject"}
        )
    ).status_code == 404
    assert (
        await team_client.get(f"/api/papers/{a1['paper_id']}/summary")
    ).status_code == 404
    assert (
        await team_client.get(f"/api/papers/{a1['paper_id']}/sections")
    ).status_code == 404
    assert (
        await team_client.post(
            f"/api/papers/{a1['paper_id']}/summaries/generate", json={}
        )
    ).status_code == 404
    await _logout(team_client)

    await _login(team_client, "alice", "member-pass-123")
    pending = (await team_client.get("/api/proposals")).json()
    assert [p["proposal_id"] for p in pending] == [proposal_id]
    assert (await team_client.get(f"/api/proposals/{proposal_id}")).status_code == 200
    # 自己的文献可以生成（mock provider 不联网）；这里只验证没有 403
    assert (
        await team_client.get(f"/api/papers/{a1['paper_id']}/sections")
    ).status_code == 200
