# SPDX-License-Identifier: AGPL-3.0-or-later
"""worker 内置 agent 任务执行器（M3）。

M3 先用服务端内置 worker 同步调 LLM（单任务、可重试，够本机用）；外部 agent 领任务
走 M5 的 MCP。执行器与解析 job 共用同一个 worker 进程：每轮先消费解析 job，队列空时
再领取一个 agent 任务。
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from ..auto_accept import sweep_auto_accept
from ..bbox_review import ai_review_bbox
from ..db import get_sessionmaker
from ..figure_review import ai_review_figure
from ..models import AgentTask
from ..tasks import (
    TaskError,
    claim_task,
    complete_task,
    fail_task,
)

log = logging.getLogger("litbench.worker.tasks")

WORKER_ACTOR = "worker"


async def _run_figure_review(db, task: AgentTask) -> dict:
    payload = task.payload_json or {}
    figure_id = str(payload.get("figure_id") or task.target_id or "")
    if not figure_id:
        raise TaskError("figure_review 任务缺少 figure_id")
    proposal = await ai_review_figure(db, figure_id)
    return {
        "proposal_id": proposal.get("proposal_id"),
        "status": proposal.get("status"),
        "confidence": proposal.get("confidence"),
        "auto_accepted": proposal.get("auto_accepted"),
        "applied": (proposal.get("proposed") or {}).get("needs_change"),
        "cost": float(proposal.get("cost") or 0.0),
    }


async def _run_auto_accept_sweep(db, task: AgentTask) -> dict:
    payload = task.payload_json or {}
    return await sweep_auto_accept(
        db,
        limit=int(payload.get("limit") or 50),
        paper_id=payload.get("paper_id"),
    )


async def _run_bbox_review(db, task: AgentTask) -> dict:
    payload = task.payload_json or {}
    figure_id = str(payload.get("figure_id") or task.target_id or "")
    if not figure_id:
        raise TaskError("figure_bbox_review 任务缺少 figure_id")
    proposal = await ai_review_bbox(db, figure_id)
    return {
        "proposal_id": proposal.get("proposal_id"),
        "kind": proposal.get("kind"),
        "status": proposal.get("status"),
        "confidence": proposal.get("confidence"),
        "auto_accepted": proposal.get("auto_accepted"),
        "iou": (proposal.get("proposed") or {}).get("iou"),
        "cost": float(proposal.get("cost") or 0.0),
    }


async def run_task_once() -> str | None:
    """领取并执行一个 agent 任务；队列空返回 None。"""
    async with get_sessionmaker()() as db:
        task = await claim_task(db, actor=WORKER_ACTOR)
        if task is None:
            return None
        task_id = task.id
        try:
            if task.kind == "figure_review":
                result = await _run_figure_review(db, task)
            elif task.kind == "figure_bbox_review":
                result = await _run_bbox_review(db, task)
            elif task.kind == "auto_accept_sweep":
                result = await _run_auto_accept_sweep(db, task)
            else:  # pragma: no cover - claim 白名单已挡
                raise TaskError(f"未知任务类型 {task.kind!r}")
            await complete_task(
                db,
                task_id,
                actor=WORKER_ACTOR,
                result=result,
                cost=float((result or {}).get("cost") or 0.0),
            )
            log.info("agent task %s done: %s", task_id, task.kind)
            return "done"
        except Exception as exc:  # noqa: BLE001 - worker 必须兜住所有异常
            await db.rollback()
            try:
                failed = await fail_task(
                    db,
                    task_id,
                    actor=WORKER_ACTOR,
                    error=f"{type(exc).__name__}: {exc}",
                )
                log.warning(
                    "agent task %s failed（第 %s/%s 次，status=%s）：%s",
                    task_id,
                    failed.attempts,
                    failed.max_attempts,
                    failed.status,
                    exc,
                )
            except Exception:  # pragma: no cover - 记录失败本身失败
                log.exception("agent task %s 失败且无法写回状态", task_id)
            return "failed"


async def count_queued() -> int:
    """队列里还有多少任务（worker 空转时避免每轮都查全表）。"""
    from sqlalchemy import func  # noqa: PLC0415

    async with get_sessionmaker()() as db:
        return int(
            (
                await db.execute(
                    select(func.count(AgentTask.id)).where(AgentTask.status == "queued")
                )
            ).scalar_one()
        )


async def reset_task_for_test(task_id: str) -> None:  # pragma: no cover - 测试辅助
    """把任务重置为 queued（仅测试/手工排查用）。"""
    async with get_sessionmaker()() as db:
        task = await db.get(AgentTask, task_id)
        if task is not None:
            task.status = "queued"
            task.actor = None
            task.lease_until = None
            await db.commit()


__all__ = ["WORKER_ACTOR", "count_queued", "run_task_once"]
