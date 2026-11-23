# SPDX-License-Identifier: AGPL-3.0-or-later
"""学位论文 LaTeX 包 API（M8，2026-11-15）。

两个端点（docs/17 §11.3.3）：

* ``POST /thesis/export``：把 claim 卡片 + 文献元数据导出成 ctexbook 骨架、
  ``refs.bib``、``references_gbt.tex``（只读派生，不落库）；
* ``POST /thesis/check``：对导出的 .tex（或用户改过的稿子）做 ``\\cite{}`` /
  ``\\ref{fig:*}`` / ``\\ref{tab:*}`` 的确定性交叉检查。

错误语义与写作侧一致：找不到 → 404，参数/内容不合法 → 422。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..db import get_session
from ..models import AppUser
from ..schemas import (
    ThesisCheckIn,
    ThesisCheckOut,
    ThesisExportIn,
    ThesisExportOut,
)
from ..team import ensure_paper_access
from ..thesis import ThesisError, check_thesis, export_thesis

router = APIRouter(tags=["thesis"])


def _error(exc: ThesisError) -> HTTPException:
    message = str(exc)
    return HTTPException(
        status_code=404 if "not found" in message else 422, detail=message
    )


@router.post("/thesis/export", response_model=ThesisExportOut)
async def export_thesis_bundle(
    body: ThesisExportIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ThesisExportOut:
    """导出学位论文骨架（.tex + refs.bib + references_gbt.tex）。"""
    # 只导自己能看见的文献；看不见统一 404（与写作导出一致）
    for paper_id in body.paper_ids or []:
        await ensure_paper_access(session, user, paper_id)
    try:
        payload = await export_thesis(
            session,
            title=body.title,
            author=body.author,
            school=body.school,
            degree=body.degree,
            advisor=body.advisor,
            year=body.year,
            paper_ids=body.paper_ids,
            claim_ids=body.claim_ids,
            chapter_order=body.chapter_order,
            include_drafts=body.include_drafts,
            user=user,
        )
    except ThesisError as exc:
        raise _error(exc) from exc
    return ThesisExportOut(**payload)


@router.post("/thesis/check", response_model=ThesisCheckOut)
async def check_thesis_bundle(
    body: ThesisCheckIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ThesisCheckOut:
    """引用/图表标签交叉检查（词面确定性检查，不调模型）。"""
    for paper_id in body.paper_ids or []:
        await ensure_paper_access(session, user, paper_id)
    payload = await check_thesis(
        session, text=body.text, paper_ids=body.paper_ids, user=user
    )
    return ThesisCheckOut(**payload)
