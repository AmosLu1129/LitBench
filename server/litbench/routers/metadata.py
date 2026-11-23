# SPDX-License-Identifier: AGPL-3.0-or-later
"""元数据补全 API（M8，2026-11-16）。

两个端点：

* ``POST /papers/{id}/metadata/lookup``：按 DOI / arXiv id / 题名搜索查 Crossref 或
  arXiv，返回字段 diff 或候选列表；**只读**，不落库；
* ``POST /papers/{id}/metadata/apply``：把用户确认的字段写进 paper（默认只填空白，
  覆盖已有值要 ``overwrite=true``）；需要 owner / 管理员权限。

外部服务失败语义：不存在 → 404，网络/接口失败 → 502，参数/不支持的字段 → 422，
覆盖冲突 → 409，功能关闭 → 503。不写 Zotero。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..config import settings
from ..db import get_session
from ..metadata import (
    MetadataConflict,
    MetadataError,
    MetadataNotFound,
    MetadataUnavailable,
    apply_metadata,
    lookup_for_paper,
)
from ..models import AppUser
from ..schemas import (
    MetadataApplyIn,
    MetadataLookupIn,
    MetadataLookupOut,
    PaperOut,
)
from ..team import ensure_paper_access, ensure_paper_writable
from .papers import _paper_outs

router = APIRouter(tags=["metadata"])


@router.post("/papers/{paper_id}/metadata/lookup", response_model=MetadataLookupOut)
async def lookup_paper_metadata(
    paper_id: str,
    body: MetadataLookupIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> MetadataLookupOut:
    """查 Crossref / arXiv，返回当前值 → 建议值的 diff（只读，不落库）。"""
    if not settings.metadata_lookup_enabled:
        raise HTTPException(
            status_code=503, detail="元数据补全已被配置关闭（METADATA_LOOKUP_ENABLED=0）"
        )
    paper = await ensure_paper_access(session, user, paper_id)
    try:
        payload = await lookup_for_paper(
            paper,
            mode=body.mode,
            doi=body.doi,
            arxiv_id=body.arxiv_id,
            query=body.query,
        )
    except MetadataNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MetadataUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except MetadataError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return MetadataLookupOut(**payload)


@router.post("/papers/{paper_id}/metadata/apply", response_model=PaperOut)
async def apply_paper_metadata(
    paper_id: str,
    body: MetadataApplyIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> PaperOut:
    """把确认过的字段写进 paper；默认只填空白，覆盖已有值要 overwrite=true。"""
    paper = await ensure_paper_access(session, user, paper_id)
    await ensure_paper_writable(user, paper)
    try:
        apply_metadata(paper, body.fields, overwrite=body.overwrite)
    except MetadataConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MetadataError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await session.commit()
    await session.refresh(paper)
    return (await _paper_outs(session, [paper], user))[0]
