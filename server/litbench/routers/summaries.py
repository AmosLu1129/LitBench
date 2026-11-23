# SPDX-License-Identifier: AGPL-3.0-or-later
"""章节摘要 API（M2a）。

链路：``GET /papers/{id}/sections``（章节树）→ ``POST /papers/{id}/summaries/generate``
（服务端跑 prompt + provider + 证据校验）→ ``GET /summaries/{id}``（正文 + 证据块）
→ ``PATCH /summaries/{id}``（人工编辑，锁住不被覆盖）→ ``POST /summaries/{id}/publish``
→ revisions 列表/回滚。

M2a 没有 key：provider 固定 mock；M2b 只换 provider，接口与 UI 不变。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..db import get_session
from ..models import AppUser
from ..paper_summary import (
    PAPER_SECTION_KEY,
    PaperSummaryError,
    generate_paper_summary,
    get_paper_summary,
)
from ..schemas import (
    EvidenceBlockOut,
    PaperSummaryGenerateIn,
    PaperSummaryGenerateOut,
    SectionOut,
    SectionsOut,
    SummaryBriefOut,
    SummaryDetailOut,
    SummaryEditIn,
    SummaryGenerateIn,
    SummaryGenerateOut,
    SummaryOut,
    SummaryRevisionOut,
)
from ..summaries import (
    SummaryError,
    edit_summary,
    evidence_blocks,
    generate_summaries,
    get_summary,
    list_revisions,
    list_summaries,
    paper_sections,
    publish_summary,
    restore_revision,
    revision_to_dict,
    summaries_by_section,
    summary_to_dict,
)
from ..team import (
    ensure_paper_access,
    ensure_paper_edit,
    ensure_summary_access,
    ensure_summary_edit,
    scoped_user,
)

router = APIRouter(tags=["summaries"])


def _actor(user: AppUser | None, fallback: str | None = None) -> str:
    """审计 actor：认证打开时强制当前用户，单机模式才用 body 里的兼容字段。"""
    current = scoped_user(user)
    if current is not None:
        return current.username
    return ((fallback or "human").strip()[:64]) or "human"


def _error(exc: SummaryError) -> HTTPException:
    return HTTPException(
        status_code=404 if "not found" in str(exc) else 422, detail=str(exc)
    )


@router.get("/papers/{paper_id}/sections", response_model=SectionsOut)
async def get_paper_sections(
    paper_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SectionsOut:
    """章节树 + 每节已有的摘要状态（M2a：标题来自 block 启发式，见 sections.py）。"""
    await ensure_paper_access(session, user, paper_id)
    try:
        version, sections = await paper_sections(session, paper_id)
        existing = await summaries_by_section(session, version.id)
    except SummaryError as exc:
        raise _error(exc) from exc
    out: list[SectionOut] = []
    for section in sections:
        summary = existing.get(section.key)
        brief = None
        if summary is not None:
            brief = SummaryBriefOut(
                summary_id=summary.id,
                status=summary.status,
                source=summary.source,
                auto_accepted=summary.auto_accepted,
                human_locked=summary.human_locked,
                content_chars=len(summary.content or ""),
            )
        out.append(
            SectionOut(
                key=section.key,
                title=section.title,
                level=section.level,
                kind=section.kind,
                page_start=section.page_start,
                page_end=section.page_end,
                block_count=len(section.block_ids),
                char_count=section.char_count,
                figure_count=section.figure_count,
                summary=brief,
            )
        )
    return SectionsOut(
        paper_id=paper_id,
        version_id=version.id,
        page_count=version.page_count,
        sections=out,
    )


@router.get("/papers/{paper_id}/summaries", response_model=list[SummaryOut])
async def list_paper_summaries(
    paper_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[SummaryOut]:
    await ensure_paper_access(session, user, paper_id)
    try:
        rows = await list_summaries(session, paper_id)
    except SummaryError as exc:
        raise _error(exc) from exc
    # 整篇速览走 /papers/{id}/summary，不混进「章节摘要」列表
    rows = [s for s in rows if s.section_key != PAPER_SECTION_KEY]
    return [SummaryOut(**summary_to_dict(s)) for s in rows]


@router.get("/papers/{paper_id}/summary", response_model=SummaryDetailOut)
async def get_paper_overview(
    paper_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SummaryDetailOut:
    """整篇速览（AI 一键总结文献）：没生成过返回 404，前端据此显示「一键总结」按钮。"""
    await ensure_paper_access(session, user, paper_id)
    try:
        summary = await get_paper_summary(session, paper_id)
        if summary is None:
            raise HTTPException(status_code=404, detail="这篇文献还没有整篇速览")
        blocks = await evidence_blocks(session, summary)
    except PaperSummaryError as exc:
        raise _error(exc) from exc
    return SummaryDetailOut(
        **summary_to_dict(summary),
        evidence=[EvidenceBlockOut(**b) for b in blocks],
    )


@router.post("/papers/{paper_id}/summary", response_model=PaperSummaryGenerateOut)
async def generate_paper_overview(
    paper_id: str,
    body: PaperSummaryGenerateIn | None = None,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> PaperSummaryGenerateOut:
    """一键总结整篇文献：一次模型调用 → 证据校验 → 落库（status=created/updated）。"""
    body = body or PaperSummaryGenerateIn()
    await ensure_paper_edit(session, user, paper_id)
    try:
        stats = await generate_paper_summary(session, paper_id, force=body.force)
    except PaperSummaryError as exc:
        raise _error(exc) from exc
    return PaperSummaryGenerateOut(**stats)


@router.post("/papers/{paper_id}/summaries/generate", response_model=SummaryGenerateOut)
async def generate_paper_summaries(
    paper_id: str,
    body: SummaryGenerateIn | None = None,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SummaryGenerateOut:
    """生成章节摘要（M2a：mock provider，不花钱、不联网）。"""
    body = body or SummaryGenerateIn()
    await ensure_paper_edit(session, user, paper_id)
    try:
        stats = await generate_summaries(
            session,
            paper_id,
            section_keys=body.section_keys,
            force=body.force,
        )
    except SummaryError as exc:
        raise _error(exc) from exc
    return SummaryGenerateOut(**stats)


@router.get("/summaries/{summary_id}", response_model=SummaryDetailOut)
async def get_summary_detail(
    summary_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SummaryDetailOut:
    try:
        summary = await get_summary(session, summary_id)
        await ensure_summary_access(session, user, summary)
        blocks = await evidence_blocks(session, summary)
    except SummaryError as exc:
        raise _error(exc) from exc
    data = summary_to_dict(summary)
    return SummaryDetailOut(
        **data,
        evidence=[EvidenceBlockOut(**b) for b in blocks],
    )


@router.patch("/summaries/{summary_id}", response_model=SummaryOut)
async def edit_summary_content(
    summary_id: str,
    body: SummaryEditIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SummaryOut:
    """人工编辑摘要：source→human_edited、human_locked=true，自动生成不再覆盖。"""
    try:
        summary = await get_summary(session, summary_id)
        await ensure_summary_edit(session, user, summary)
        summary = await edit_summary(
            session,
            summary_id,
            content=body.content,
            key_points=body.key_points,
            actor=_actor(user, body.actor),
        )
    except SummaryError as exc:
        raise _error(exc) from exc
    return SummaryOut(**summary_to_dict(summary))


@router.post("/summaries/{summary_id}/publish", response_model=SummaryOut)
async def publish_summary_content(
    summary_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SummaryOut:
    try:
        summary = await get_summary(session, summary_id)
        await ensure_summary_edit(session, user, summary)
        summary = await publish_summary(session, summary_id, actor=_actor(user))
    except SummaryError as exc:
        raise _error(exc) from exc
    return SummaryOut(**summary_to_dict(summary))


@router.get("/summaries/{summary_id}/revisions", response_model=list[SummaryRevisionOut])
async def get_summary_revisions(
    summary_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[SummaryRevisionOut]:
    try:
        summary = await get_summary(session, summary_id)
        await ensure_summary_access(session, user, summary)
        rows = await list_revisions(session, summary_id)
    except SummaryError as exc:
        raise _error(exc) from exc
    return [SummaryRevisionOut(**revision_to_dict(r)) for r in rows]


@router.post(
    "/summaries/{summary_id}/revisions/{revision_id}/restore", response_model=SummaryOut
)
async def restore_summary_revision(
    summary_id: str,
    revision_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SummaryOut:
    try:
        summary = await get_summary(session, summary_id)
        await ensure_summary_edit(session, user, summary)
        summary = await restore_revision(
            session, summary_id, revision_id, actor=_actor(user)
        )
    except SummaryError as exc:
        raise _error(exc) from exc
    return SummaryOut(**summary_to_dict(summary))
