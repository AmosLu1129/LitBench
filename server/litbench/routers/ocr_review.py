# SPDX-License-Identifier: AGPL-3.0-or-later
"""OCR 与大模型校对不一致的页级提案（轻量 OCR 工作流，2026-11-17）。

* ``GET /papers/{id}/ocr-proposals``：阅读页拉当前待决定项；
* ``POST /ocr-proposals/{id}/decide``：接受 OCR / 接受模型修订 / 拒绝。

OCR 结果本身已经写成 Block（用户不操作也能先读）；只有模型校对发现出入时才出现提案。
接受模型修订会替换该页的 OCR 文字块；接受 OCR / 拒绝都不改正文。
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..db import get_session
from ..models import AppUser, Block, OcrProposal, Paper, PaperVersion
from ..schemas import OcrProposalDecideIn, OcrProposalOut
from ..team import ensure_paper_access, ensure_paper_edit, scoped_user
from ..writing import latest_version

router = APIRouter(tags=["ocr-review"])


def _out(row: OcrProposal) -> OcrProposalOut:
    return OcrProposalOut(
        proposal_id=row.id,
        paper_version_id=row.paper_version_id,
        page=row.page,
        ocr_text=row.ocr_text or "",
        model_text=row.model_text or "",
        differences=list(row.diff_json or []),
        status=row.status,
        provider=row.provider or "",
        model=row.model or "",
        tokens_in=row.tokens_in or 0,
        tokens_out=row.tokens_out or 0,
        cost=row.cost or 0.0,
        latency_ms=row.latency_ms or 0,
        created_at=row.created_at,
        decided_at=row.decided_at,
        decided_by=row.decided_by,
    )


@router.get("/papers/{paper_id}/ocr-proposals", response_model=list[OcrProposalOut])
async def list_ocr_proposals(
    paper_id: str,
    status: str | None = Query(
        None, description="pending|accepted_ocr|accepted_model|rejected|all"
    ),
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[OcrProposalOut]:
    await ensure_paper_access(session, user, paper_id)
    version = await latest_version(session, paper_id)
    if version is None:
        return []
    stmt = (
        select(OcrProposal)
        .where(OcrProposal.paper_version_id == version.id)
        .order_by(OcrProposal.created_at.asc())
    )
    if status and status != "all":
        stmt = stmt.where(OcrProposal.status == status)
    rows = (await session.execute(stmt)).scalars().all()
    return [_out(row) for row in rows]


async def _apply_model_blocks(
    session: AsyncSession, proposal: OcrProposal
) -> None:
    """接受模型修订：替换该页 OCR 文字块（保留该页图/表）。"""
    text = (proposal.model_text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="模型修订为空，不能应用")
    blocks = (
        await session.execute(
            select(Block).where(
                Block.paper_version_id == proposal.paper_version_id,
                Block.page == proposal.page,
                Block.engine == "liteocr",
            )
        )
    ).scalars().all()
    bbox = [0.0, 0.0, 0.0, 0.0]
    order_idx = 0
    if blocks:
        order_idx = min(int(row.order_idx or 0) for row in blocks)
        xs0 = [float((row.bbox_json or [0, 0, 0, 0])[0]) for row in blocks]
        ys0 = [float((row.bbox_json or [0, 0, 0, 0])[1]) for row in blocks]
        xs1 = [float((row.bbox_json or [0, 0, 0, 0])[2]) for row in blocks]
        ys1 = [float((row.bbox_json or [0, 0, 0, 0])[3]) for row in blocks]
        bbox = [min(xs0), min(ys0), max(xs1), max(ys1)]
    await session.execute(
        delete(Block).where(
            Block.paper_version_id == proposal.paper_version_id,
            Block.page == proposal.page,
            Block.engine == "liteocr",
        )
    )
    session.add(
        Block(
            paper_version_id=proposal.paper_version_id,
            page=proposal.page,
            bbox_json=bbox,
            type="text",
            text=text,
            order_idx=order_idx,
            engine="llm_cross_check",
        )
    )


@router.post("/ocr-proposals/{proposal_id}/decide", response_model=OcrProposalOut)
async def decide_ocr_proposal(
    proposal_id: str,
    body: OcrProposalDecideIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> OcrProposalOut:
    proposal = await session.get(OcrProposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="OCR 提案不存在")
    version = await session.get(PaperVersion, proposal.paper_version_id)
    paper = await session.get(Paper, version.paper_id) if version is not None else None
    if version is None or paper is None:
        raise HTTPException(status_code=404, detail="OCR 提案对应的文献版本不存在")
    await ensure_paper_access(session, user, paper.id)
    await ensure_paper_edit(session, user, paper.id)
    if proposal.status != "pending":
        raise HTTPException(status_code=409, detail=f"该提案已处理（{proposal.status}）")

    if body.action == "accept_model":
        await _apply_model_blocks(session, proposal)
        proposal.status = "accepted_model"
    elif body.action == "accept_ocr":
        proposal.status = "accepted_ocr"
    else:
        proposal.status = "rejected"
    current = scoped_user(user)
    proposal.decided_by = (current.username if current is not None else "human")[:64]
    proposal.decided_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(proposal)
    return _out(proposal)
