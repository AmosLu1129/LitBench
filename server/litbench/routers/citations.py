# SPDX-License-Identifier: AGPL-3.0-or-later
"""引用核验 API（M9.4）：给 MCP ``cite.resolve(review=true)`` 用的模型复核档。

只暴露一个写形状的只读端点：不落库、不生成引用文本，只判断「给定证据能否直接支持
claim」。模型配置沿用 ``llm.py``（UI 设置 > 环境变量）；未配置时 503 并带
``code=llm_not_configured``，调用方应回退到词面档。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..citation_review import (
    CitationReviewError,
    load_review_blocks,
    review_citation,
)
from ..db import get_session
from ..models import AppUser
from ..schemas import CitationReviewIn, CitationReviewOut
from ..team import ensure_paper_access

router = APIRouter(prefix="/citations", tags=["citations"])


@router.post("/review", response_model=CitationReviewOut)
async def review_citation_endpoint(
    payload: CitationReviewIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> CitationReviewOut:
    """用当前配置的模型复核一条 claim 是否被指定证据块支持。"""
    await ensure_paper_access(session, user, payload.paper_id)
    blocks = await load_review_blocks(
        session, paper_id=payload.paper_id, block_ids=list(payload.block_ids)
    )
    if not blocks:
        raise HTTPException(
            status_code=422,
            detail="没有可用的证据块（block_ids 不属于该文献，或正文为空）",
        )
    try:
        result = await review_citation(payload.claim_text, blocks)
    except CitationReviewError as exc:
        status = 503 if exc.code == "llm_not_configured" else 502
        raise HTTPException(
            status_code=status, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    return CitationReviewOut(
        verdict=result.verdict,
        confidence=result.confidence,
        reason=result.reason,
        provider=result.provider,
        model=result.model,
        block_ids=result.block_ids,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        latency_ms=result.latency_ms,
        prompt_version=result.prompt_version,
    )
