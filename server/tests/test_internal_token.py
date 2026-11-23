# SPDX-License-Identifier: AGPL-3.0-or-later
"""MCP 内部服务令牌（2026-11-15，docs/17 §11.1）。

认证打开后，MCP 不能带 cookie；API 允许 ``X-LitBench-Internal-Token`` 与
``INTERNAL_API_TOKEN`` 相同（常量时间比较）时，以 system 主体访问全库。
这里守四条：

1. 不带令牌 → 401（和普通匿名请求一样）；
2. 错误令牌 → 401（不能因为"带了头"就放宽）；
3. 正确令牌 → 可读所有人的文献、可访问管理接口（system 视为超管）；
4. 服务端没配置令牌时（默认单机/未部署）→ 即使带正确字符串也 401。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pdf_fixture import make_pdf
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import litbench.db as dbmod
from litbench.config import Settings, settings
from litbench.db import get_session
from litbench.main import app
from litbench.models import Base, Paper

TOKEN = "test-internal-token-123456"


@pytest.fixture
async def internal_client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    monkeypatch.setattr(settings, "internal_api_token", TOKEN)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'internal.db').as_posix()}", poolclass=NullPool
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


async def _seed(client: AsyncClient) -> dict:
    """建管理员 + 一个普通成员，给成员放一篇私有文献。"""
    setup = await client.post(
        "/api/auth/setup",
        json={"username": "boss", "password": "admin-pass-123", "display_name": "组长"},
    )
    assert setup.status_code == 200, setup.text
    created = await client.post(
        "/api/users",
        json={
            "username": "alice",
            "password": "member-pass-123",
            "display_name": "李雷",
            "role": "member",
        },
    )
    assert created.status_code == 201, created.text
    alice = created.json()
    async with client.session_factory() as session:
        paper = Paper(
            title="alice 的私有文献",
            title_norm="alice 的私有文献",
            owner_user_id=alice["id"],
        )
        session.add(paper)
        await session.commit()
        await session.refresh(paper)
        paper_id = paper.id
    # setupAdmin 会顺手登录，把 cookie 清掉：下面要验证的是「没有 cookie」的 MCP 请求
    await client.post("/api/auth/logout")
    return {"alice": alice, "paper_id": paper_id}


async def test_internal_token_rejected_when_missing_or_wrong(internal_client) -> None:
    await _seed(internal_client)
    assert (await internal_client.get("/api/papers")).status_code == 401
    bad = await internal_client.get(
        "/api/papers", headers={"X-LitBench-Internal-Token": "wrong-token-123456"}
    )
    assert bad.status_code == 401


async def test_internal_token_sees_everyones_library(internal_client) -> None:
    seed = await _seed(internal_client)
    headers = {"X-LitBench-Internal-Token": TOKEN}

    listed = await internal_client.get("/api/papers", headers=headers)
    assert listed.status_code == 200, listed.text
    assert [paper["id"] for paper in listed.json()] == [seed["paper_id"]]

    detail = await internal_client.get(f"/api/papers/{seed['paper_id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["can_edit"] is True

    # system 视为超管：管理接口也可用（长期会改成 MCP token 绑定用户）
    users = await internal_client.get("/api/users", headers=headers)
    assert users.status_code == 200
    assert any(row["username"] == "alice" for row in users.json())

    workgroups = await internal_client.get("/api/workgroups", headers=headers)
    assert workgroups.status_code == 200


async def test_internal_token_disabled_when_server_has_no_secret(
    internal_client, monkeypatch
) -> None:
    await _seed(internal_client)
    monkeypatch.setattr(settings, "internal_api_token", "")
    response = await internal_client.get(
        "/api/papers", headers={"X-LitBench-Internal-Token": TOKEN}
    )
    assert response.status_code == 401


async def test_system_writes_are_legacy_not_orphans(internal_client) -> None:
    """system 没有个人库：它上传的文献要落成 legacy（owner=NULL），
    否则普通人的私有列表永远看不见（却又能按 id 访问），变成一群「看不见的孤儿」。"""
    await _seed(internal_client)
    headers = {"X-LitBench-Internal-Token": TOKEN}
    pdf = make_pdf()
    uploaded = await internal_client.post(
        "/api/papers",
        headers=headers,
        files={"file": ("mcp-upload.pdf", pdf, "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text
    paper_id = uploaded.json()["paper_id"]

    async with internal_client.session_factory() as session:
        paper = await session.get(Paper, paper_id)
    assert paper is not None and paper.owner_user_id is None

    # 管理员用 cookie 登录（不带令牌）也能在私有列表里看到它
    login = await internal_client.post(
        "/api/auth/login", json={"username": "boss", "password": "admin-pass-123"}
    )
    assert login.status_code == 200
    listed = await internal_client.get("/api/papers")
    assert listed.status_code == 200
    assert paper_id in {row["id"] for row in listed.json()}


async def test_system_token_can_use_admin_only_surfaces(internal_client) -> None:
    """auth 打开后任务/审计/golden/自动接受收紧为管理员，system 内部令牌必须仍然可用——
    MCP 的 task.* / dataset.golden 就是走这条路径。"""
    await _seed(internal_client)
    headers = {"X-LitBench-Internal-Token": TOKEN}
    for method, path, body in [
        ("GET", "/api/tasks", None),
        ("GET", "/api/audit", None),
        ("GET", "/api/dataset/golden", None),
        ("POST", "/api/proposals/auto-accept", {}),
    ]:
        response = await internal_client.request(method, path, headers=headers, json=body)
        assert response.status_code == 200, f"{method} {path} → {response.status_code}"


def test_internal_token_min_length_is_enforced() -> None:
    """内部令牌等价于全库 system 权限：太短直接启动失败，不静默接受。"""
    with pytest.raises(ValidationError):
        Settings(internal_api_token="short")
    assert Settings(internal_api_token="").internal_api_token == ""
    token = "x" * 16
    assert Settings(internal_api_token=token).internal_api_token == token
