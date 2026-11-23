# SPDX-License-Identifier: AGPL-3.0-or-later
"""账号与登录 API（2026-11-15，团队共享第一步）。

* ``GET /api/auth/status``：认证是否开启、是否已初始化、当前用户（前端进门用）；
* ``POST /api/auth/setup``：仅当库里**一个用户都没有**时可用，创建第一个管理员并登录；
* ``POST /api/auth/login`` / ``POST /api/auth/logout``；
* ``GET /api/users`` / ``POST /api/users`` / ``PATCH /api/users/{id}``：管理员建号、
  停用/启用、重置密码（团队共享的「管理员建号」路线）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import (
    AuthError,
    authenticate,
    count_users,
    create_session,
    create_user,
    current_user,
    list_users,
    require_admin,
    resolve_session,
    revoke_session,
    set_active,
    set_password,
    user_to_dict,
)
from ..config import settings
from ..db import get_session
from ..models import AppUser
from ..schemas import (
    AuthStatusOut,
    LoginIn,
    SetupIn,
    UserCreateIn,
    UserOut,
    UserPatchIn,
)

router = APIRouter(tags=["auth"])


def _error(exc: AuthError) -> HTTPException:
    message = str(exc)
    status = 409 if "已存在" in message else 422
    return HTTPException(status_code=status, detail=message)


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        max_age=max(1, int(settings.auth_session_days)) * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.auth_cookie_secure,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=settings.auth_cookie_name, path="/")


@router.get("/auth/status", response_model=AuthStatusOut)
async def auth_status(
    request: Request, session: AsyncSession = Depends(get_session)
) -> AuthStatusOut:
    """认证开关 + 是否已初始化 + 当前登录用户（cookie 有就带出来）。"""
    user = None
    if settings.auth_enabled:
        token = request.cookies.get(settings.auth_cookie_name)
        user = await resolve_session(session, token)
    initialized = await count_users(session) > 0
    return AuthStatusOut(
        auth_enabled=bool(settings.auth_enabled),
        initialized=initialized,
        user=UserOut(**user_to_dict(user)) if user is not None else None,
    )


@router.post("/auth/setup", response_model=AuthStatusOut)
async def setup_admin(
    body: SetupIn, response: Response, session: AsyncSession = Depends(get_session)
) -> AuthStatusOut:
    """首次初始化：只在「零用户」时允许，创建管理员并直接登录。"""
    if not settings.auth_enabled:
        raise HTTPException(status_code=409, detail="认证未开启（LITBENCH_AUTH=0），不需要初始化")
    if await count_users(session) > 0:
        raise HTTPException(status_code=409, detail="已经初始化过，请直接登录")
    try:
        user = await create_user(
            session,
            username=body.username,
            password=body.password,
            display_name=body.display_name,
            role="admin",
        )
    except AuthError as exc:
        raise _error(exc) from exc
    token = await create_session(session, user)
    _set_session_cookie(response, token)
    return AuthStatusOut(
        auth_enabled=True, initialized=True, user=UserOut(**user_to_dict(user))
    )


@router.post("/auth/login", response_model=AuthStatusOut)
async def login(
    body: LoginIn, response: Response, session: AsyncSession = Depends(get_session)
) -> AuthStatusOut:
    if not settings.auth_enabled:
        raise HTTPException(status_code=409, detail="认证未开启（LITBENCH_AUTH=0）")
    try:
        user = await authenticate(session, body.username, body.password)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    token = await create_session(session, user)
    _set_session_cookie(response, token)
    return AuthStatusOut(
        auth_enabled=True, initialized=True, user=UserOut(**user_to_dict(user))
    )


@router.post("/auth/logout")
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """退出登录：撤销服务端 session + 清 cookie（幂等，未登录也返回 ok）。"""
    token = request.cookies.get(settings.auth_cookie_name)
    if settings.auth_enabled and token:
        await revoke_session(session, token)
    _clear_session_cookie(response)
    return {"ok": True}


@router.get("/auth/me", response_model=UserOut)
async def me(user: AppUser = Depends(current_user)) -> UserOut:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    return UserOut(**user_to_dict(user))


# ---------------------------------------------------------------------------
# 管理员：建号 / 列表 / 停用 / 重置密码
# ---------------------------------------------------------------------------
@router.get("/users", response_model=list[UserOut])
async def admin_list_users(
    _admin: AppUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[UserOut]:
    return [UserOut(**user_to_dict(row)) for row in await list_users(session)]


@router.post("/users", response_model=UserOut, status_code=201)
async def admin_create_user(
    body: UserCreateIn,
    _admin: AppUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    try:
        user = await create_user(
            session,
            username=body.username,
            password=body.password,
            display_name=body.display_name,
            role=body.role,
        )
    except AuthError as exc:
        raise _error(exc) from exc
    return UserOut(**user_to_dict(user))


@router.patch("/users/{user_id}", response_model=UserOut)
async def admin_patch_user(
    user_id: str,
    body: UserPatchIn,
    admin: AppUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    target = await session.get(AppUser, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.id == admin.id and body.is_active is False:
        raise HTTPException(status_code=422, detail="不能停用当前登录的管理员自己")
    if target.id == admin.id and body.role is not None and body.role != "admin":
        admin_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(AppUser)
                    .where(AppUser.role == "admin", AppUser.is_active.is_(True))
                )
            ).scalar()
            or 0
        )
        if admin_count <= 1:
            raise HTTPException(
                status_code=422,
                detail="不能把当前登录的最后一个管理员降级（否则没人能再进管理面）",
            )
    try:
        if body.is_active is not None:
            target = await set_active(session, user_id, body.is_active)
        if body.role is not None:
            target.role = body.role
            await session.commit()
            await session.refresh(target)
        if body.password is not None:
            target = await set_password(session, user_id, body.password)
    except AuthError as exc:
        raise _error(exc) from exc
    return UserOut(**user_to_dict(target))
