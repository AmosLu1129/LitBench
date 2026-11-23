# SPDX-License-Identifier: AGPL-3.0-or-later
"""Agent 协作入口（M3）：AI 纠错建议 + 自动接受/回滚/审计 + agent 任务租约。

链路（用户视角）：
``POST /figures/{id}/ai-review``（对一张图跑一次 AI 质检，产出 pending 建议）
→ 自动接受器立刻过一道（白名单 + 置信 ≥ 阈值 + 证据 + 未人工改过）
→ ``GET /proposals?status=pending``（待审队列，带 before/after）
→ ``POST /proposals/{id}/decide``（采纳/拒绝）或 ``/proposals/{id}/rollback``（回滚）
→ ``GET /audit``（每次正式写都有 before/after）。

M3 正式化新增：
* ``POST /tasks`` / ``/tasks/{id}/claim|heartbeat|complete|fail``：agent 任务租约（内置
  worker 领取执行；M5 外部 agent 复用同一套 API）；
* ``POST /tasks/enqueue-figures``：把一批待确认图排成 AI 复核任务；
* ``POST /proposals/auto-accept`` / ``POST /proposals/decide-batch``：自动接受扫描与批量采纳。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import list_audit, write_audit
from ..auth import admin_when_enabled, current_user
from ..auto_accept import ProposalError, rollback_proposal, sweep_auto_accept
from ..bbox_review import BboxReviewError, ai_review_bbox
from ..db import get_session
from ..figure_review import (
    FigureReviewError,
    _candidate_labels,
    _load_figure,
    _validated,
    ai_review_figure,
    decide_proposal,
    get_proposal,
    list_proposals,
    proposal_to_dict,
)
from ..labeling import LOW_CONFIDENCE
from ..llm import effective_config
from ..models import AgentTask, AppUser, Block, Figure, FigureProposal, Paper, PaperVersion
from ..schemas import (
    AgentTaskOut,
    AuditLogOut,
    AutoAcceptIn,
    AutoAcceptOut,
    FigureOut,
    FigureProposalOut,
    ProposalBatchIn,
    ProposalBatchOut,
    ProposalCreateIn,
    ProposalDecideIn,
    ProposalDecideOut,
    ProposalRollbackIn,
    TaskClaimIn,
    TaskCompleteIn,
    TaskCreateIn,
    TaskEnqueueBboxIn,
    TaskEnqueueFiguresIn,
    TaskEnqueueOut,
    TaskFailIn,
    TaskHeartbeatIn,
)
from ..tasks import (
    TaskError,
    claim_task_by_id,
    complete_task,
    create_task,
    fail_task,
    get_task,
    heartbeat_task,
    list_tasks,
    recover_expired_tasks,
    retry_task,
    task_to_dict,
)
from ..team import (
    ensure_figure_access,
    ensure_figure_edit,
    scoped_user,
    visible_paper_condition,
)
from .figures import _render_crop_to_storage
from .figures import _to_out as figure_to_out

router = APIRouter(tags=["agent"])
log = logging.getLogger("litbench.agent")


async def _rerender_after_bbox(session: AsyncSession, figure_id: str) -> None:
    """采纳/回滚框建议后按新框重裁缩略图（失败不影响主流程）。"""
    row = await _figure_row(session, figure_id)
    if row is None:
        return
    fig, ver, _paper = row
    bbox = fig.bbox_override_json or fig.bbox_json
    if not bbox or len(bbox) != 4:
        return
    try:
        new_path = await asyncio.get_running_loop().run_in_executor(
            None,
            _render_crop_to_storage,
            [float(v) for v in bbox],
            fig.page,
            fig.cite_key,
            ver.file_path,
        )
    except Exception:  # noqa: BLE001 - 缩略图失败不该让采纳/回滚失败
        log.exception("figure %s 重裁缩略图失败", figure_id)
        return
    if new_path:
        fig.image_path = new_path
        await session.commit()
        await session.refresh(fig)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _error(exc: Exception) -> HTTPException:
    message = str(exc)
    return HTTPException(status_code=404 if "not found" in message else 422, detail=message)


def _task_error(exc: Exception) -> HTTPException:
    message = str(exc)
    return HTTPException(status_code=404 if "not found" in message else 409, detail=message)


def _require_llm() -> None:
    """排任务前先确认模型可用：否则任务注定重试到 failed，不如直接告诉用户去设置页。"""
    source, _cfg = effective_config()
    if source == "mock":
        raise HTTPException(
            status_code=422,
            detail="还没有配置模型：请先在右上角「设置」里填 base_url / model / API key",
        )


async def _figure_row(session: AsyncSession, figure_id: str):
    return (
        await session.execute(
            select(Figure, PaperVersion, Paper)
            .join(PaperVersion, Figure.paper_version_id == PaperVersion.id)
            .join(Paper, PaperVersion.paper_id == Paper.id)
            .where(Figure.id == figure_id)
        )
    ).first()


# ---------------------------------------------------------------------------
# AI 纠错建议（原有链路）
# ---------------------------------------------------------------------------
@router.post("/figures/{figure_id}/ai-review", response_model=FigureProposalOut)
async def run_figure_ai_review(
    figure_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FigureProposalOut:
    """对一张图跑 AI 纠错：图号是否读错、图注是否有 OCR 噪声、裁框是否完整。

    只产建议；proposal 落库后立刻过自动接受器（M3 正式化）——满足条件就直接写正式字段
    （返回 status=accepted / auto_accepted=true），否则留在 pending 等人工。
    """
    await ensure_figure_edit(session, user, figure_id)
    try:
        data = await ai_review_figure(session, figure_id)
    except FigureReviewError as exc:
        raise _error(exc) from exc
    return FigureProposalOut(**data)


@router.post("/figures/{figure_id}/bbox-review", response_model=FigureProposalOut)
async def run_figure_bbox_review(
    figure_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FigureProposalOut:
    """M3c 框复核：整页 + 红框标注 + 裁图三张图给多模态模型，判断当前框是否漏框/多框。

    默认只产出 ``kind=figure_bbox`` 的 pending 建议（``BBOX_REVIEW_AUTO_ACCEPT=0``）；
    采纳后才写 ``bbox_override_json`` 并重裁缩略图。
    """
    await ensure_figure_edit(session, user, figure_id)
    try:
        data = await ai_review_bbox(session, figure_id)
    except BboxReviewError as exc:
        raise _error(exc) from exc
    return FigureProposalOut(**data)


@router.get("/figures/{figure_id}/proposals", response_model=list[FigureProposalOut])
async def list_figure_proposals(
    figure_id: str,
    status: str = Query("pending", description="pending|accepted|rejected|superseded|all"),
    limit: int = Query(20, ge=1, le=100),
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[FigureProposalOut]:
    await ensure_figure_access(session, user, figure_id)
    rows = await list_proposals(
        session,
        status="" if status == "all" else status,
        figure_id=figure_id,
        limit=limit,
    )
    return [FigureProposalOut(**row) for row in rows]


@router.get("/proposals", response_model=list[FigureProposalOut])
async def list_all_proposals(
    status: str = Query(
        "pending", description="pending|accepted|rejected|superseded|no_change|all"
    ),
    paper_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[FigureProposalOut]:
    current = scoped_user(user)
    rows = await list_proposals(
        session,
        status="" if status == "all" else status,
        paper_id=paper_id,
        paper_scope=visible_paper_condition(current) if current is not None else None,
        limit=limit,
    )
    return [FigureProposalOut(**row) for row in rows]


@router.post("/proposals", response_model=FigureProposalOut, status_code=201)
async def create_external_proposal(
    body: ProposalCreateIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FigureProposalOut:
    """M5：外部 agent（MCP ``proposal.create``）投递一条图纠错提案。

    与内置 AI 走**同一套校验器**（``figure_review._validated``）：图号必须在候选集合内、
    图注清洗不能大改、裁框问题只记录；不满足就 422，不落库。

    额外的硬约束（比内置更严，因为外部 agent 不受服务端 prompt 约束）：

    * 必须有至少一条 ``evidence_block_ids``，且都属于这张图所在版本；
    * ``needs_change=false`` 的「无建议」直接拒绝——agent 应该把任务标 done，
      而不是造一条空提案浪费人审队列；
    * 只允许 ``figure_review``（框复核需要服务端做页内/IoU/caption 锚点四道校验，
      外部提案走不了那条链，见 docs/15 §12.19）。
    """
    try:
        fig, version, paper, siblings = await _load_figure(session, body.figure_id)
    except FigureReviewError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # 外部 agent 走 system 内部令牌（全库）；浏览器里的普通成员只能对自己的图投递，
    # 否则可以往别人待审队列里塞建议、覆盖掉 pending 条目。
    await ensure_figure_edit(session, user, fig.id)

    candidates = _candidate_labels(fig, siblings)
    proposed, notes = _validated(body.proposed, current=fig, candidates=candidates)
    if not proposed.get("needs_change"):
        raise HTTPException(
            status_code=422,
            detail=(
                "提案没有实际改动（needs_change=false）：agent 应把任务标记 done，"
                "不要造空提案。校验备注：" + "；".join(notes)
            ),
        )

    evidence_ids = [str(eid).strip() for eid in body.evidence_block_ids if str(eid).strip()]
    if not evidence_ids:
        raise HTTPException(
            status_code=422,
            detail="提案必须带证据 block_id（至少 1 条）：先 evidence.get 找到支撑块再投递",
        )
    blocks = {
        block.id: block
        for block in (
            await session.execute(select(Block).where(Block.id.in_(evidence_ids)))
        ).scalars()
    }
    missing = [eid for eid in evidence_ids if eid not in blocks]
    if missing:
        raise HTTPException(
            status_code=422, detail=f"证据 block 不存在：{', '.join(missing)}"
        )
    foreign = [eid for eid in evidence_ids if blocks[eid].paper_version_id != version.id]
    if foreign:
        raise HTTPException(
            status_code=422,
            detail=f"证据 block 不属于这张图所在的版本：{', '.join(foreign)}",
        )
    evidence = [
        {
            "block_id": blocks[eid].id,
            "page": blocks[eid].page,
            "type": blocks[eid].type,
            "text": (blocks[eid].text or "")[:300],
        }
        for eid in evidence_ids
    ]

    # 同一张图的旧 pending 建议作废（与内置 AI 一致：UI 只认最新一条）
    for row in (
        await session.execute(
            select(FigureProposal).where(
                FigureProposal.figure_id == fig.id, FigureProposal.status == "pending"
            )
        )
    ).scalars():
        row.status = "superseded"

    confidence = body.confidence or float(proposed.get("confidence") or 0.0)
    proposal = FigureProposal(
        figure_id=fig.id,
        paper_version_id=version.id,
        kind="figure_review",
        proposed_json=proposed,
        evidence_json=evidence,
        confidence=confidence,
        status="pending",
        provider="external",
        model=body.actor[:64],
        prompt_version="",
        mode="external",
    )
    session.add(proposal)
    await session.flush()
    write_audit(
        session,
        actor=body.actor,
        action="proposal.create",
        target_type="figure_proposal",
        target_id=proposal.id,
        after={
            "figure_id": fig.id,
            "kind": proposal.kind,
            "needs_change": bool(proposed.get("needs_change")),
            "confidence": confidence,
        },
        evidence=evidence_ids,
        note=body.note,
    )
    await session.commit()
    await session.refresh(proposal)
    return FigureProposalOut(**proposal_to_dict(proposal, fig=fig, paper=paper))


@router.get("/proposals/stats")
async def proposal_stats(
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """待审队列统计（顶栏徽标 + 待审页签用）。

    认证打开时只统计当前用户可见文献的 proposal：徽标数字不能把别人私有库的
    待审数量算进来。
    """
    current = scoped_user(user)
    scope = visible_paper_condition(current)

    status_stmt = (
        select(FigureProposal.status, func.count(FigureProposal.id))
        .select_from(FigureProposal)
        .join(Figure, FigureProposal.figure_id == Figure.id)
        .join(PaperVersion, FigureProposal.paper_version_id == PaperVersion.id)
        .join(Paper, PaperVersion.paper_id == Paper.id)
        .where(scope)
        .group_by(FigureProposal.status)
    )
    rows = (await session.execute(status_stmt)).all()
    by_status = {str(status): int(count) for status, count in rows}

    kind_stmt = (
        select(FigureProposal.kind, func.count(FigureProposal.id))
        .select_from(FigureProposal)
        .join(Figure, FigureProposal.figure_id == Figure.id)
        .join(PaperVersion, FigureProposal.paper_version_id == PaperVersion.id)
        .join(Paper, PaperVersion.paper_id == Paper.id)
        .where(scope)
        .group_by(FigureProposal.kind)
    )
    kind_rows = (await session.execute(kind_stmt)).all()
    return {
        "total": sum(by_status.values()),
        "pending": by_status.get("pending", 0),
        "accepted": by_status.get("accepted", 0),
        "rejected": by_status.get("rejected", 0),
        "rolled_back": by_status.get("rolled_back", 0),
        "by_status": by_status,
        "by_kind": {str(kind): int(count) for kind, count in kind_rows},
    }


@router.post("/proposals/auto-accept", response_model=AutoAcceptOut)
async def auto_accept_pending(
    body: AutoAcceptIn,
    _admin: AppUser | None = Depends(admin_when_enabled),
    session: AsyncSession = Depends(get_session),
) -> AutoAcceptOut:
    """扫一遍 pending 建议，把满足条件的自动接受（返回 accepted/skipped 明细）。

    这是**全库维护动作**：认证打开后仅管理员/system 可用（内置 worker 直接调函数，
    不走这个 HTTP 入口）；否则普通成员能触发别人文献的正式字段变更。
    """
    result = await sweep_auto_accept(session, limit=body.limit, paper_id=body.paper_id)
    return AutoAcceptOut(**result)


@router.post("/proposals/decide-batch", response_model=ProposalBatchOut)
async def decide_proposals_batch(
    body: ProposalBatchIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ProposalBatchOut:
    """批量采纳/拒绝（人审队列的「全选采纳」入口）。

    逐条校验图的写权限：看不见/只读共享的 proposal 进 failed，不整批失败。
    """
    out = ProposalBatchOut(requested=len(body.proposal_ids))
    for proposal_id in body.proposal_ids:
        try:
            current_proposal = await get_proposal(session, proposal_id)
            await ensure_figure_edit(session, user, current_proposal["figure_id"])
            _proposal, _applied = await decide_proposal(
                session,
                proposal_id,
                action=body.action,
                actor=body.actor,
                reason=body.reason,
            )
            if body.action == "accept":
                out.accepted += 1
            else:
                out.rejected += 1
        except FigureReviewError as exc:
            out.failed.append({"proposal_id": proposal_id, "error": str(exc)})
        except HTTPException as exc:
            out.failed.append({"proposal_id": proposal_id, "error": str(exc.detail)})
        except Exception as exc:  # noqa: BLE001 - 一条失败不拖垮整批
            await session.rollback()
            out.failed.append({"proposal_id": proposal_id, "error": f"{type(exc).__name__}: {exc}"})
    return out


@router.get("/proposals/{proposal_id}", response_model=FigureProposalOut)
async def get_proposal_detail(
    proposal_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FigureProposalOut:
    try:
        data = await get_proposal(session, proposal_id)
    except FigureReviewError as exc:
        raise _error(exc) from exc
    await ensure_figure_access(session, user, data["figure_id"])
    return FigureProposalOut(**data)


@router.post("/proposals/{proposal_id}/decide", response_model=ProposalDecideOut)
async def decide_figure_proposal(
    proposal_id: str,
    body: ProposalDecideIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ProposalDecideOut:
    """采纳或拒绝一条 AI 建议。

    采纳会立刻写正式字段（图号 / 图注 / 裁框错误标记）+ 快照 + 审计，并返回更新后的图；
    拒绝也留审计（被拒建议本身是模型可靠性的数据）。
    """
    try:
        current_proposal = await get_proposal(session, proposal_id)
    except FigureReviewError as exc:
        raise _error(exc) from exc
    await ensure_figure_edit(session, user, current_proposal["figure_id"])
    try:
        proposal, applied = await decide_proposal(
            session,
            proposal_id,
            action=body.action,
            actor=body.actor,
            reason=body.reason,
        )
    except FigureReviewError as exc:
        raise _error(exc) from exc

    figure_out: FigureOut | None = None
    if "bbox_override" in applied:
        await _rerender_after_bbox(session, proposal["figure_id"])
    row = await _figure_row(session, proposal["figure_id"])
    if row is not None:
        figure_out = figure_to_out(*row)
    return ProposalDecideOut(
        proposal=FigureProposalOut(**proposal),
        applied=applied,
        figure=figure_out,
    )


@router.post("/proposals/{proposal_id}/rollback", response_model=ProposalDecideOut)
async def rollback_figure_proposal(
    proposal_id: str,
    body: ProposalRollbackIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ProposalDecideOut:
    """回滚一条已采纳建议：按 before 快照恢复图号/图注/复核标记，并写审计。"""
    try:
        current_proposal = await get_proposal(session, proposal_id)
        await ensure_figure_edit(session, user, current_proposal["figure_id"])
        _proposal, _fig, _snapshot = await rollback_proposal(
            session, proposal_id, actor=body.actor
        )
        data = await get_proposal(session, proposal_id)
    except (ProposalError, FigureReviewError) as exc:
        raise _error(exc) from exc
    if data.get("kind") == "figure_bbox":
        await _rerender_after_bbox(session, data["figure_id"])
    figure_out: FigureOut | None = None
    row = await _figure_row(session, data["figure_id"])
    if row is not None:
        figure_out = figure_to_out(*row)
    return ProposalDecideOut(
        proposal=FigureProposalOut(**data),
        applied={"rolled_back": True},
        figure=figure_out,
    )


# ---------------------------------------------------------------------------
# agent 任务租约（M3）
# ---------------------------------------------------------------------------
@router.get("/tasks", response_model=list[AgentTaskOut])
async def list_agent_tasks(
    status: str | None = Query(None, description="queued|running|done|failed|canceled"),
    kind: str | None = Query(None, description="figure_review|auto_accept_sweep"),
    target_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _admin: AppUser | None = Depends(admin_when_enabled),
    session: AsyncSession = Depends(get_session),
) -> list[AgentTaskOut]:
    rows = await list_tasks(
        session,
        status=status,
        kind=kind,
        target_id=target_id,
        limit=limit,
        offset=offset,
    )
    return [AgentTaskOut(**row) for row in rows]


@router.post("/tasks", response_model=AgentTaskOut, status_code=201)
async def create_agent_task(
    body: TaskCreateIn,
    _admin: AppUser | None = Depends(admin_when_enabled),
    session: AsyncSession = Depends(get_session),
) -> AgentTaskOut:
    """排一个 agent 任务（幂等键命中时返回已有任务，不重复排）。"""
    if body.kind == "figure_review":
        _require_llm()
    payload = dict(body.payload or {})
    target_id = body.target_id
    if body.kind == "figure_review":
        target_id = target_id or str(payload.get("figure_id") or "")
        payload.setdefault("figure_id", target_id)
    try:
        task, _created = await create_task(
            session,
            kind=body.kind,
            target_type=body.target_type,
            target_id=target_id,
            payload=payload,
            priority=body.priority,
            max_attempts=body.max_attempts,
            idempotency_key=body.idempotency_key,
        )
        await session.commit()
        await session.refresh(task)
    except TaskError as exc:
        raise _error(exc) from exc
    return AgentTaskOut(**task_to_dict(task))


@router.post("/tasks/enqueue-figures", response_model=TaskEnqueueOut)
async def enqueue_figure_review_tasks(
    body: TaskEnqueueFiguresIn,
    _admin: AppUser | None = Depends(admin_when_enabled),
    session: AsyncSession = Depends(get_session),
) -> TaskEnqueueOut:
    """把一批「待确认图」排成 AI 复核任务（M3a 批量入口）。

    - ``figure_ids`` 给了就只排这些图；否则按 ``label_state`` 筛选（默认 unreviewed_all）；
    - 同一张图已有 queued/running 任务则跳过（不重复烧模型钱）。
    """
    _require_llm()
    stmt = select(Figure.id).where(Figure.deleted_at.is_(None))
    if body.figure_ids:
        stmt = stmt.where(Figure.id.in_(body.figure_ids))
    else:
        low_confidence = (
            Figure.label_norm.isnot(None),
            Figure.label_confidence < LOW_CONFIDENCE,
        )
        if body.label_state == "unlabeled":
            stmt = stmt.where(Figure.label_norm.is_(None))
        elif body.label_state == "low_confidence":
            stmt = stmt.where(*low_confidence)
        else:  # unreviewed_all
            stmt = stmt.where(or_(Figure.label_norm.is_(None), *low_confidence))
        stmt = stmt.where(Figure.label_source.is_distinct_from("review"))
        if body.paper_id:
            stmt = stmt.join(PaperVersion, Figure.paper_version_id == PaperVersion.id).where(
                PaperVersion.paper_id == body.paper_id
            )
    stmt = stmt.limit(body.limit)
    figure_ids = [row[0] for row in (await session.execute(stmt)).all()]

    active: set[str] = set()
    if figure_ids:
        active_rows = (
            await session.execute(
                select(AgentTask.target_id).where(
                    AgentTask.kind == "figure_review",
                    AgentTask.status.in_(("queued", "running")),
                    AgentTask.target_id.in_(figure_ids),
                )
            )
        ).all()
        active = {row[0] for row in active_rows}

    created: list[str] = []
    skipped = 0
    for figure_id in figure_ids:
        if figure_id in active:
            skipped += 1
            continue
        task, was_created = await create_task(
            session,
            kind="figure_review",
            target_type="figure",
            target_id=figure_id,
            payload={"figure_id": figure_id},
        )
        if was_created:
            created.append(task.id)
        else:  # pragma: no cover - 没有幂等键时不会命中
            skipped += 1
    await session.commit()
    return TaskEnqueueOut(created=len(created), skipped=skipped, task_ids=created)


@router.post("/tasks/enqueue-bbox-review", response_model=TaskEnqueueOut)
async def enqueue_bbox_review_tasks(
    body: TaskEnqueueBboxIn,
    _admin: AppUser | None = Depends(admin_when_enabled),
    session: AsyncSession = Depends(get_session),
) -> TaskEnqueueOut:
    """把一批图排成 M3c 框复核任务（多模态；默认只出建议，不自动改框）。"""
    _require_llm()
    stmt = select(Figure.id).where(
        Figure.deleted_at.is_(None), Figure.bbox_json.isnot(None)
    )
    if body.figure_ids:
        stmt = stmt.where(Figure.id.in_(body.figure_ids))
    elif body.paper_id:
        stmt = stmt.join(PaperVersion, Figure.paper_version_id == PaperVersion.id).where(
            PaperVersion.paper_id == body.paper_id
        )
    stmt = stmt.limit(body.limit)
    figure_ids = [row[0] for row in (await session.execute(stmt)).all()]

    active: set[str] = set()
    if figure_ids:
        active = {
            row[0]
            for row in (
                await session.execute(
                    select(AgentTask.target_id).where(
                        AgentTask.kind == "figure_bbox_review",
                        AgentTask.status.in_(("queued", "running")),
                        AgentTask.target_id.in_(figure_ids),
                    )
                )
            ).all()
        }
    created: list[str] = []
    for figure_id in figure_ids:
        if figure_id in active:
            continue
        task, was_created = await create_task(
            session,
            kind="figure_bbox_review",
            target_type="figure",
            target_id=figure_id,
            payload={"figure_id": figure_id},
        )
        if was_created:
            created.append(task.id)
    await session.commit()
    return TaskEnqueueOut(
        created=len(created), skipped=len(figure_ids) - len(created), task_ids=created
    )


@router.post("/tasks/recover-expired")
async def recover_agent_tasks(
    _admin: AppUser | None = Depends(admin_when_enabled),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """手工触发一次过期租约回收（worker 启动时也会自动跑）。"""
    count = await recover_expired_tasks(session)
    return {"recovered": count}


@router.get("/tasks/{task_id}", response_model=AgentTaskOut)
async def get_agent_task(
    task_id: str,
    _admin: AppUser | None = Depends(admin_when_enabled),
    session: AsyncSession = Depends(get_session),
) -> AgentTaskOut:
    try:
        task = await get_task(session, task_id)
    except TaskError as exc:
        raise _task_error(exc) from exc
    return AgentTaskOut(**task_to_dict(task))


@router.post("/tasks/{task_id}/claim", response_model=AgentTaskOut)
async def claim_agent_task(
    task_id: str,
    body: TaskClaimIn,
    _admin: AppUser | None = Depends(admin_when_enabled),
    session: AsyncSession = Depends(get_session),
) -> AgentTaskOut:
    """按 id 领取任务（外部 agent 用；内置 worker 走「领最早一条」的 claim_task）。"""
    try:
        task = await claim_task_by_id(
            session, task_id, actor=body.actor, lease_seconds=body.lease_seconds
        )
    except TaskError as exc:
        raise _task_error(exc) from exc
    return AgentTaskOut(**task_to_dict(task))


@router.post("/tasks/{task_id}/heartbeat", response_model=AgentTaskOut)
async def heartbeat_agent_task(
    task_id: str,
    body: TaskHeartbeatIn,
    _admin: AppUser | None = Depends(admin_when_enabled),
    session: AsyncSession = Depends(get_session),
) -> AgentTaskOut:
    try:
        task = await heartbeat_task(
            session, task_id, actor=body.actor, lease_seconds=body.lease_seconds
        )
    except TaskError as exc:
        raise _task_error(exc) from exc
    return AgentTaskOut(**task_to_dict(task))


@router.post("/tasks/{task_id}/complete", response_model=AgentTaskOut)
async def complete_agent_task(
    task_id: str,
    body: TaskCompleteIn,
    _admin: AppUser | None = Depends(admin_when_enabled),
    session: AsyncSession = Depends(get_session),
) -> AgentTaskOut:
    try:
        task = await complete_task(
            session, task_id, actor=body.actor, result=body.result, cost=body.cost
        )
    except TaskError as exc:
        raise _task_error(exc) from exc
    return AgentTaskOut(**task_to_dict(task))


@router.post("/tasks/{task_id}/fail", response_model=AgentTaskOut)
async def fail_agent_task(
    task_id: str,
    body: TaskFailIn,
    _admin: AppUser | None = Depends(admin_when_enabled),
    session: AsyncSession = Depends(get_session),
) -> AgentTaskOut:
    try:
        task = await fail_task(
            session, task_id, actor=body.actor, error=body.error, retry=body.retry
        )
    except TaskError as exc:
        raise _task_error(exc) from exc
    return AgentTaskOut(**task_to_dict(task))


@router.post("/tasks/{task_id}/retry", response_model=AgentTaskOut)
async def retry_agent_task(
    task_id: str,
    _admin: AppUser | None = Depends(admin_when_enabled),
    session: AsyncSession = Depends(get_session),
) -> AgentTaskOut:
    """人工重试 failed / canceled 任务（M3 收尾：任务面板的「重试」按钮）。"""
    try:
        task = await retry_task(session, task_id)
    except TaskError as exc:
        raise _task_error(exc) from exc
    return AgentTaskOut(**task_to_dict(task))


# ---------------------------------------------------------------------------
# 审计（M3）：所有正式字段写操作都能查
# ---------------------------------------------------------------------------
@router.get("/audit", response_model=list[AuditLogOut])
async def list_audit_log(
    target_type: str | None = Query(None, description="figure_proposal|figure|summary"),
    target_id: str | None = None,
    actor: str | None = None,
    action: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _admin: AppUser | None = Depends(admin_when_enabled),
    session: AsyncSession = Depends(get_session),
) -> list[AuditLogOut]:
    rows = await list_audit(
        session,
        target_type=target_type,
        target_id=target_id,
        actor=actor,
        action=action,
        limit=limit,
        offset=offset,
    )
    return [AuditLogOut(**row) for row in rows]
