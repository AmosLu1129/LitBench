# SPDX-License-Identifier: AGPL-3.0-or-later
"""本地账号与会话（2026-11-15，团队共享第一步）。

对外语义：

* ``hash_password`` / ``verify_password``：scrypt，存 ``scrypt$N$r$p$salt$hash``；
* ``create_user`` / ``authenticate``：管理员建号，用户登录；
* ``create_session`` / ``resolve_session`` / ``revoke_session``：服务端 session，
  浏览器 cookie 只是随机 token，库里存 sha256；
* ``current_user``：FastAPI 依赖，认证关闭时返回 ``None``（单机/测试模式）。

认证关闭（``settings.auth_enabled == False``）时，所有「需要用户」的逻辑必须
按旧行为工作（全局库可见）；打开后由中间件统一拦截 ``/api/*``。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_session
from .models import AppUser, UserSession

# scrypt 参数：本地登录 QPS 很低，N=2^14 在普通机器上约 30–60ms，够用且不算卡。
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_KEYLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 200
ROLES = ("admin", "member")
# 内部服务主体（MCP 内部令牌）：不落库、不走 session，role=system 表示「全库可读可管理」。
# 与 admin 的区别：admin 的界面默认仍只看自己的库；system 没有个人库，列表条件直接放行。
SYSTEM_USER_ID = "system"
SYSTEM_ROLE = "system"
SUPERUSER_ROLES = ("admin", SYSTEM_ROLE)


class AuthError(RuntimeError):
    """账号/登录的可读错误。"""


def is_superuser(user: AppUser | None) -> bool:
    """管理员或内部服务主体（共享判定层统一用它，避免漏掉 system）。"""
    return user is not None and user.role in SUPERUSER_ROLES


def system_user() -> AppUser:
    """构造内部服务主体（不写入数据库；只用于 request.state.user）。

    密码哈希为空、id 固定为 ``system``：这个对象永远不出现在 ``app_user`` 表里，
    也不能拿来登录（``authenticate`` 只查库）。
    """
    return AppUser(
        id=SYSTEM_USER_ID,
        username=SYSTEM_USER_ID,
        display_name="内部服务",
        password_hash="",
        role=SYSTEM_ROLE,
        is_active=True,
    )


def owner_id_for_write(user: AppUser | None) -> str | None:
    """写数据归属时用：认证关闭 / 内部服务 system 主体 → None（legacy 公共语料）。

    system 没有个人库；如果把它写成 owner，普通人的私有列表（只认 owner=我 或 NULL）
    永远看不到 MCP 上传的文献，而按 id 又能访问——制造一类「看不见的孤儿」。
    """
    if user is None or user.role == SYSTEM_ROLE:
        return None
    return user.id


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LEN:
        raise AuthError(f"密码至少 {MIN_PASSWORD_LEN} 位")
    if len(password) > MAX_PASSWORD_LEN:
        raise AuthError(f"密码太长（最多 {MAX_PASSWORD_LEN} 位）")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=_SCRYPT_KEYLEN,
    )
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N,
        _SCRYPT_R,
        _SCRYPT_P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    """常量时间校验；哈希格式坏掉按失败处理，不抛异常（避免登录接口 500）。"""
    try:
        scheme, n_raw, r_raw, p_raw, salt_raw, digest_raw = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_raw)
        expected = base64.b64decode(digest_raw)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_raw),
            r=int(r_raw),
            p=int(p_raw),
            maxmem=_SCRYPT_MAXMEM,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# ---------------------------------------------------------------------------
# 用户
# ---------------------------------------------------------------------------
def _clean_username(username: str) -> str:
    value = (username or "").strip()
    if not value:
        raise AuthError("用户名不能为空")
    if len(value) > 64:
        raise AuthError("用户名太长（最多 64 字符）")
    # 允许中英文、数字、._-；不允许空白和斜杠，避免 URL/日志歧义
    if any(char.isspace() for char in value) or "/" in value or "\\" in value:
        raise AuthError("用户名不能包含空白或斜杠")
    return value


async def count_users(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count(AppUser.id)))).scalar_one())


async def get_user_by_username(session: AsyncSession, username: str) -> AppUser | None:
    return (
        await session.execute(
            select(AppUser).where(AppUser.username == _clean_username(username))
        )
    ).scalar_one_or_none()


async def create_user(
    session: AsyncSession,
    *,
    username: str,
    password: str,
    display_name: str = "",
    role: str = "member",
) -> AppUser:
    name = _clean_username(username)
    if role not in ROLES:
        raise AuthError(f"role 只支持 {' / '.join(ROLES)}")
    existing = await get_user_by_username(session, name)
    if existing is not None:
        raise AuthError(f"用户名已存在：{name}")
    user = AppUser(
        username=name,
        display_name=(display_name or "").strip()[:64] or name,
        password_hash=hash_password(password),
        role=role,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def authenticate(session: AsyncSession, username: str, password: str) -> AppUser:
    """登录校验；不区分「用户不存在/密码错」的返回文案（避免枚举账号）。"""
    user = await get_user_by_username(session, username)
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        raise AuthError("用户名或密码不对")
    user.last_login_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(user)
    return user


async def list_users(session: AsyncSession) -> list[AppUser]:
    return list(
        (
            await session.execute(select(AppUser).order_by(AppUser.created_at.asc()))
        ).scalars()
    )


async def set_password(session: AsyncSession, user_id: str, password: str) -> AppUser:
    user = await session.get(AppUser, user_id)
    if user is None:
        raise AuthError("用户不存在")
    user.password_hash = hash_password(password)
    await session.commit()
    await session.refresh(user)
    return user


async def set_active(session: AsyncSession, user_id: str, active: bool) -> AppUser:
    user = await session.get(AppUser, user_id)
    if user is None:
        raise AuthError("用户不存在")
    user.is_active = bool(active)
    await session.commit()
    await session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


async def create_session(session: AsyncSession, user: AppUser) -> str:
    token = secrets.token_urlsafe(32)
    row = UserSession(
        user_id=user.id,
        token_sha256=_hash_token(token),
        expires_at=_now() + timedelta(days=max(1, int(settings.auth_session_days))),
    )
    session.add(row)
    await session.commit()
    return token


async def resolve_session(session: AsyncSession, token: str | None) -> AppUser | None:
    value = (token or "").strip()
    if not value:
        return None
    row = (
        await session.execute(
            select(UserSession).where(UserSession.token_sha256 == _hash_token(value))
        )
    ).scalar_one_or_none()
    if row is None or row.expires_at is None:
        return None
    expires = row.expires_at
    if expires.tzinfo is None:  # SQLite 读回来是 naive
        expires = expires.replace(tzinfo=UTC)
    if expires <= _now():
        return None
    user = await session.get(AppUser, row.user_id)
    if user is None or not user.is_active:
        return None
    row.last_seen_at = _now()
    await session.commit()
    return user


async def revoke_session(session: AsyncSession, token: str | None) -> None:
    value = (token or "").strip()
    if not value:
        return
    row = (
        await session.execute(
            select(UserSession).where(UserSession.token_sha256 == _hash_token(value))
        )
    ).scalar_one_or_none()
    if row is not None:
        await session.delete(row)
        await session.commit()


# ---------------------------------------------------------------------------
# FastAPI 依赖
# ---------------------------------------------------------------------------
async def current_user(request: Request) -> AppUser | None:
    """当前登录用户；认证关闭或中间件未设置时为 None。"""
    return getattr(request.state, "user", None)


async def require_user(request: Request, session: AsyncSession = Depends(get_session)) -> AppUser:
    """必须登录（认证关闭时同样要求？不：关闭时抛 503 而不是放行，避免误用）。"""
    if not settings.auth_enabled:
        raise HTTPException(status_code=503, detail="认证未开启：LITBENCH_AUTH=0 时该接口不可用")
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    return user


async def require_admin(user: AppUser = Depends(require_user)) -> AppUser:
    if not is_superuser(user):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


async def admin_when_enabled(request: Request) -> AppUser | None:
    """认证关闭时放行（单机旧行为）；认证打开后要求管理员/system。

    给「单机时人人可用、多人部署时属于维护者」的接口用：全局模型配置、agent
    任务队列、审计日志、自动接受扫描等。不能用 ``require_admin``——它在认证关闭时
    直接 503，会把单机模式打坏。
    """
    if not settings.auth_enabled:
        return None
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if not is_superuser(user):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def user_to_dict(user: AppUser) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name or user.username,
        "role": user.role,
        "is_active": bool(user.is_active),
        "created_at": user.created_at,
        "last_login_at": user.last_login_at,
    }


__all__ = [
    "ROLES",
    "SUPERUSER_ROLES",
    "SYSTEM_ROLE",
    "SYSTEM_USER_ID",
    "AuthError",
    "admin_when_enabled",
    "authenticate",
    "count_users",
    "create_session",
    "create_user",
    "current_user",
    "get_user_by_username",
    "hash_password",
    "is_superuser",
    "list_users",
    "owner_id_for_write",
    "require_admin",
    "require_user",
    "resolve_session",
    "revoke_session",
    "set_active",
    "set_password",
    "system_user",
    "user_to_dict",
    "verify_password",
]
