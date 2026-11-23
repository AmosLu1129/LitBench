# SPDX-License-Identifier: AGPL-3.0-or-later
"""工作组与文献共享 API（M7 第二步，2026-11-15）。

接口形状（docs/17 §10.3）：

* ``GET/POST /workgroups``、``GET/PATCH/DELETE /workgroups/{id}``；
* ``POST /workgroups/{id}/members``、``DELETE /workgroups/{id}/members/{user_id}``；
* ``POST /papers/{id}/share``、``DELETE /papers/{id}/share/{workgroup_id}``、
  ``GET /papers/{id}/shares``。

这些接口都必须登录（认证关闭时返回 503，而不是悄悄放宽）——工作组本身就是按人
划权限的，没有「匿名工作组」这回事。判定逻辑全在 ``litbench.team``，这里只做
参数校验和 404/403 的翻译。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import AuthError, get_user_by_username, is_superuser, require_user
from ..db import get_session
from ..models import AppUser, Paper, PaperShare, Workgroup, WorkgroupMember
from ..schemas import (
    PaperShareIn,
    PaperShareOut,
    PaperShareResultOut,
    WorkgroupDetailOut,
    WorkgroupIn,
    WorkgroupMemberIn,
    WorkgroupMemberOut,
    WorkgroupOut,
    WorkgroupPatch,
)
from ..team import (
    ROLE_OWNER,
    TeamError,
    add_workgroup_member,
    can_access_paper,
    ensure_paper_access,
    membership,
    paper_shares,
    require_workgroup_manager,
    require_workgroup_member,
    share_paper,
    unshare_paper,
    workgroup_member_count,
    workgroup_members,
    workgroup_share_count,
)

router = APIRouter(tags=["team"])


def _error(exc: TeamError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


async def _workgroup_out(
    session: AsyncSession, workgroup: Workgroup, user: AppUser
) -> WorkgroupOut:
    member = await membership(session, workgroup.id, user.id)
    creator = (
        await session.get(AppUser, workgroup.created_by) if workgroup.created_by else None
    )
    return WorkgroupOut(
        id=workgroup.id,
        name=workgroup.name,
        description=workgroup.description,
        created_by=workgroup.created_by,
        created_by_name=(creator.display_name or creator.username) if creator else None,
        role=member.role if member else ("admin" if is_superuser(user) else "member"),
        member_count=await workgroup_member_count(session, workgroup.id),
        paper_count=await workgroup_share_count(session, workgroup.id),
        created_at=workgroup.created_at,
    )


async def _visible_shares(
    session: AsyncSession, user: AppUser, paper: Paper
) -> list[dict]:
    """owner/管理员看全部共享；普通成员只看自己所在组的共享。"""
    rows = await paper_shares(session, paper.id)
    if is_superuser(user) or paper.owner_user_id == user.id:
        return rows
    my_groups = set(
        (
            await session.execute(
                select(WorkgroupMember.workgroup_id).where(
                    WorkgroupMember.user_id == user.id
                )
            )
        ).scalars()
    )
    return [row for row in rows if row["workgroup_id"] in my_groups]


@router.get("/workgroups", response_model=list[WorkgroupOut])
async def list_workgroups(
    user: AppUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> list[WorkgroupOut]:
    """我加入的工作组（管理员额外能看到全部组，便于管理）。"""
    if is_superuser(user):
        groups = (
            await session.execute(select(Workgroup).order_by(Workgroup.created_at.desc()))
        ).scalars().all()
    else:
        groups = (
            await session.execute(
                select(Workgroup)
                .join(WorkgroupMember, WorkgroupMember.workgroup_id == Workgroup.id)
                .where(WorkgroupMember.user_id == user.id)
                .order_by(Workgroup.created_at.desc())
            )
        ).scalars().all()
    return [await _workgroup_out(session, group, user) for group in groups]


@router.post("/workgroups", response_model=WorkgroupDetailOut, status_code=201)
async def create_workgroup(
    body: WorkgroupIn,
    user: AppUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> WorkgroupDetailOut:
    """建组：创建者自动成为 owner 成员。"""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="工作组名称不能为空")
    workgroup = Workgroup(
        name=name,
        description=(body.description or "").strip() or None,
        created_by=user.id,
    )
    session.add(workgroup)
    await session.flush()
    session.add(
        WorkgroupMember(workgroup_id=workgroup.id, user_id=user.id, role=ROLE_OWNER)
    )
    await session.commit()
    await session.refresh(workgroup)
    out = await _workgroup_out(session, workgroup, user)
    return WorkgroupDetailOut(
        **out.model_dump(),
        members=[
            WorkgroupMemberOut(**row)
            for row in await workgroup_members(session, workgroup.id)
        ],
    )


@router.get("/workgroups/{workgroup_id}", response_model=WorkgroupDetailOut)
async def get_workgroup_detail(
    workgroup_id: str,
    user: AppUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> WorkgroupDetailOut:
    workgroup, _member = await require_workgroup_member(session, user, workgroup_id)
    out = await _workgroup_out(session, workgroup, user)
    return WorkgroupDetailOut(
        **out.model_dump(),
        members=[
            WorkgroupMemberOut(**row)
            for row in await workgroup_members(session, workgroup.id)
        ],
    )


@router.patch("/workgroups/{workgroup_id}", response_model=WorkgroupDetailOut)
async def update_workgroup(
    workgroup_id: str,
    body: WorkgroupPatch,
    user: AppUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> WorkgroupDetailOut:
    workgroup = await require_workgroup_manager(session, user, workgroup_id)
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="工作组名称不能为空")
        workgroup.name = name
    if "description" in body.model_fields_set:
        workgroup.description = (body.description or "").strip() or None
    await session.commit()
    await session.refresh(workgroup)
    out = await _workgroup_out(session, workgroup, user)
    return WorkgroupDetailOut(
        **out.model_dump(),
        members=[
            WorkgroupMemberOut(**row)
            for row in await workgroup_members(session, workgroup.id)
        ],
    )


@router.delete("/workgroups/{workgroup_id}")
async def delete_workgroup(
    workgroup_id: str,
    user: AppUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """解散工作组：成员与共享关系一起删（文献本身不动）。"""
    workgroup = await require_workgroup_manager(session, user, workgroup_id)
    for share in (
        await session.execute(
            select(PaperShare).where(PaperShare.workgroup_id == workgroup.id)
        )
    ).scalars():
        await session.delete(share)
    for member in (
        await session.execute(
            select(WorkgroupMember).where(
                WorkgroupMember.workgroup_id == workgroup.id
            )
        )
    ).scalars():
        await session.delete(member)
    await session.delete(workgroup)
    await session.commit()
    return {"workgroup_id": workgroup_id, "deleted": True}


@router.post(
    "/workgroups/{workgroup_id}/members",
    response_model=WorkgroupMemberOut,
    status_code=201,
)
async def add_member(
    workgroup_id: str,
    body: WorkgroupMemberIn,
    user: AppUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> WorkgroupMemberOut:
    """按用户名加人（管理员可加任何激活账号；owner 只能加已有账号）。"""
    workgroup = await require_workgroup_manager(session, user, workgroup_id)
    try:
        target = await get_user_by_username(session, body.username)
    except AuthError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if target is None:
        raise HTTPException(status_code=404, detail=f"用户不存在：{body.username}")
    if not target.is_active:
        raise HTTPException(status_code=422, detail=f"用户已停用：{target.username}")
    try:
        member = await add_workgroup_member(session, workgroup, target=target)
    except TeamError as exc:
        raise _error(exc) from exc
    return WorkgroupMemberOut(
        user_id=member.user_id,
        username=target.username,
        display_name=target.display_name or target.username,
        role=member.role,
        joined_at=member.joined_at,
    )


@router.delete("/workgroups/{workgroup_id}/members/{user_id}")
async def remove_member(
    workgroup_id: str,
    user_id: str,
    user: AppUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """移除成员 / 自己退出；不允许把最后一个 owner 移走（否则组没人能管）。

    非成员看不到组（统一 404）：先走 ``require_workgroup_member``，避免按 user_id
    探测「某人是不是这个组的成员」。
    """
    workgroup, actor = await require_workgroup_member(session, user, workgroup_id)
    target = await membership(session, workgroup.id, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="该用户不在工作组里")

    is_self = user_id == user.id
    is_manager = is_superuser(user) or (actor is not None and actor.role == ROLE_OWNER)
    if not is_self and not is_manager:
        raise HTTPException(status_code=403, detail="只有工作组 owner 或管理员能移除成员")
    if target.role == ROLE_OWNER:
        owner_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(WorkgroupMember)
                    .where(
                        WorkgroupMember.workgroup_id == workgroup.id,
                        WorkgroupMember.role == ROLE_OWNER,
                    )
                )
            ).scalar()
            or 0
        )
        if owner_count <= 1:
            raise HTTPException(
                status_code=422,
                detail="工作组至少保留一个 owner：先让组里还有人当 owner（v1 暂不支持转让）",
            )

    await session.delete(target)
    await session.commit()
    return {"workgroup_id": workgroup.id, "user_id": user_id, "removed": True}


# ---------------------------------------------------------------------------
# 文献共享
# ---------------------------------------------------------------------------
@router.post(
    "/papers/{paper_id}/share", response_model=PaperShareResultOut, status_code=201
)
async def share_paper_to_workgroup(
    paper_id: str,
    body: PaperShareIn,
    user: AppUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> PaperShareResultOut:
    # 看不见的文献统一 404（与其它按 id 的入口一致）：共享接口不能用来枚举 paper_id
    paper = await ensure_paper_access(session, user, paper_id)
    share, created = await share_paper(session, user, paper, body.workgroup_id)
    rows = await paper_shares(session, paper.id)
    matched = next(
        (row for row in rows if row["workgroup_id"] == share.workgroup_id), None
    )
    if matched is None:  # pragma: no cover - 刚写入必然查得到
        raise HTTPException(status_code=500, detail="共享写入后回读失败")
    return PaperShareResultOut(share=PaperShareOut(**matched), created=created)


@router.delete("/papers/{paper_id}/share/{workgroup_id}")
async def unshare_paper_from_workgroup(
    paper_id: str,
    workgroup_id: str,
    user: AppUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    # 同样先过可见性：别人的私有文献不能靠 unshare 探测是否存在
    paper = await ensure_paper_access(session, user, paper_id)
    removed = await unshare_paper(session, user, paper, workgroup_id)
    return {"paper_id": paper_id, "workgroup_id": workgroup_id, "removed": removed}


@router.get("/papers/{paper_id}/shares", response_model=list[PaperShareOut])
async def list_paper_shares(
    paper_id: str,
    user: AppUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> list[PaperShareOut]:
    paper = await session.get(Paper, paper_id)
    if paper is None or not await can_access_paper(session, user, paper):
        raise HTTPException(status_code=404, detail="paper not found")
    return [
        PaperShareOut(**row)
        for row in await _visible_shares(session, user, paper)
    ]


__all__ = ["router"]
