# SPDX-License-Identifier: AGPL-3.0-or-later
"""agent 任务 + 租约（M3）。

为什么不用 ``job`` 表：``job`` 是解析流水线（秒级幂等、没有「谁在跑」的概念）；
agent 任务要记录租约、心跳、重试次数、执行者，还要能被外部 agent（M5 MCP）领取。

租约语义：

* ``claim_task`` 把 queued（或租约已过期的 running）任务置为 running、attempts+1、
  lease_until=now+lease；并发 claim 用 ``UPDATE ... WHERE status/lease`` 的条件更新保证
  只有一个赢家（SQLite 单写者 + rowcount 判定）；
* 领取者必须周期 ``heartbeat`` 续租；进程被杀后租约过期 → 任务可被重新领取，attempts
  超过 ``max_attempts`` 则标记 failed（不无限重试）；
* ``complete``/``fail`` 都要求任务当前处于 running；actor 不匹配不写（防止旧进程覆盖新结果）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .models import AgentTask

# 允许的 kind 白名单（未知 kind 直接 422，避免任务表变成垃圾场）
TASK_KINDS = ("figure_review", "figure_bbox_review", "auto_accept_sweep")
TERMINAL_STATUSES = ("done", "failed", "canceled")


class TaskError(RuntimeError):
    """任务操作的可读错误（找不到 / 状态不对 / 不是领取者）。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def task_to_dict(task: AgentTask) -> dict:
    return {
        "id": task.id,
        "kind": task.kind,
        "target_type": task.target_type,
        "target_id": task.target_id,
        "payload": task.payload_json or {},
        "status": task.status,
        "priority": task.priority,
        "idempotency_key": task.idempotency_key,
        "attempts": task.attempts,
        "max_attempts": task.max_attempts,
        "actor": task.actor,
        "lease_until": task.lease_until,
        "heartbeat_at": task.heartbeat_at,
        "result": task.result_json,
        "error": task.error,
        "cost": task.cost,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
    }


async def create_task(
    session: AsyncSession,
    *,
    kind: str,
    target_type: str = "figure",
    target_id: str = "",
    payload: dict | None = None,
    priority: int = 0,
    max_attempts: int | None = None,
    idempotency_key: str | None = None,
) -> tuple[AgentTask, bool]:
    """排一个任务。返回 ``(task, created)``；幂等键命中时返回已有任务且 created=False。

    不 commit：调用方（API/批量）决定事务边界。
    """
    kind = (kind or "").strip()
    if kind not in TASK_KINDS:
        raise TaskError(f"kind 只支持 {' / '.join(TASK_KINDS)}，收到 {kind!r}")
    if idempotency_key:
        existing = (
            await session.execute(
                select(AgentTask).where(AgentTask.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False
    task = AgentTask(
        kind=kind,
        target_type=(target_type or "figure")[:32],
        target_id=str(target_id or "")[:32],
        payload_json=payload or {},
        priority=int(priority or 0),
        max_attempts=int(max_attempts or settings.task_max_attempts),
        idempotency_key=idempotency_key,
    )
    session.add(task)
    await session.flush()
    return task, True


async def claim_task(
    session: AsyncSession,
    *,
    actor: str,
    lease_seconds: int | None = None,
    kind: str | None = None,
) -> AgentTask | None:
    """领取一个任务（优先高 priority、先进先出；含租约过期的 running）。"""
    now = _now()
    lease = int(lease_seconds or settings.task_lease_seconds)
    stmt = (
        select(AgentTask.id)
        .where(
            or_(
                AgentTask.status == "queued",
                and_(AgentTask.status == "running", AgentTask.lease_until < now),
            )
        )
        .order_by(AgentTask.priority.desc(), AgentTask.created_at.asc())
        .limit(1)
    )
    if kind:
        stmt = stmt.where(AgentTask.kind == kind)
    for _ in range(5):
        task_id = (await session.execute(stmt)).scalar_one_or_none()
        if task_id is None:
            return None
        result = await session.execute(
            update(AgentTask)
            .where(
                AgentTask.id == task_id,
                or_(
                    AgentTask.status == "queued",
                    and_(AgentTask.status == "running", AgentTask.lease_until < now),
                ),
            )
            .values(
                status="running",
                actor=(actor or "worker")[:64],
                attempts=AgentTask.attempts + 1,
                started_at=now,
                heartbeat_at=now,
                lease_until=now + timedelta(seconds=lease),
                error=None,
            )
        )
        if result.rowcount == 1:
            await session.commit()
            task = await session.get(AgentTask, task_id)
            return task
        # 被别人抢走 → 重试下一轮
    return None


async def claim_task_by_id(
    session: AsyncSession,
    task_id: str,
    *,
    actor: str,
    lease_seconds: int | None = None,
) -> AgentTask:
    """按 id 领取指定任务（外部 agent 用；内置 worker 用 claim_task 领最早一条）。"""
    now = _now()
    lease = int(lease_seconds or settings.task_lease_seconds)
    result = await session.execute(
        update(AgentTask)
        .where(
            AgentTask.id == task_id,
            or_(
                AgentTask.status == "queued",
                and_(AgentTask.status == "running", AgentTask.lease_until < now),
            ),
        )
        .values(
            status="running",
            actor=(actor or "worker")[:64],
            attempts=AgentTask.attempts + 1,
            started_at=now,
            heartbeat_at=now,
            lease_until=now + timedelta(seconds=lease),
            error=None,
        )
    )
    if result.rowcount != 1:
        raise TaskError("任务已被领取或已结束（claim 被拒绝）")
    await session.commit()
    task = await session.get(AgentTask, task_id)
    if task is None:  # pragma: no cover
        raise TaskError("task not found")
    return task


async def heartbeat_task(
    session: AsyncSession,
    task_id: str,
    *,
    actor: str,
    lease_seconds: int | None = None,
) -> AgentTask:
    """续租；只有当前 running 且 actor 一致才允许（防止旧进程覆盖新领取者）。"""
    now = _now()
    lease = int(lease_seconds or settings.task_lease_seconds)
    result = await session.execute(
        update(AgentTask)
        .where(
            AgentTask.id == task_id,
            AgentTask.status == "running",
            AgentTask.actor == (actor or "worker"),
        )
        .values(heartbeat_at=now, lease_until=now + timedelta(seconds=lease))
    )
    if result.rowcount != 1:
        raise TaskError("任务不在运行中，或不是当前领取者（heartbeat 被拒绝）")
    await session.commit()
    task = await session.get(AgentTask, task_id)
    if task is None:  # pragma: no cover - 行刚存在
        raise TaskError("task not found")
    return task


async def complete_task(
    session: AsyncSession,
    task_id: str,
    *,
    actor: str,
    result: dict | None = None,
    cost: float = 0.0,
) -> AgentTask:
    now = _now()
    updated = await session.execute(
        update(AgentTask)
        .where(
            AgentTask.id == task_id,
            AgentTask.status == "running",
            AgentTask.actor == (actor or "worker"),
        )
        .values(
            status="done",
            result_json=result or {},
            error=None,
            cost=float(cost or 0.0),
            finished_at=now,
            lease_until=None,
            heartbeat_at=now,
        )
    )
    if updated.rowcount != 1:
        raise TaskError("任务不在运行中，或不是当前领取者（complete 被拒绝）")
    await session.commit()
    task = await session.get(AgentTask, task_id)
    if task is None:  # pragma: no cover
        raise TaskError("task not found")
    return task


async def fail_task(
    session: AsyncSession,
    task_id: str,
    *,
    actor: str,
    error: str,
    retry: bool = True,
) -> AgentTask:
    """失败：还有重试次数就回 queued（attempts 已在 claim 时 +1），否则 failed。"""
    now = _now()
    task = await session.get(AgentTask, task_id)
    if task is None:
        raise TaskError("task not found")
    if task.status != "running" or task.actor != (actor or "worker"):
        raise TaskError("任务不在运行中，或不是当前领取者（fail 被拒绝）")
    task.error = (error or "")[:2000]
    if retry and task.attempts < task.max_attempts:
        task.status = "queued"
        task.actor = None
        task.lease_until = None
        task.finished_at = None
    else:
        task.status = "failed"
        task.finished_at = now
        task.lease_until = None
    task.heartbeat_at = now
    await session.commit()
    await session.refresh(task)
    return task


async def recover_expired_tasks(session: AsyncSession) -> int:
    """把租约过期仍处于 running 的任务回收：还有次数回 queued，否则 failed。"""
    now = _now()
    tasks = (
        await session.execute(
            select(AgentTask).where(
                AgentTask.status == "running", AgentTask.lease_until < now
            )
        )
    ).scalars().all()
    for task in tasks:
        if task.attempts < task.max_attempts:
            task.status = "queued"
            task.actor = None
            task.lease_until = None
        else:
            task.status = "failed"
            task.finished_at = now
            task.lease_until = None
            task.error = task.error or "租约过期且超过最大重试次数"
    if tasks:
        await session.commit()
    return len(tasks)


async def get_task(session: AsyncSession, task_id: str) -> AgentTask:
    task = await session.get(AgentTask, task_id)
    if task is None:
        raise TaskError("task not found")
    return task


async def retry_task(session: AsyncSession, task_id: str) -> AgentTask:
    """人工重试一个 failed/canceled 任务：清空错误与租约，attempts 归零。

    只允许终态（failed/canceled）重排：运行中的任务重排会和正在跑的 worker 抢同一条记录。
    queued/done 不处理，报可读错误，避免 UI 上点了没反应。
    """
    task = await get_task(session, task_id)
    if task.status not in ("failed", "canceled"):
        raise TaskError(f"只有 failed / canceled 的任务能重试（当前 {task.status}）")
    task.status = "queued"
    task.attempts = 0
    task.actor = None
    task.lease_until = None
    task.heartbeat_at = None
    task.started_at = None
    task.finished_at = None
    task.error = None
    task.cost = 0.0
    await session.commit()
    await session.refresh(task)
    return task


async def list_tasks(
    session: AsyncSession,
    *,
    status: str | None = None,
    kind: str | None = None,
    target_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    stmt = (
        select(AgentTask)
        .order_by(AgentTask.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if status:
        stmt = stmt.where(AgentTask.status == status)
    if kind:
        stmt = stmt.where(AgentTask.kind == kind)
    if target_id:
        stmt = stmt.where(AgentTask.target_id == target_id)
    rows = (await session.execute(stmt)).scalars().all()
    return [task_to_dict(task) for task in rows]


__all__ = [
    "TASK_KINDS",
    "TaskError",
    "claim_task",
    "claim_task_by_id",
    "complete_task",
    "create_task",
    "fail_task",
    "get_task",
    "heartbeat_task",
    "list_tasks",
    "recover_expired_tasks",
    "task_to_dict",
]
