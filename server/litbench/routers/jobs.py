# SPDX-License-Identifier: AGPL-3.0-or-later
"""任务查询。前端用 SSE 推进度，这里只提供状态快照。

认证打开时按文献可见性过滤：job 挂在 paper_version 上，别人私有库的解析任务
（以及失败原因里的路径/错误信息）不能从列表或按 id 的入口看到。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user, is_superuser
from ..db import get_session
from ..models import AppUser, Job, Paper, PaperVersion
from ..schemas import JobOut
from ..team import can_access_paper, scoped_user, visible_paper_condition

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _to_out(job: Job) -> JobOut:
    payload = job.payload_json or {}
    return JobOut(
        id=job.id,
        kind=job.kind,
        status=job.status,
        progress=job.progress,
        error=job.error,
        attempts=job.attempts,
        paper_version_id=job.paper_version_id,
        created_at=job.created_at,
        stage=payload.get("backend"),
        escalated_from=payload.get("escalated_from"),
        escalated_to=payload.get("escalated_to"),
        # 父 job 写 escalate_reason，子 job 写 reason（它就是被升级的原因）
        escalate_reason=payload.get("escalate_reason") or payload.get("reason"),
    )


def _scope_jobs(stmt: Select, user: AppUser | None) -> Select:
    """认证关闭/管理员/system：全库；普通成员：只看当前可见文献的 job。"""
    current = scoped_user(user)
    if current is None or is_superuser(current):
        return stmt
    visible_versions = (
        select(PaperVersion.id)
        .join(Paper, PaperVersion.paper_id == Paper.id)
        .where(visible_paper_condition(user))
    )
    return stmt.where(Job.paper_version_id.in_(visible_versions))


async def _job_visible(session: AsyncSession, user: AppUser | None, job: Job) -> bool:
    current = scoped_user(user)
    if current is None or is_superuser(current):
        return True
    if not job.paper_version_id:
        return False
    version = await session.get(PaperVersion, job.paper_version_id)
    if version is None:
        return False
    paper = await session.get(Paper, version.paper_id)
    return paper is not None and await can_access_paper(session, user, paper)


@router.get("", response_model=list[JobOut])
async def list_jobs(
    status: str | None = Query(None, description="queued|running|done|failed"),
    limit: int = Query(50, ge=1, le=200),
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[JobOut]:
    stmt = _scope_jobs(select(Job).order_by(Job.created_at.desc()), user)
    if status:
        stmt = stmt.where(Job.status == status)
    stmt = stmt.limit(limit)
    jobs = (await session.execute(stmt)).scalars().all()
    return [_to_out(j) for j in jobs]


@router.get("/{job_id}", response_model=JobOut)
async def get_job(
    job_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> JobOut:
    job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if not job or not await _job_visible(session, user, job):
        raise HTTPException(status_code=404, detail="job not found")
    return _to_out(job)
