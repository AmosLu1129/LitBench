# SPDX-License-Identifier: AGPL-3.0-or-later
"""知识库 / 文献树 / Zotero 只读导入（M1，见 docs/15）。

设计要点（D1）：Zotero 继续当文献库主人，这边只做**只读镜像**：
集合按 ``zotero_key``、条目按 ``zotero_item_key`` 幂等 upsert；LitBench 的标注/摘要/
增删图不回写 Zotero。

接口：

* ``GET/POST /api/collections``、``PATCH/DELETE /api/collections/{id}``
* ``POST/DELETE /api/collections/{id}/papers``（手动归档）
* ``POST /api/import/zotero``、``GET /api/import/runs``
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import admin_when_enabled, current_user, is_superuser, owner_id_for_write
from ..db import get_session
from ..models import AppUser, Collection, ImportRun, Paper, PaperCollection
from ..schemas import (
    CollectionIn,
    CollectionOut,
    CollectionPaperIn,
    CollectionPatch,
    ImportIn,
    ImportRunOut,
)
from ..team import (
    can_access_collection,
    can_edit_collection,
    ensure_paper_access,
    private_collection_condition,
    private_paper_condition,
    scoped_user,
)
from ..zotero import ZoteroImportError, import_zotero

router = APIRouter(tags=["knowledge"])


def _out(col: Collection, count: int = 0) -> CollectionOut:
    return CollectionOut(
        id=col.id,
        name=col.name,
        parent_id=col.parent_id,
        sort_order=col.sort_order,
        source=col.source,
        zotero_key=col.zotero_key,
        paper_count=count,
    )


@router.get("/collections", response_model=list[CollectionOut])
async def list_collections(
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[CollectionOut]:
    """整棵目录树（平铺返回，前端按 parent_id 组装）。

    认证打开后只返回自己的目录（+ legacy），文献计数也只算自己库里的文献。
    """
    current = scoped_user(user)
    stmt = select(Collection)
    if current is not None:
        stmt = stmt.where(private_collection_condition(current))
    cols = (
        await session.execute(stmt.order_by(Collection.sort_order, Collection.name))
    ).scalars().all()

    count_stmt = select(PaperCollection.collection_id, func.count()).group_by(
        PaperCollection.collection_id
    )
    if current is not None:
        count_stmt = (
            count_stmt.join(Paper, PaperCollection.paper_id == Paper.id)
            .where(private_paper_condition(current))
            .group_by(PaperCollection.collection_id)
        )
    counts = dict((await session.execute(count_stmt)).all())
    return [_out(c, int(counts.get(c.id, 0))) for c in cols]


async def _get_collection(
    session: AsyncSession, collection_id: str, user: AppUser | None = None
) -> Collection:
    col = await session.get(Collection, collection_id)
    if col is None or not await can_access_collection(session, user, col):
        raise HTTPException(status_code=404, detail="collection not found")
    return col


async def _is_descendant(session: AsyncSession, maybe_child: str, ancestor: str) -> bool:
    """防止把父节点挂到自己的子孙下面（会丢一棵子树）。"""
    seen: set[str] = set()
    cur: str | None = maybe_child
    while cur and cur not in seen:
        if cur == ancestor:
            return True
        seen.add(cur)
        col = await session.get(Collection, cur)
        cur = col.parent_id if col else None
    return False


@router.post("/collections", response_model=CollectionOut, status_code=201)
async def create_collection(
    body: CollectionIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> CollectionOut:
    if body.parent_id:
        await _get_collection(session, body.parent_id, user)
    current = scoped_user(user)
    col = Collection(
        name=body.name.strip(),
        parent_id=body.parent_id,
        source="manual",
        owner_user_id=owner_id_for_write(current),
    )
    session.add(col)
    await session.commit()
    await session.refresh(col)
    return _out(col, 0)


@router.patch("/collections/{collection_id}", response_model=CollectionOut)
async def update_collection(
    collection_id: str,
    body: CollectionPatch,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> CollectionOut:
    col = await _get_collection(session, collection_id, user)
    if not await can_edit_collection(user, col):
        raise HTTPException(status_code=403, detail="只有目录 owner 或管理员能修改")
    if body.name is not None:
        col.name = body.name.strip()
    if "parent_id" in body.model_fields_set:
        if body.parent_id == collection_id:
            raise HTTPException(status_code=422, detail="不能把集合挂到自己下面")
        if body.parent_id:
            await _get_collection(session, body.parent_id, user)
            if await _is_descendant(session, body.parent_id, collection_id):
                raise HTTPException(status_code=422, detail="不能把集合挂到自己的子集合下面")
        col.parent_id = body.parent_id
    await session.commit()
    await session.refresh(col)
    count = (
        await session.execute(
            select(func.count()).select_from(PaperCollection).where(
                PaperCollection.collection_id == col.id
            )
        )
    ).scalar() or 0
    return _out(col, int(count))


@router.delete("/collections/{collection_id}")
async def delete_collection(
    collection_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """删集合：子集合上提一层，文献关联删除；文献本身不动。"""
    col = await _get_collection(session, collection_id, user)
    if not await can_edit_collection(user, col):
        raise HTTPException(status_code=403, detail="只有目录 owner 或管理员能删除")
    for child in (
        await session.execute(select(Collection).where(Collection.parent_id == col.id))
    ).scalars():
        child.parent_id = col.parent_id
    for link in (
        await session.execute(
            select(PaperCollection).where(PaperCollection.collection_id == col.id)
        )
    ).scalars():
        await session.delete(link)
    await session.delete(col)
    await session.commit()
    return {"collection_id": collection_id, "deleted": True}


@router.post("/collections/{collection_id}/papers")
async def add_paper_to_collection(
    collection_id: str,
    body: CollectionPaperIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    col = await _get_collection(session, collection_id, user)
    if not await can_edit_collection(user, col):
        raise HTTPException(status_code=403, detail="只有目录 owner 或管理员能修改")
    await ensure_paper_access(session, user, body.paper_id)
    exists = (
        await session.execute(
            select(PaperCollection).where(
                PaperCollection.collection_id == collection_id,
                PaperCollection.paper_id == body.paper_id,
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        session.add(PaperCollection(paper_id=body.paper_id, collection_id=collection_id))
        await session.commit()
    return {"collection_id": collection_id, "paper_id": body.paper_id, "added": True}


@router.delete("/collections/{collection_id}/papers/{paper_id}")
async def remove_paper_from_collection(
    collection_id: str,
    paper_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    col = await _get_collection(session, collection_id, user)
    if not await can_edit_collection(user, col):
        raise HTTPException(status_code=403, detail="只有目录 owner 或管理员能修改")
    link = (
        await session.execute(
            select(PaperCollection).where(
                PaperCollection.collection_id == collection_id,
                PaperCollection.paper_id == paper_id,
            )
        )
    ).scalar_one_or_none()
    if link is not None:
        await session.delete(link)
        await session.commit()
    return {"collection_id": collection_id, "paper_id": paper_id, "removed": True}


def _run_out(run: ImportRun) -> ImportRunOut:
    return ImportRunOut(
        id=run.id,
        source=run.source,
        started_at=run.started_at,
        finished_at=run.finished_at,
        stats=run.stats_json or {},
        error=run.error,
    )


@router.post("/import/zotero", response_model=ImportRunOut)
async def import_from_zotero(
    body: ImportIn | None = None,
    user: AppUser | None = Depends(current_user),
    _admin: AppUser | None = Depends(admin_when_enabled),
    session: AsyncSession = Depends(get_session),
) -> ImportRunOut:
    """从服务端本机 Zotero（sqlite / local API / JSON 路径）导入。

    认证打开后仅管理员/system 可调：``path``/``api_base`` 由请求体控制，
    普通成员可借此让服务器读任意本地文件或请求任意 URL（SSRF）；
    成员需要导入自己的库时用上传 JSON 的 ``/import/zotero/file``。
    """
    body = body or ImportIn()
    current = scoped_user(user)
    try:
        stats = await import_zotero(
            session,
            source=body.source,
            path=body.path,
            api_base=body.api_base,
            owner_user_id=owner_id_for_write(current),
            is_superuser=is_superuser(current),
        )
    except ZoteroImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — 导入失败要给出可读原因，不吞
        raise HTTPException(status_code=500, detail=f"Zotero 导入失败：{exc}") from exc

    run = (
        await session.execute(select(ImportRun).order_by(ImportRun.started_at.desc()).limit(1))
    ).scalars().first()
    if run is None:  # 理论上不会；保底返回本次 stats
        return ImportRunOut(
            id="",
            source=str(stats.get("source", body.source)),
            stats=stats,
        )
    return _run_out(run)


@router.post("/import/zotero/file", response_model=ImportRunOut)
async def import_zotero_json_file(
    file: UploadFile = File(
        ..., description="Zotero 导出的 JSON（local API 形状或 {collections,items}）"
    ),
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ImportRunOut:
    """上传 JSON 导入（不用本机装 Zotero，直接从 Zotero「导出库」拿文件）。"""
    current = scoped_user(user)
    fd, tmp_name = tempfile.mkstemp(suffix=".json")
    tmp = Path(tmp_name)
    try:
        # 边落盘边计数：超限立刻 413，不把整个上传先缓冲进内存（F10）
        total = 0
        with os.fdopen(fd, "wb") as fh:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > 50 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="JSON 太大（上限 50MB）")
                fh.write(chunk)
        try:
            await import_zotero(
                session,
                source="json",
                path=str(tmp),
                owner_user_id=owner_id_for_write(current),
                is_superuser=is_superuser(current),
            )
        except ZoteroImportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        run = (
            await session.execute(
                select(ImportRun).order_by(ImportRun.started_at.desc()).limit(1)
            )
        ).scalars().first()
        if run is None:
            return ImportRunOut(id="", source="json", stats={})
        return _run_out(run)
    finally:
        tmp.unlink(missing_ok=True)


@router.get("/import/runs", response_model=list[ImportRunOut])
async def list_import_runs(
    limit: int = Query(20, ge=1, le=100),
    _admin: AppUser | None = Depends(admin_when_enabled),
    session: AsyncSession = Depends(get_session),
) -> list[ImportRunOut]:
    """导入历史是全局审计面（import_run 无 owner 字段），认证打开后仅管理员可看。"""
    runs = (
        await session.execute(select(ImportRun).order_by(ImportRun.started_at.desc()).limit(limit))
    ).scalars().all()
    return [_run_out(r) for r in runs]
