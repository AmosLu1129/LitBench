# SPDX-License-Identifier: AGPL-3.0-or-later
"""工作组、共享与数据归属（M7 第二步，2026-11-15）。

这一层只做**判定**，不碰业务规则；路由负责把判定翻译成 404/403。

三条不变量：

1. **认证关闭时一切照旧**（``settings.auth_enabled == False``）：单机/测试模式
   没有「用户」概念，所有查询按旧行为看全局库。这里所有函数在认证关闭时都返回
   「可访问」，路由也因此不用写 ``if auth_enabled`` 分支。
2. **私有库 = 我 owner 的文献 + legacy（owner 为 NULL）**；团队库 = 我所在工作组里
   有人共享过的文献。二者都是**查询时实时判定**，所以取消共享立刻生效。
3. **可见 ≠ 可改**：共享文献对非 owner 只读（笔记/收藏/阅读状态除外——那是
   每人自己的 ``user_paper_state``，改它不影响 owner）。

为什么把判定放进一个模块：文献/图/表/摘要/claim 六七条路由都要用同一套规则，
分散写必然出现「列表过滤了、详情忘了过滤」这种越权洞。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import Select, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import SYSTEM_ROLE, is_superuser
from .config import settings
from .models import (
    AppUser,
    Claim,
    Collection,
    Figure,
    Paper,
    PaperShare,
    PaperVersion,
    UserPaperState,
    Workgroup,
    WorkgroupMember,
)

ROLE_OWNER = "owner"
ROLE_MEMBER = "member"

# 用来区分「没传这个字段」与「传了 None」：PATCH 里 note 清空是合法操作
UNSET: Any = object()


class TeamError(RuntimeError):
    """工作组/共享的可读业务错误（路由统一转 422）。"""


# ---------------------------------------------------------------------------
# 当前用户与权限判定
# ---------------------------------------------------------------------------
def scoped_user(user: AppUser | None) -> AppUser | None:
    """认证关闭 → None（旧行为）；认证打开 → 当前用户本身。"""
    return user if settings.auth_enabled else None


def _deny_when_anon() -> bool:
    """认证打开却没有用户：拒绝（正常请求走不到，除非路由漏了依赖）。"""
    return not settings.auth_enabled


async def is_shared_to_user(session: AsyncSession, paper_id: str, user_id: str) -> bool:
    stmt = (
        select(PaperShare.paper_id)
        .join(
            WorkgroupMember,
            WorkgroupMember.workgroup_id == PaperShare.workgroup_id,
        )
        .where(PaperShare.paper_id == paper_id, WorkgroupMember.user_id == user_id)
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None


async def can_access_paper(
    session: AsyncSession, user: AppUser | None, paper: Paper
) -> bool:
    """能看见这篇文献（own / legacy / 共享到我的组 / 管理员）。"""
    current = scoped_user(user)
    if current is None:
        return _deny_when_anon()
    if is_superuser(current):
        return True
    if paper.owner_user_id in (None, current.id):
        return True
    return await is_shared_to_user(session, paper.id, current.id)


async def can_edit_paper(user: AppUser | None, paper: Paper) -> bool:
    """能改文献本身（共享/取消共享/删除/著录字段/增删图）。"""
    current = scoped_user(user)
    if current is None:
        return _deny_when_anon()
    if is_superuser(current):
        return True
    return paper.owner_user_id == current.id


def private_paper_condition(user: AppUser | None):
    """私有库列表条件：我的文献 + legacy（owner NULL）。

    system（MCP 内部令牌）没有个人库 → 直接全库；admin 仍只看自己的，避免误操作。
    """
    current = scoped_user(user)
    if current is None:
        return true()
    if current.role == SYSTEM_ROLE:
        return true()
    return or_(Paper.owner_user_id.is_(None), Paper.owner_user_id == current.id)


def visible_paper_condition(user: AppUser | None):
    """所有能看见的文献：私有库 + 共享到我所在工作组的。"""
    current = scoped_user(user)
    if current is None:
        return true()
    if is_superuser(current):
        # 管理员详情可看全部；列表接口仍会显式用 private_paper_condition
        return true()
    shared = (
        select(PaperShare.paper_id)
        .join(
            WorkgroupMember,
            WorkgroupMember.workgroup_id == PaperShare.workgroup_id,
        )
        .where(WorkgroupMember.user_id == current.id)
    )
    return or_(
        Paper.owner_user_id.is_(None),
        Paper.owner_user_id == current.id,
        Paper.id.in_(shared),
    )


def workgroup_paper_condition(workgroup_id: str):
    return Paper.id.in_(
        select(PaperShare.paper_id).where(PaperShare.workgroup_id == workgroup_id)
    )


async def ensure_paper_access(
    session: AsyncSession, user: AppUser | None, paper_id: str
) -> Paper:
    """取文献并校验可见性；看不见统一 404（不用 403，避免暴露存在性）。"""
    paper = await session.get(Paper, paper_id)
    if paper is None or not await can_access_paper(session, user, paper):
        raise HTTPException(status_code=404, detail="paper not found")
    return paper


async def ensure_paper_edit(
    session: AsyncSession, user: AppUser | None, paper_id: str
) -> Paper:
    paper = await ensure_paper_access(session, user, paper_id)
    if not await can_edit_paper(user, paper):
        raise HTTPException(status_code=403, detail="只有文献 owner 或管理员能修改")
    return paper


async def ensure_paper_writable(user: AppUser | None, paper: Paper) -> None:
    """已经拿到 paper 对象时的可写校验（避免再查一次）。"""
    if not await can_edit_paper(user, paper):
        raise HTTPException(status_code=403, detail="只有文献 owner 或管理员能修改")


async def paper_owner_names(
    session: AsyncSession, papers: list[Paper]
) -> dict[str, str]:
    ids = {paper.owner_user_id for paper in papers if paper.owner_user_id}
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(AppUser.id, AppUser.display_name, AppUser.username).where(
                AppUser.id.in_(ids)
            )
        )
    ).all()
    return {uid: (display or username or uid) for uid, display, username in rows}


async def paper_states(
    session: AsyncSession, user: AppUser | None, paper_ids: list[str]
) -> dict[str, UserPaperState]:
    """当前用户对这些文献的私有状态（认证关闭时为空）。"""
    current = scoped_user(user)
    if current is None or not paper_ids:
        return {}
    rows = (
        await session.execute(
            select(UserPaperState).where(
                UserPaperState.user_id == current.id,
                UserPaperState.paper_id.in_(paper_ids),
            )
        )
    ).scalars()
    return {row.paper_id: row for row in rows}


async def set_paper_state(
    session: AsyncSession,
    user: AppUser | None,
    paper_id: str,
    *,
    note: Any = UNSET,
    favorite: Any = UNSET,
    reading_status: Any = UNSET,
) -> UserPaperState | None:
    """写当前用户的私有状态；认证关闭时返回 None（调用方回退到 paper 字段）。"""
    current = scoped_user(user)
    if current is None:
        return None
    state = await session.get(UserPaperState, (current.id, paper_id))
    if state is None:
        state = UserPaperState(user_id=current.id, paper_id=paper_id)
        session.add(state)
    if note is not UNSET:
        state.note = note
    if favorite is not UNSET:
        state.favorite = bool(favorite)
    if reading_status is not UNSET:
        state.reading_status = reading_status
    return state


async def shared_workgroup_map(
    session: AsyncSession, user: AppUser | None, paper_ids: list[str]
) -> dict[str, list[str]]:
    """paper_id → 我所在工作组里共享了它的组 id（给前端显示共享状态）。"""
    current = scoped_user(user)
    if current is None or not paper_ids:
        return {}
    rows = (
        await session.execute(
            select(PaperShare.paper_id, PaperShare.workgroup_id)
            .join(
                WorkgroupMember,
                WorkgroupMember.workgroup_id == PaperShare.workgroup_id,
            )
            .where(
                PaperShare.paper_id.in_(paper_ids),
                WorkgroupMember.user_id == current.id,
            )
        )
    ).all()
    out: dict[str, list[str]] = {}
    for paper_id, workgroup_id in rows:
        out.setdefault(paper_id, []).append(workgroup_id)
    return out


# ---------------------------------------------------------------------------
# 工作组
# ---------------------------------------------------------------------------
async def get_workgroup(session: AsyncSession, workgroup_id: str) -> Workgroup:
    workgroup = await session.get(Workgroup, workgroup_id)
    if workgroup is None:
        raise HTTPException(status_code=404, detail="workgroup not found")
    return workgroup


async def membership(
    session: AsyncSession, workgroup_id: str, user_id: str
) -> WorkgroupMember | None:
    return await session.get(WorkgroupMember, (workgroup_id, user_id))


async def require_workgroup_member(
    session: AsyncSession, user: AppUser, workgroup_id: str
) -> tuple[Workgroup, WorkgroupMember | None]:
    workgroup = await get_workgroup(session, workgroup_id)
    if not settings.auth_enabled:
        raise HTTPException(status_code=503, detail="认证未开启：工作组功能需要账号")
    member = await membership(session, workgroup_id, user.id)
    if member is None and not is_superuser(user):
        # 不是成员就当作不存在，不暴露组名/成员
        raise HTTPException(status_code=404, detail="workgroup not found")
    return workgroup, member


async def require_workgroup_manager(
    session: AsyncSession, user: AppUser, workgroup_id: str
) -> Workgroup:
    workgroup, member = await require_workgroup_member(session, user, workgroup_id)
    if not is_superuser(user) and (member is None or member.role != ROLE_OWNER):
        raise HTTPException(status_code=403, detail="只有工作组 owner 或管理员能管理成员")
    return workgroup


async def workgroup_ids_for_user(session: AsyncSession, user: AppUser) -> list[str]:
    rows = (
        await session.execute(
            select(WorkgroupMember.workgroup_id).where(WorkgroupMember.user_id == user.id)
        )
    ).scalars()
    return list(rows)


async def workgroup_members(session: AsyncSession, workgroup_id: str) -> list[dict]:
    rows = (
        await session.execute(
            select(WorkgroupMember, AppUser)
            .join(AppUser, WorkgroupMember.user_id == AppUser.id)
            .where(WorkgroupMember.workgroup_id == workgroup_id)
            .order_by(WorkgroupMember.joined_at.asc())
        )
    ).all()
    return [
        {
            "user_id": member.user_id,
            "username": account.username,
            "display_name": account.display_name or account.username,
            "role": member.role,
            "joined_at": member.joined_at,
        }
        for member, account in rows
    ]


async def workgroup_member_count(session: AsyncSession, workgroup_id: str) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(WorkgroupMember)
                .where(WorkgroupMember.workgroup_id == workgroup_id)
            )
        ).scalar()
        or 0
    )


async def workgroup_share_count(session: AsyncSession, workgroup_id: str) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(PaperShare)
                .where(PaperShare.workgroup_id == workgroup_id)
            )
        ).scalar()
        or 0
    )


async def add_workgroup_member(
    session: AsyncSession,
    workgroup: Workgroup,
    *,
    target: AppUser,
    role: str = ROLE_MEMBER,
) -> WorkgroupMember:
    if role not in (ROLE_OWNER, ROLE_MEMBER):
        raise TeamError(f"role 只支持 {ROLE_OWNER} / {ROLE_MEMBER}")
    existing = await membership(session, workgroup.id, target.id)
    if existing is not None:
        raise TeamError(f"{target.username} 已经在工作组里")
    member = WorkgroupMember(workgroup_id=workgroup.id, user_id=target.id, role=role)
    session.add(member)
    await session.commit()
    await session.refresh(member)
    return member


# ---------------------------------------------------------------------------
# 共享
# ---------------------------------------------------------------------------
async def share_paper(
    session: AsyncSession,
    user: AppUser,
    paper: Paper,
    workgroup_id: str,
) -> tuple[PaperShare, bool]:
    """把文献共享到工作组；重复共享幂等（返回 created=False）。"""
    workgroup, member = await require_workgroup_member(session, user, workgroup_id)
    if not await can_edit_paper(user, paper):
        raise HTTPException(status_code=403, detail="只有文献 owner 或管理员能共享")
    # 管理员/内部服务可以替不在组里的用户共享；普通用户必须先是组员
    if member is None and not is_superuser(user):
        raise HTTPException(status_code=403, detail="需要先加入这个工作组")
    existing = await session.get(PaperShare, (paper.id, workgroup.id))
    if existing is not None:
        return existing, False
    share = PaperShare(paper_id=paper.id, workgroup_id=workgroup.id, shared_by=user.id)
    session.add(share)
    await session.commit()
    await session.refresh(share)
    return share, True


async def unshare_paper(
    session: AsyncSession,
    user: AppUser,
    paper: Paper,
    workgroup_id: str,
) -> bool:
    """取消共享；返回是否真的删了一条。"""
    if not await can_edit_paper(user, paper):
        raise HTTPException(status_code=403, detail="只有文献 owner 或管理员能取消共享")
    workgroup = await get_workgroup(session, workgroup_id)
    share = await session.get(PaperShare, (paper.id, workgroup.id))
    if share is None:
        return False
    await session.delete(share)
    await session.commit()
    return True


async def paper_shares(session: AsyncSession, paper_id: str) -> list[dict]:
    rows = (
        await session.execute(
            select(PaperShare, Workgroup, AppUser)
            .join(Workgroup, PaperShare.workgroup_id == Workgroup.id)
            .outerjoin(AppUser, PaperShare.shared_by == AppUser.id)
            .where(PaperShare.paper_id == paper_id)
            .order_by(PaperShare.shared_at.asc())
        )
    ).all()
    return [
        {
            "paper_id": share.paper_id,
            "workgroup_id": workgroup.id,
            "workgroup_name": workgroup.name,
            "shared_by": share.shared_by,
            "shared_by_name": (
                (account.display_name or account.username) if account else None
            ),
            "shared_at": share.shared_at,
        }
        for share, workgroup, account in rows
    ]


# ---------------------------------------------------------------------------
# 知识库目录 / 写作卡片
# ---------------------------------------------------------------------------
async def can_access_collection(
    session: AsyncSession, user: AppUser | None, collection: Collection
) -> bool:
    current = scoped_user(user)
    if current is None:
        return _deny_when_anon()
    if is_superuser(current):
        return True
    return collection.owner_user_id in (None, current.id)


async def can_edit_collection(user: AppUser | None, collection: Collection) -> bool:
    current = scoped_user(user)
    if current is None:
        return _deny_when_anon()
    if is_superuser(current):
        return True
    return collection.owner_user_id == current.id


def private_collection_condition(user: AppUser | None):
    current = scoped_user(user)
    if current is None:
        return true()
    if current.role == SYSTEM_ROLE:
        return true()
    return or_(Collection.owner_user_id.is_(None), Collection.owner_user_id == current.id)


def can_access_claim(user: AppUser | None, claim: Claim) -> bool:
    current = scoped_user(user)
    if current is None:
        return _deny_when_anon()
    if is_superuser(current):
        return True
    return claim.owner_user_id in (None, current.id)


def can_edit_claim(user: AppUser | None, claim: Claim) -> bool:
    current = scoped_user(user)
    if current is None:
        return _deny_when_anon()
    if is_superuser(current):
        return True
    return claim.owner_user_id == current.id


def private_claim_condition(user: AppUser | None):
    current = scoped_user(user)
    if current is None:
        return true()
    if current.role == SYSTEM_ROLE:
        return true()
    return or_(Claim.owner_user_id.is_(None), Claim.owner_user_id == current.id)


async def ensure_claim_access(
    session: AsyncSession, user: AppUser | None, claim: Claim
) -> None:
    """claim 读门（SOTA 审查 F2）：owner / 管理员直通；NULL-owner 旧卡跟随文献可见性。

    早期 ``can_access_claim`` 对 NULL-owner 一律放行，团队模式下任何人只要拿到 id
    就能读别人私有文献上的旧卡（update/delete 还会用 403 暴露存在性）。这里统一
    先过 owner 判定，再对旧卡补一次文献可见性，看不见一律 404。
    """
    if not can_access_claim(user, claim):
        raise HTTPException(status_code=404, detail="claim not found")
    current = scoped_user(user)
    if current is not None and not is_superuser(current) and claim.owner_user_id is None:
        await ensure_paper_access(session, user, claim.paper_id)


# ---------------------------------------------------------------------------
# 图 / 摘要：它们的可见性完全跟随 paper，这里只做「图 → paper」的一次跳转
# ---------------------------------------------------------------------------
async def paper_id_for_figure(session: AsyncSession, figure_id: str) -> str | None:
    return (
        await session.execute(
            select(PaperVersion.paper_id)
            .join(Figure, Figure.paper_version_id == PaperVersion.id)
            .where(Figure.id == figure_id)
        )
    ).scalar_one_or_none()


async def ensure_figure_access(
    session: AsyncSession, user: AppUser | None, figure_id: str
) -> None:
    current = scoped_user(user)
    if current is None and not settings.auth_enabled:
        return
    paper_id = await paper_id_for_figure(session, figure_id)
    if paper_id is None:
        raise HTTPException(status_code=404, detail="figure not found")
    await ensure_paper_access(session, user, paper_id)


async def ensure_figure_edit(
    session: AsyncSession, user: AppUser | None, figure_id: str
) -> None:
    current = scoped_user(user)
    if current is None and not settings.auth_enabled:
        return
    paper_id = await paper_id_for_figure(session, figure_id)
    if paper_id is None:
        raise HTTPException(status_code=404, detail="figure not found")
    await ensure_paper_edit(session, user, paper_id)


async def ensure_summary_access(
    session: AsyncSession, user: AppUser | None, summary
) -> None:
    """摘要挂在 paper_version 上；看不见这篇文献就当作摘要不存在。"""
    current = scoped_user(user)
    if current is None and not settings.auth_enabled:
        return
    version_id = getattr(summary, "paper_version_id", None)
    if not version_id:
        raise HTTPException(status_code=404, detail="summary not found")
    version = await session.get(PaperVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="summary not found")
    await ensure_paper_access(session, user, version.paper_id)


async def ensure_summary_edit(
    session: AsyncSession, user: AppUser | None, summary
) -> None:
    current = scoped_user(user)
    if current is None and not settings.auth_enabled:
        return
    version_id = getattr(summary, "paper_version_id", None)
    version = await session.get(PaperVersion, version_id) if version_id else None
    if version is None:
        raise HTTPException(status_code=404, detail="summary not found")
    await ensure_paper_edit(session, user, version.paper_id)


def apply_paper_scope(stmt: Select, condition) -> Select:
    """把可见性条件贴到任何以 Paper 为主表的 select 上（小工具，读起来顺一点）。"""
    return stmt.where(condition)


__all__ = [
    "ROLE_MEMBER",
    "ROLE_OWNER",
    "TeamError",
    "UNSET",
    "add_workgroup_member",
    "apply_paper_scope",
    "can_access_claim",
    "can_access_collection",
    "can_access_paper",
    "can_edit_claim",
    "can_edit_collection",
    "can_edit_paper",
    "ensure_claim_access",
    "ensure_figure_access",
    "ensure_figure_edit",
    "ensure_paper_access",
    "ensure_paper_edit",
    "ensure_paper_writable",
    "ensure_summary_access",
    "ensure_summary_edit",
    "get_workgroup",
    "is_shared_to_user",
    "membership",
    "paper_id_for_figure",
    "paper_owner_names",
    "paper_shares",
    "paper_states",
    "private_claim_condition",
    "private_collection_condition",
    "private_paper_condition",
    "require_workgroup_manager",
    "require_workgroup_member",
    "scoped_user",
    "set_paper_state",
    "share_paper",
    "shared_workgroup_map",
    "unshare_paper",
    "visible_paper_condition",
    "workgroup_ids_for_user",
    "workgroup_member_count",
    "workgroup_members",
    "workgroup_paper_condition",
    "workgroup_share_count",
]
