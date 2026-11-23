# SPDX-License-Identifier: AGPL-3.0-or-later
"""账号/登录/管理员建号（2026-11-15，团队共享第一步）。

守四条：

1. 首个管理员只能通过 ``/auth/setup`` 且仅当零用户时创建；
2. 密码只存 scrypt 哈希，不落明文、登录失败不区分「用户不存在/密码错」；
3. 认证打开后，未带 cookie 的 ``/api/*`` 一律 401（登录/探针/文档除外）；
4. 停用账号后旧 session 立即失效；只有管理员能建号/停用/重置密码。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import litbench.db as dbmod
from litbench.config import settings
from litbench.db import get_session
from litbench.main import app
from litbench.models import AppUser, Base


@pytest.fixture
async def auth_client(tmp_path, monkeypatch):
    """认证打开 + 测试库：middleware 与依赖都指向同一个 sqlite 文件。"""
    monkeypatch.setattr(settings, "auth_enabled", True)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'auth.db').as_posix()}", poolclass=NullPool
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
        yield ac
    app.dependency_overrides.clear()
    dbmod._engine, dbmod._sessionmaker = old_engine, old_sessionmaker
    await engine.dispose()


async def _setup(client: AsyncClient, password: str = "admin-pass-123") -> dict:
    response = await client.post(
        "/api/auth/setup",
        json={"username": "boss", "password": password, "display_name": "组长"},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_status_before_setup(auth_client) -> None:
    response = await auth_client.get("/api/auth/status")
    assert response.status_code == 200
    data = response.json()
    assert data["auth_enabled"] is True
    assert data["initialized"] is False
    assert data["user"] is None

    # 未登录访问受保护接口
    blocked = await auth_client.get("/api/papers")
    assert blocked.status_code == 401

    # 健康探针必须免登录（Docker HEALTHCHECK 用它判容器存活）
    ready = await auth_client.get("/api/ready")
    assert ready.status_code == 200


async def test_setup_creates_admin_and_logs_in(auth_client) -> None:
    data = await _setup(auth_client)
    assert data["initialized"] is True
    assert data["user"]["role"] == "admin"
    assert data["user"]["username"] == "boss"

    # cookie 生效
    me = await auth_client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "boss"
    protected = await auth_client.get("/api/papers")
    assert protected.status_code == 200

    # 库里只存哈希
    async with auth_client.session_factory() as s:  # type: ignore[attr-defined]
        user = (
            await s.execute(select(AppUser).where(AppUser.username == "boss"))
        ).scalar_one()
        assert user.password_hash.startswith("scrypt$")
        assert "admin-pass-123" not in user.password_hash

    # 不能重复初始化
    again = await auth_client.post(
        "/api/auth/setup", json={"username": "boss2", "password": "another-pass-1"}
    )
    assert again.status_code == 409


async def test_login_logout_and_bad_password(auth_client) -> None:
    await _setup(auth_client)
    await auth_client.post("/api/auth/logout")
    assert (await auth_client.get("/api/auth/me")).status_code == 401

    bad = await auth_client.post(
        "/api/auth/login", json={"username": "boss", "password": "wrong-pass-1"}
    )
    assert bad.status_code == 401
    assert "不对" in bad.text

    ok = await auth_client.post(
        "/api/auth/login", json={"username": "boss", "password": "admin-pass-123"}
    )
    assert ok.status_code == 200
    assert ok.json()["user"]["username"] == "boss"
    assert (await auth_client.get("/api/auth/me")).status_code == 200


async def test_admin_creates_member_and_role_guard(auth_client) -> None:
    await _setup(auth_client)
    created = await auth_client.post(
        "/api/users",
        json={"username": "member1", "password": "member-pass-1", "role": "member"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["role"] == "member"

    # 管理员能看到用户列表
    listed = await auth_client.get("/api/users")
    assert listed.status_code == 200
    assert {row["username"] for row in listed.json()} == {"boss", "member1"}

    # 换成员登录：不能建号/列用户
    await auth_client.post("/api/auth/logout")
    login = await auth_client.post(
        "/api/auth/login", json={"username": "member1", "password": "member-pass-1"}
    )
    assert login.status_code == 200
    assert (await auth_client.get("/api/users")).status_code == 403
    forbidden = await auth_client.post(
        "/api/users", json={"username": "x", "password": "12345678"}
    )
    assert forbidden.status_code == 403


async def test_disable_user_kills_session(auth_client) -> None:
    await _setup(auth_client)
    created = await auth_client.post(
        "/api/users", json={"username": "member2", "password": "member-pass-2"}
    )
    member_id = created.json()["id"]

    await auth_client.post("/api/auth/logout")
    await auth_client.post(
        "/api/auth/login", json={"username": "member2", "password": "member-pass-2"}
    )
    assert (await auth_client.get("/api/auth/me")).status_code == 200

    # 管理员停用成员（管理员自己重新登录）
    await auth_client.post("/api/auth/logout")
    await auth_client.post(
        "/api/auth/login", json={"username": "boss", "password": "admin-pass-123"}
    )
    patched = await auth_client.patch(f"/api/users/{member_id}", json={"is_active": False})
    assert patched.status_code == 200
    assert patched.json()["is_active"] is False

    await auth_client.post("/api/auth/logout")
    # 被停用的账号不能登录
    denied = await auth_client.post(
        "/api/auth/login", json={"username": "member2", "password": "member-pass-2"}
    )
    assert denied.status_code == 401


async def test_short_password_rejected(auth_client) -> None:
    response = await auth_client.post(
        "/api/auth/setup", json={"username": "boss", "password": "short"}
    )
    assert response.status_code == 422


async def test_session_cookie_flags_and_secure_switch(auth_client, monkeypatch) -> None:
    """cookie 只给浏览器看：HttpOnly + SameSite=Lax；HTTPS 部署时可加 Secure。"""
    response = await auth_client.post(
        "/api/auth/setup",
        json={"username": "boss", "password": "admin-pass-123"},
    )
    assert response.status_code == 200, response.text
    cookie = response.headers.get("set-cookie", "")
    lowered = cookie.lower()
    assert "httponly" in lowered
    assert "samesite=lax" in lowered
    assert "; secure" not in lowered  # 默认 http 单机/局域网不上 Secure

    await auth_client.post("/api/auth/logout")
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    login = await auth_client.post(
        "/api/auth/login", json={"username": "boss", "password": "admin-pass-123"}
    )
    assert login.status_code == 200
    assert "; secure" in login.headers.get("set-cookie", "").lower()


async def test_disabling_user_kills_existing_session(auth_client) -> None:
    """不是「停用后不能再登录」——旧的 cookie 必须立刻失效。"""
    await _setup(auth_client)
    created = await auth_client.post(
        "/api/users", json={"username": "member3", "password": "member-pass-3"}
    )
    member_id = created.json()["id"]

    # 成员登录（auth_client 的 cookie jar 里现在只有成员 session）
    await auth_client.post("/api/auth/logout")
    await auth_client.post(
        "/api/auth/login", json={"username": "member3", "password": "member-pass-3"}
    )
    assert (await auth_client.get("/api/auth/me")).status_code == 200

    # 另开一个客户端用管理员身份停用成员；成员客户端保持原 cookie 不动
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as admin:
        admin.session_factory = auth_client.session_factory  # type: ignore[attr-defined]
        login = await admin.post(
            "/api/auth/login", json={"username": "boss", "password": "admin-pass-123"}
        )
        assert login.status_code == 200
        patched = await admin.patch(f"/api/users/{member_id}", json={"is_active": False})
        assert patched.status_code == 200

    # 旧 session 立刻被拒（resolve_session 检查 is_active）
    assert (await auth_client.get("/api/auth/me")).status_code == 401
    assert (await auth_client.get("/api/papers")).status_code == 401


async def test_member_cannot_patch_other_users(auth_client) -> None:
    await _setup(auth_client)
    member = await auth_client.post(
        "/api/users", json={"username": "member4", "password": "member-pass-4"}
    )
    await auth_client.post("/api/auth/logout")
    await auth_client.post(
        "/api/auth/login", json={"username": "member4", "password": "member-pass-4"}
    )
    assert (
        await auth_client.patch(f"/api/users/{member.json()['id']}", json={"is_active": False})
    ).status_code == 403
    assert (
        await auth_client.patch(f"/api/users/{member.json()['id']}", json={"role": "admin"})
    ).status_code == 403
