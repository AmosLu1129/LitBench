# SPDX-License-Identifier: AGPL-3.0-or-later
"""写作侧 API（M5）：claim 卡片 / 对比矩阵 / LaTeX·BibTeX 导出 / 引用校验。

四个入口对应写作台的三块：

* ``/claims``：一句论断 + 证据，CRUD 落库；
* ``/matrix``：选 1–8 篇文献，实时派生对比矩阵（不落库）；
* ``/writing/export``：把 claim 导出成 LaTeX（带 refs.bib）/ BibTeX / Markdown；
* ``/writing/check-citations``：检查 ``\\cite{}`` 里的键能否在文献库解析。

错误语义与其它路由一致：找不到 → 404，参数/证据不合法 → 422，不吞错。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user, owner_id_for_write
from ..db import get_session
from ..doc_export import (
    DocExportError,
    DocExportFailed,
    DocExportUnavailable,
    export_capabilities,
    export_document,
)
from ..models import AppUser, Paper
from ..outline import build_outline
from ..schemas import (
    CitationCheckIn,
    CitationCheckOut,
    ClaimCreateIn,
    ClaimOut,
    ClaimRevisionOut,
    ClaimUpdateIn,
    DocExportCapabilitiesOut,
    MatrixDimensionsOut,
    MatrixOut,
    OutlineIn,
    OutlineOut,
    WritingDocExportIn,
    WritingExportIn,
    WritingExportOut,
)
from ..team import (
    can_edit_claim,
    ensure_claim_access,
    ensure_paper_access,
    scoped_user,
)
from ..thesis import ThesisError
from ..writing import (
    ClaimError,
    build_matrix,
    check_citations,
    claim_to_dict,
    create_claim,
    delete_claim,
    export_writing,
    get_claim,
    list_claim_revisions,
    list_claims,
    list_matrix_dimensions,
    restore_claim_revision,
    revision_to_dict,
    update_claim,
)

router = APIRouter(tags=["writing"])


def _error(exc: ClaimError) -> HTTPException:
    message = str(exc)
    return HTTPException(
        status_code=404 if "not found" in message else 422, detail=message
    )


@router.get("/claims", response_model=list[ClaimOut])
async def list_writing_claims(
    paper_id: str | None = None,
    section: str | None = None,
    status: str | None = Query(None, description="draft|approved"),
    q: str | None = None,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ClaimOut]:
    try:
        rows = await list_claims(
            session,
            paper_id=paper_id,
            section=section,
            status=status,
            q=q,
            user=user,
            limit=limit,
            offset=offset,
        )
    except ClaimError as exc:
        raise _error(exc) from exc
    return [ClaimOut(**row) for row in rows]


@router.post("/claims", response_model=ClaimOut, status_code=201)
async def create_writing_claim(
    body: ClaimCreateIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ClaimOut:
    # 共享文献可以写自己的卡片；但看不见的文献不能挂 claim
    await ensure_paper_access(session, user, body.paper_id)
    current = scoped_user(user)
    try:
        row = await create_claim(
            session,
            paper_id=body.paper_id,
            text=body.text,
            section=body.section,
            evidence=[item.model_dump() for item in body.evidence],
            tags=body.tags,
            status=body.status,
            source=body.source,
            created_by=body.source,
            owner_user_id=owner_id_for_write(current),
            order_idx=body.order_idx,
        )
    except ClaimError as exc:
        raise _error(exc) from exc
    return ClaimOut(**row)


@router.get("/claims/{claim_id}", response_model=ClaimOut)
async def get_writing_claim(
    claim_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ClaimOut:
    try:
        claim = await get_claim(session, claim_id)
    except ClaimError as exc:
        raise _error(exc) from exc
    await ensure_claim_access(session, user, claim)
    paper = await session.get(Paper, claim.paper_id)
    return ClaimOut(**claim_to_dict(claim, paper))


@router.patch("/claims/{claim_id}", response_model=ClaimOut)
async def update_writing_claim(
    claim_id: str,
    body: ClaimUpdateIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ClaimOut:
    try:
        existing = await get_claim(session, claim_id)
    except ClaimError as exc:
        raise _error(exc) from exc
    await ensure_claim_access(session, user, existing)
    if not can_edit_claim(user, existing):
        raise HTTPException(status_code=403, detail="只有卡片 owner 或管理员能修改")
    current = scoped_user(user)
    try:
        row = await update_claim(
            session,
            claim_id,
            text=body.text,
            section=body.section,
            evidence=[item.model_dump() for item in body.evidence]
            if body.evidence is not None
            else None,
            tags=body.tags,
            status=body.status,
            order_idx=body.order_idx,
            edited_by=(current.username if current is not None else "single"),
        )
    except ClaimError as exc:
        raise _error(exc) from exc
    return ClaimOut(**row)


@router.delete("/claims/{claim_id}", status_code=204)
async def delete_writing_claim(
    claim_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        existing = await get_claim(session, claim_id)
    except ClaimError as exc:
        raise _error(exc) from exc
    await ensure_claim_access(session, user, existing)
    if not can_edit_claim(user, existing):
        raise HTTPException(status_code=403, detail="只有卡片 owner 或管理员能删除")
    try:
        await delete_claim(session, claim_id)
    except ClaimError as exc:
        raise _error(exc) from exc
    return Response(status_code=204)


@router.get("/claims/{claim_id}/revisions", response_model=list[ClaimRevisionOut])
async def list_writing_claim_revisions(
    claim_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ClaimRevisionOut]:
    """claim 的历史版本（新→旧）；每次真正改内容前留一份。"""
    try:
        claim = await get_claim(session, claim_id)
    except ClaimError as exc:
        raise _error(exc) from exc
    await ensure_claim_access(session, user, claim)
    rows = await list_claim_revisions(session, claim_id)
    return [ClaimRevisionOut(**revision_to_dict(row)) for row in rows]


@router.post(
    "/claims/{claim_id}/revisions/{revision_id}/restore", response_model=ClaimOut
)
async def restore_writing_claim_revision(
    claim_id: str,
    revision_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ClaimOut:
    """把 claim 恢复到某个历史版本；恢复前会先把当前状态压一份。"""
    try:
        existing = await get_claim(session, claim_id)
    except ClaimError as exc:
        raise _error(exc) from exc
    await ensure_claim_access(session, user, existing)
    if not can_edit_claim(user, existing):
        raise HTTPException(status_code=403, detail="只有卡片 owner 或管理员能修改")
    current = scoped_user(user)
    try:
        row = await restore_claim_revision(
            session,
            claim_id,
            revision_id,
            edited_by=(current.username if current is not None else "single"),
        )
    except ClaimError as exc:
        raise _error(exc) from exc
    return ClaimOut(**row)


@router.get("/matrix/dimensions", response_model=MatrixDimensionsOut)
async def get_matrix_dimensions() -> MatrixDimensionsOut:
    """矩阵可选维度清单（默认 7 行 + cite/type/tags 可选）。"""
    return MatrixDimensionsOut(
        dimensions=list_matrix_dimensions(),
        note="维度由前端勾选、后端按顺序派生；不落库，随时可重算。",
    )


@router.get("/matrix", response_model=MatrixOut)
async def get_matrix(
    paper_ids: str = Query(..., description="逗号分隔的 paper_id，1–8 篇"),
    dimensions: str | None = Query(
        None, description="逗号分隔的维度 key（GET /matrix/dimensions）；不传=默认 7 行"
    ),
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> MatrixOut:
    ids = [value.strip() for value in paper_ids.split(",") if value.strip()]
    dim_list = (
        [part.strip() for part in dimensions.split(",") if part.strip()]
        if dimensions
        else None
    )
    current = scoped_user(user)
    if current is not None:
        # 认证打开后：看不见的文献不能进对比矩阵（缺失 id 仍交给 build_matrix 报 422）
        for paper_id in ids:
            await ensure_paper_access(session, user, paper_id)
    try:
        payload = await build_matrix(session, ids, dimensions=dim_list, user=user)
    except ClaimError as exc:
        raise _error(exc) from exc
    return MatrixOut(**payload)


@router.post("/writing/export", response_model=WritingExportOut)
async def export_writing_bundle(
    body: WritingExportIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> WritingExportOut:
    current = scoped_user(user)
    if current is not None:
        for paper_id in body.paper_ids or []:
            await ensure_paper_access(session, user, paper_id)
    try:
        payload = await export_writing(
            session,
            fmt=body.format,
            paper_ids=body.paper_ids,
            claim_ids=body.claim_ids,
            title=body.title,
            section_order=body.section_order,
            include_drafts=body.include_drafts,
            with_evidence_comments=body.with_evidence_comments,
            owner_user_id=current.id if current is not None else None,
            user=user,
        )
    except ClaimError as exc:
        raise _error(exc) from exc
    return WritingExportOut(**payload)


@router.get("/writing/export/capabilities", response_model=DocExportCapabilitiesOut)
async def get_document_export_capabilities() -> DocExportCapabilitiesOut:
    """Word/PDF 导出在当前环境是否可用（缺 pandoc/typst 时前端置灰并提示）。"""
    return DocExportCapabilitiesOut(**export_capabilities())


@router.post("/writing/export/document")
async def export_writing_document(
    body: WritingDocExportIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """claim 草稿 → Word (.docx) / PDF（pandoc --citeproc；PDF 用 typst 引擎）。"""
    current = scoped_user(user)
    if current is not None:
        for paper_id in body.paper_ids or []:
            await ensure_paper_access(session, user, paper_id)
    try:
        payload = await export_document(
            session,
            fmt=body.format,
            paper_ids=body.paper_ids,
            claim_ids=body.claim_ids,
            title=body.title,
            section_order=body.section_order,
            include_drafts=body.include_drafts,
            csl_style=body.csl_style,
            owner_user_id=current.id if current is not None else None,
            user=user,
        )
    except DocExportUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except DocExportFailed as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except DocExportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # 中文文件名用 RFC 5987；ASCII fallback 给老客户端
    disposition = (
        'attachment; filename="litbench-draft.'
        + body.format
        + '"; filename*=UTF-8\'\''
        + quote(payload["filename"])
    )
    return Response(
        content=payload["content"],
        media_type=payload["media_type"],
        headers={
            "Content-Disposition": disposition,
            "X-LitBench-Claims": str(payload["claim_count"]),
            "X-LitBench-Papers": str(payload["paper_count"]),
        },
    )


@router.post("/writing/outline", response_model=OutlineOut)
async def get_writing_outline(
    body: OutlineIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> OutlineOut:
    """claim → 章节大纲（section → claim 排序）+ 图表/引用交叉检查（只读）。"""
    # 显式点名的 paper/claim 先过可见性，语义与 /writing/export、/thesis/export 一致：
    # 看不见统一 404，而不是过滤后报「没有可整理的 claim」的 422。
    for paper_id in body.paper_ids or []:
        await ensure_paper_access(session, user, paper_id)
    for claim_id in body.claim_ids or []:
        try:
            claim = await get_claim(session, claim_id)
        except ClaimError as exc:
            raise _error(exc) from exc
        await ensure_claim_access(session, user, claim)
    try:
        payload = await build_outline(
            session,
            claim_ids=body.claim_ids,
            paper_ids=body.paper_ids,
            include_drafts=body.include_drafts,
            section_order=body.section_order,
            user=user,
        )
    except ClaimError as exc:
        raise _error(exc) from exc
    except ThesisError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return OutlineOut(**payload)


@router.post("/writing/check-citations", response_model=CitationCheckOut)
async def check_writing_citations(
    body: CitationCheckIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> CitationCheckOut:
    payload = await check_citations(session, body.text, user=user)
    return CitationCheckOut(**payload)
