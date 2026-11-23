# SPDX-License-Identifier: AGPL-3.0-or-later
"""文献上传与列表。

上传是流式的：边算 sha256 边落盘，不在内存里攒整个 PDF。
用 sha256 做去重——同一篇文献传两次不会产生两份版本。
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user, is_superuser, owner_id_for_write
from ..config import settings
from ..db import get_session
from ..labels import normalize_title
from ..models import (
    AppUser,
    Block,
    Collection,
    Figure,
    Job,
    Paper,
    PaperCollection,
    PaperVersion,
    TableBlock,
    UserPaperState,
)
from ..paths import resolve_within, to_storage_relative
from ..query_expand import expand_query, should_expand
from ..retrieval import Document, authors_text, rank_documents, weighted_tokens
from ..schemas import (
    FigureOut,
    PaperCreated,
    PaperOut,
    PaperPatchIn,
    PaperTextBlockOut,
    PaperTextOut,
    PaperTextSectionOut,
    TableLatexOut,
    TableOut,
)
from ..sections import build_sections
from ..table_latex import TableLatexError, table_html_to_latex
from ..team import (
    UNSET,
    can_access_paper,
    can_edit_paper,
    ensure_paper_access,
    ensure_paper_edit,
    ensure_paper_writable,
    get_workgroup,
    membership,
    paper_owner_names,
    paper_states,
    private_collection_condition,
    private_paper_condition,
    scoped_user,
    set_paper_state,
    shared_workgroup_map,
    workgroup_paper_condition,
)

router = APIRouter(prefix="/papers", tags=["papers"])

_CHUNK = 1 << 16


async def _latest_version(session: AsyncSession, paper_id: str) -> PaperVersion | None:
    """该文献的最新解析版本（按 created_at 倒序）。

    图的真值是版本级的：v1 和 v2 的图可能完全是两套编号/两套内容。
    「按文献看图表」与列表计数都只认最新版本；混着取会把两版数量相加，
    也会让前端看到 v1 的图和 v2 的图一起出现。
    """
    return (
        await session.execute(
            select(PaperVersion)
            .where(PaperVersion.paper_id == paper_id)
            .order_by(PaperVersion.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _stream_to_disk(file: UploadFile) -> tuple[str, Path, int]:
    """落盘并返回 (sha256, 最终路径, 字节数)。失败时清理临时文件。

    目标是内容寻址路径（``<sha>.pdf``）：**同名文件已存在且大小一致时直接复用**。
    这既省一次写盘，也绕开 Windows 上「目标 PDF 正被查看器/杀毒占用」导致
    ``os.replace`` 报 PermissionError（重复上传同一 PDF 时最容易遇到）。
    """
    settings.pdf_dir.mkdir(parents=True, exist_ok=True)
    tmp = settings.pdf_dir / f".upload-{uuid.uuid4().hex}.part"
    limit = settings.max_upload_mb * 1024 * 1024
    sha = hashlib.sha256()
    size = 0
    try:
        with tmp.open("wb") as fh:
            while chunk := await file.read(_CHUNK):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过 {settings.max_upload_mb} MB 上限",
                    )
                sha.update(chunk)
                fh.write(chunk)
        digest = sha.hexdigest()
        final = settings.pdf_dir / digest[:2] / f"{digest}.pdf"
        final.parent.mkdir(parents=True, exist_ok=True)
        # 内容寻址：同 sha 的文件内容必然相同，存在就不用再写一次。
        if final.is_file() and final.stat().st_size == size:
            return digest, final, size
        try:
            tmp.replace(final)
        except PermissionError as exc:
            # 目标存在但大小不一致（残留坏文件），又被别的进程占着 → 给可读错误，
            # 而不是把 WinError 原文甩给前端。
            raise HTTPException(
                status_code=409,
                detail="同名 PDF 已存在但无法覆盖（可能正被查看器占用）；"
                "请关闭正在查看该 PDF 的窗口后重试",
            ) from exc
        return digest, final, size
    finally:
        tmp.unlink(missing_ok=True)


@router.post("", response_model=PaperCreated, status_code=201)
async def upload_paper(
    file: UploadFile = File(...),
    title: str | None = Form(None),
    doi: str | None = Form(None),
    arxiv_id: str | None = Form(None),
    year: int | None = Form(None),
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> PaperCreated:
    if file.content_type not in (None, "application/pdf", "application/octet-stream"):
        raise HTTPException(status_code=415, detail="只接受 application/pdf")

    digest, path, _size = await _stream_to_disk(file)

    existing = (
        await session.execute(
            select(PaperVersion).where(PaperVersion.file_sha256 == digest)
        )
    ).scalar_one_or_none()

    if existing:
        # 同一 PDF 可能在别人的私有库里：不能把"已存在"的原文献信息交出去
        existing_paper = await session.get(Paper, existing.paper_id)
        if existing_paper is not None and not await can_access_paper(
            session, user, existing_paper
        ):
            raise HTTPException(
                status_code=409,
                detail="这份 PDF 已在其他用户的文献库中；需要使用时请让对方共享到工作组",
            )
        pending = (
            await session.execute(
                select(Job)
                .where(Job.paper_version_id == existing.id, Job.status.in_(("queued", "running")))
                .limit(1)
            )
        ).scalar_one_or_none()
        # 只有能改这篇文献的人才允许触发重解析：共享只读成员重复上传同一 PDF
        # 不能把 owner 的图/块/提案清掉重来（deduplicated 只返回已有信息）。
        editable = existing_paper is not None and await can_edit_paper(user, existing_paper)
        job = pending
        if pending is None and editable:
            job = Job(kind="parse", paper_version_id=existing.id, payload_json={})
            session.add(job)
            await session.commit()
        return PaperCreated(
            paper_id=existing.paper_id,
            version_id=existing.id,
            job_id=job.id if job is not None else "",
            sha256=digest,
            deduplicated=True,
            page_count=existing.page_count,
        )

    current = scoped_user(user)
    paper = Paper(
        title=title or Path(file.filename or "untitled").stem,
        doi=doi,
        arxiv_id=arxiv_id,
        year=year,
        status="new",
        owner_user_id=owner_id_for_write(current),
    )
    paper.title_norm = normalize_title(paper.title)
    session.add(paper)
    await session.flush()  # paper.id 要先落出来，后面两张表要引用
    # 文献级引用锚点：M4 的 MCP 工具/agent 回答要求「每条结论带 cite_key」。
    # 手工上传以前 paper.cite_key 是 null（只有图有 cite_key），引用只能退化到 paper_id；
    # 这里按 arXiv > DOI > 内部 id 生成稳定且唯一的 key（Zotero 导入仍用 Zotero item key）。
    paper.cite_key = await _make_cite_key(session, paper.id, arxiv_id=arxiv_id, doi=doi)

    version = PaperVersion(
        paper_id=paper.id,
        kind="uploaded",
        doi=doi,
        arxiv_id=arxiv_id,
        file_sha256=digest,
        # 相对 storage 根存：换挂载点（本地 .data → 容器 /data）不用改库
        file_path=to_storage_relative(settings.storage_dir, path),
    )
    session.add(version)
    await session.flush()  # 同上，job 要引用 version.id

    job = Job(kind="parse", paper_version_id=version.id, payload_json={})
    session.add(job)

    await session.commit()
    return PaperCreated(
        paper_id=paper.id,
        version_id=version.id,
        job_id=job.id,
        sha256=digest,
    )


async def _make_cite_key(
    session: AsyncSession, paper_id: str, *, arxiv_id: str | None, doi: str | None
) -> str:
    """给手工上传的文献生成唯一 cite_key。

    优先级：``arXiv:<id>`` > ``doi:<doi>`` > ``p-<paper_id 前 8 位>``。
    同一 arXiv/DOI 重复上传（不同 PDF 版本）会撞唯一约束，所以冲突时追加 paper_id
    前 4 位；cite_key 只用于引用展示/检索，不参与版本合并。
    """
    base = (
        f"arXiv:{arxiv_id}"
        if arxiv_id
        else (f"doi:{doi}" if doi else f"p-{paper_id[:8]}")
    )
    taken = (
        await session.execute(select(Paper.id).where(Paper.cite_key == base))
    ).scalar_one_or_none()
    return base if taken is None else f"{base}#{paper_id[:4]}"


async def _counts(
    session: AsyncSession, paper_ids: list[str]
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """按文献统计图数 / 表数 / 版本数。列表和详情共用一个实现，避免两个接口给出不同答案。

    图/表只统计**最新版本**：v1 与 v2 的图会同时存在（重解析不物理删旧版本），
    跨版本累加会让「2 张图」的文献显示成 4 张。
    """
    if not paper_ids:
        return {}, {}, {}
    ver_rows = (
        await session.execute(
            select(PaperVersion.paper_id, func.count(PaperVersion.id))
            .where(PaperVersion.paper_id.in_(paper_ids))
            .group_by(PaperVersion.paper_id)
        )
    ).all()

    version_rows = (
        await session.execute(
            select(PaperVersion.paper_id, PaperVersion.id)
            .where(PaperVersion.paper_id.in_(paper_ids))
            .order_by(PaperVersion.created_at.desc())
        )
    ).all()
    latest: dict[str, str] = {}
    for paper_id, version_id in version_rows:
        latest.setdefault(paper_id, version_id)

    fig_rows: list = []
    tab_rows: list = []
    if latest:
        latest_ids = list(latest.values())
        fig_rows = (
            await session.execute(
                select(PaperVersion.paper_id, func.count(Figure.id))
                .join(Figure, Figure.paper_version_id == PaperVersion.id)
                .where(
                    PaperVersion.id.in_(latest_ids),
                    Figure.deleted_at.is_(None),
                )
                .group_by(PaperVersion.paper_id)
            )
        ).all()
        tab_rows = (
            await session.execute(
                select(PaperVersion.paper_id, func.count(TableBlock.id))
                .join(TableBlock, TableBlock.paper_version_id == PaperVersion.id)
                .where(PaperVersion.id.in_(latest_ids))
                .group_by(PaperVersion.paper_id)
            )
        ).all()
    return dict(fig_rows), dict(tab_rows), dict(ver_rows)


async def _paper_collections(session: AsyncSession, paper_ids: list[str]) -> dict[str, list[str]]:
    """paper_id → 所属 collection id 列表（知识库操作台显示勾选状态用）。"""
    if not paper_ids:
        return {}
    rows = (
        await session.execute(
            select(PaperCollection.paper_id, PaperCollection.collection_id).where(
                PaperCollection.paper_id.in_(paper_ids)
            )
        )
    ).all()
    out: dict[str, list[str]] = {}
    for paper_id, collection_id in rows:
        out.setdefault(paper_id, []).append(collection_id)
    return out


def _paper_out(
    paper: Paper,
    figs: dict,
    tabs: dict,
    vers: dict,
    cols: dict[str, list[str]],
    *,
    user: AppUser | None = None,
    state: UserPaperState | None = None,
    owner_name: str | None = None,
    shared_ids: list[str] | None = None,
    can_edit: bool | None = None,
) -> PaperOut:
    """序列化文献。认证打开时，笔记/收藏/阅读状态走当前用户的私有状态：

    * 有 ``user_paper_state`` 行 → 用行里的值；
    * 否则 owner 自己的文献 / legacy（owner NULL）→ 回退到 paper 上的旧字段；
    * 别人共享给我的文献 → 不暴露 owner 的私有备注（None / 未收藏）。
    """
    current = scoped_user(user)
    if current is None:
        note, favorite, reading_status = (
            paper.note,
            bool(paper.favorite),
            paper.reading_status,
        )
    elif state is not None:
        note, favorite, reading_status = (
            state.note,
            bool(state.favorite),
            state.reading_status,
        )
    elif paper.owner_user_id in (None, current.id):
        note, favorite, reading_status = (
            paper.note,
            bool(paper.favorite),
            paper.reading_status,
        )
    else:
        note, favorite, reading_status = None, False, "unread"

    if can_edit is None:
        can_edit = (
            True
            if current is None
            else (is_superuser(current) or paper.owner_user_id == current.id)
        )

    return PaperOut(
        id=paper.id,
        title=paper.title,
        authors_json=paper.authors_json or [],
        doi=paper.doi,
        arxiv_id=paper.arxiv_id,
        cite_key=paper.cite_key,
        year=paper.year,
        venue=paper.venue,
        volume=paper.volume,
        issue=paper.issue,
        pages=paper.pages,
        entry_type=paper.entry_type or "journal",
        thesis_school=paper.thesis_school,
        thesis_degree=paper.thesis_degree,
        thesis_place=paper.thesis_place,
        status=paper.status,
        figure_count=figs.get(paper.id, 0),
        table_count=tabs.get(paper.id, 0),
        created_at=paper.created_at,
        abstract=paper.abstract,
        tags_json=paper.tags_json or [],
        reading_status=reading_status,
        metadata_source=paper.metadata_source,
        zotero_item_key=paper.zotero_item_key,
        has_version=bool(vers.get(paper.id, 0)),
        note=note,
        favorite=favorite,
        collection_ids=cols.get(paper.id, []),
        owner_user_id=paper.owner_user_id,
        owner_username=owner_name,
        can_edit=can_edit,
        shared_workgroup_ids=shared_ids or [],
    )


async def _paper_outs(
    session: AsyncSession, papers: list[Paper], user: AppUser | None
) -> list[PaperOut]:
    """列表/详情共用：一次把计数、目录、私有状态、归属人查齐（避免 N+1）。"""
    if not papers:
        return []
    ids = [paper.id for paper in papers]
    figs, tabs, vers = await _counts(session, ids)
    cols = await _paper_collections(session, ids)
    states = await paper_states(session, user, ids)
    owners = await paper_owner_names(session, papers)
    shared = await shared_workgroup_map(session, user, ids)
    return [
        _paper_out(
            paper,
            figs,
            tabs,
            vers,
            cols,
            user=user,
            state=states.get(paper.id),
            owner_name=owners.get(paper.owner_user_id or ""),
            shared_ids=shared.get(paper.id, []),
        )
        for paper in papers
    ]


PAPER_SEARCH_CANDIDATE_CAP = 2000
_PAPER_FIELD_WEIGHTS = {
    "title": 3.0,
    "cite_key": 3.0,
    "abstract": 1.5,
    "authors": 1.5,
    "venue": 1.0,
    "tags": 2.0,
}


def _paper_document(paper: Paper) -> Document:
    """文献检索文档：标题/cite_key 权重最高，摘要/作者/标签辅助召回。"""
    tags = " ".join(str(tag) for tag in (paper.tags_json or []) if str(tag).strip())
    fields = {
        "title": paper.title or "",
        "cite_key": paper.cite_key or "",
        "abstract": paper.abstract or "",
        "authors": authors_text(paper.authors_json),
        "venue": paper.venue or "",
        "tags": tags,
    }
    # 子串加分只对齐旧 q= 语义（标题 / cite_key 片段），避免作者、摘要的
    # 任意片段被当成精确命中；它们的词面召回由 BM25 负责。
    text = f"{paper.title or ''} {paper.cite_key or ''}".lower()
    return Document(
        id=paper.id,
        tokens=weighted_tokens(fields, _PAPER_FIELD_WEIGHTS),
        text=text,
    )


@router.get("", response_model=list[PaperOut])
async def list_papers(
    q: str | None = Query(
        None, description="BM25 检索：标题/摘要/作者/citekey/标签（保留旧子串命中）"
    ),
    collection_id: str | None = Query(None, description="知识库集合过滤（M1）"),
    favorite: bool | None = Query(None, description="true=只看收藏夹"),
    workgroup_id: str | None = Query(
        None, description="工作组视图：只看共享到这个组的文献（M7）"
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[PaperOut]:
    current = scoped_user(user)
    stmt = select(Paper)

    if workgroup_id:
        # 团队库：必须是组员（管理员可看所有组）；只出 paper_share 命中的文献
        if current is None:
            raise HTTPException(
                status_code=400,
                detail="工作组视图需要登录（当前是单机模式 AUTH_ENABLED=0）",
            )
        await get_workgroup(session, workgroup_id)
        if not is_superuser(current) and (
            await membership(session, workgroup_id, current.id)
        ) is None:
            raise HTTPException(status_code=404, detail="workgroup not found")
        stmt = stmt.where(workgroup_paper_condition(workgroup_id))
    elif current is not None:
        # 私有库：我的文献 + legacy（owner 为 NULL 的旧数据）
        stmt = stmt.where(private_paper_condition(current))

    if favorite is not None:
        if current is None:
            stmt = stmt.where(Paper.favorite.is_(favorite))
        else:
            # 认证打开后收藏是「每人一份」：user_paper_state + legacy paper.favorite
            favorited = select(UserPaperState.paper_id).where(
                UserPaperState.user_id == current.id,
                UserPaperState.favorite.is_(True),
            )
            legacy = and_(Paper.owner_user_id.is_(None), Paper.favorite.is_(True))
            if favorite:
                stmt = stmt.where(or_(Paper.id.in_(favorited), legacy))
            else:
                stmt = stmt.where(~Paper.id.in_(favorited), ~legacy)

    if collection_id:
        # 选父集合时把子集合的文献也带出来（知识库树是层级，点父级应看到整棵子树）
        all_cols_stmt = select(Collection.id, Collection.parent_id)
        if current is not None:
            all_cols_stmt = all_cols_stmt.where(private_collection_condition(current))
            collection = await session.get(Collection, collection_id)
            if collection is None or not await _collection_visible(session, user, collection):
                raise HTTPException(status_code=404, detail="collection not found")
        all_cols = (await session.execute(all_cols_stmt)).all()
        children: dict[str | None, list[str]] = {}
        for cid, pid in all_cols:
            children.setdefault(pid, []).append(cid)
        ids: list[str] = [collection_id]
        stack = [collection_id]
        while stack:
            cur = stack.pop()
            for child in children.get(cur, []):
                if child not in ids:
                    ids.append(child)
                    stack.append(child)
        # 去重放在「集合 → paper_id」子查询里，而不是 join 后对整行 DISTINCT：
        # Postgres 的 paper.authors_json/tags_json 是 JSON 类型（没有等值运算符），
        # SELECT DISTINCT paper.* 会直接报错。子查询去重同样消除重复卡片，
        # 也避免 join 的重复行影响 LIMIT/OFFSET 的偏移。
        stmt = stmt.where(
            Paper.id.in_(
                select(PaperCollection.paper_id)
                .where(PaperCollection.collection_id.in_(ids))
                .distinct()
            )
        )
    query_text = (q or "").strip()
    if query_text:
        # M9.1b-lite：中文查询先用已配置的 chat 模型改写成英文术语再 BM25；
        # 未配置/失败时静默回退原查询（不抛错、不阻塞）。
        if should_expand(query_text):
            expanded = await expand_query(query_text)
            if expanded is not None:
                query_text = expanded.text
        # 先按范围/集合/收藏过滤，再在候选集上做 BM25 排序；个人库规模下
        # 2000 篇的候选上限足够，避免 q= 把大库全部拉进内存。
        candidates = (
            await session.execute(
                stmt.order_by(Paper.created_at.desc(), Paper.id).limit(
                    PAPER_SEARCH_CANDIDATE_CAP
                )
            )
        ).scalars().all()
        ranked = rank_documents([_paper_document(paper) for paper in candidates], query_text)
        by_id = {paper.id: paper for paper in candidates}
        papers = [by_id[paper_id] for paper_id, _score in ranked][
            offset : offset + limit
        ]
    else:
        stmt = (
            stmt.order_by(Paper.created_at.desc(), Paper.id).limit(limit).offset(offset)
        )
        papers = (await session.execute(stmt)).scalars().all()
    return await _paper_outs(session, list(papers), user)


async def _collection_visible(session: AsyncSession, user: AppUser | None, collection) -> bool:
    current = scoped_user(user)
    if current is None:
        return True
    return is_superuser(current) or collection.owner_user_id in (None, current.id)


@router.get("/{paper_id}", response_model=PaperOut)
async def get_paper(
    paper_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> PaperOut:
    await ensure_paper_access(session, user, paper_id)
    paper = (await session.execute(select(Paper).where(Paper.id == paper_id))).scalar_one()
    return (await _paper_outs(session, [paper], user))[0]


@router.patch("/{paper_id}", response_model=PaperOut)
async def patch_paper(
    paper_id: str,
    body: PaperPatchIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> PaperOut:
    """工作台里的轻量编辑：备注 / 收藏 / 阅读状态；以及 references 用的著录字段。

    不碰 Zotero 镜像来的标题/作者/DOI（D1 只读）；volume/issue/pages 与
    entry_type/thesis_*（迁移 0015）是手动补全，只在「生成 references 条目 /
    学位论文骨架」时使用，Zotero 重新导入时上游有值会覆盖。

    认证打开后：
    * 笔记/收藏/阅读状态写入当前用户的 ``user_paper_state``（B 改备注不会影响 A）；
    * 著录字段是文献级字段，只有 owner / 管理员能改（共享文献对成员只读）。
    """
    paper = await ensure_paper_access(session, user, paper_id)
    current = scoped_user(user)
    biblio_fields = (
        "volume",
        "issue",
        "pages",
        "entry_type",
        "thesis_school",
        "thesis_degree",
        "thesis_place",
    )
    if body.reading_status is not None and body.reading_status not in (
        "unread",
        "reading",
        "done",
        "shelved",
    ):
        raise HTTPException(
            status_code=422,
            detail="reading_status 只支持 unread|reading|done|shelved",
        )
    if any(getattr(body, attr) is not None for attr in biblio_fields):
        await ensure_paper_writable(user, paper)

    if current is not None:
        await set_paper_state(
            session,
            user,
            paper.id,
            note=(body.note.strip() or None) if body.note is not None else UNSET,
            favorite=body.favorite if body.favorite is not None else UNSET,
            reading_status=body.reading_status if body.reading_status is not None else UNSET,
        )
        for attr in biblio_fields:
            value = getattr(body, attr)
            if value is not None:
                setattr(paper, attr, value.strip() or None)
    else:
        if body.note is not None:
            paper.note = body.note.strip() or None
        if body.favorite is not None:
            paper.favorite = bool(body.favorite)
        if body.reading_status is not None:
            paper.reading_status = body.reading_status
        for attr in biblio_fields:
            value = getattr(body, attr)
            if value is not None:
                setattr(paper, attr, value.strip() or None)

    await session.commit()
    await session.refresh(paper)
    return (await _paper_outs(session, [paper], user))[0]


@router.get("/{paper_id}/text", response_model=PaperTextOut)
async def get_paper_text(
    paper_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> PaperTextOut:
    """正文速览：把最新版本的正文块按章节组织成可读流（只读，不落库）。

    前端用它做「文献正文速览」：按节标题分段、每段可点击跳回原文页/bbox。
    """
    await ensure_paper_access(session, user, paper_id)
    version = await _latest_version(session, paper_id)
    if version is None:
        raise HTTPException(status_code=404, detail="这篇文献还没有解析版本，先上传并解析 PDF")
    blocks = list(
        (
            await session.execute(
                select(Block)
                .where(Block.paper_version_id == version.id)
                .order_by(Block.page, Block.order_idx)
            )
        ).scalars()
    )
    figure_pages = [
        int(page)
        for page in (
            await session.execute(
                select(Figure.page).where(
                    Figure.paper_version_id == version.id, Figure.deleted_at.is_(None)
                )
            )
        ).scalars()
    ]
    sections = build_sections(
        blocks,
        page_count=version.page_count,
        figure_pages=figure_pages,
        parser=version.parser,
    )
    block_by_id = {b.id: b for b in blocks}
    out_sections: list[PaperTextSectionOut] = []
    for section in sections:
        section_blocks = [
            block_by_id[bid] for bid in section.block_ids if bid in block_by_id
        ]
        out_sections.append(
            PaperTextSectionOut(
                key=section.key,
                title=section.title,
                level=section.level,
                kind=section.kind,
                page_start=section.page_start,
                page_end=section.page_end,
                blocks=[
                    PaperTextBlockOut(
                        block_id=b.id,
                        page=b.page,
                        type=b.type,
                        text=(b.text or "").strip(),
                        bbox=b.bbox_json or [],
                    )
                    for b in section_blocks
                    if (b.text or "").strip()
                ],
            )
        )
    return PaperTextOut(
        paper_id=paper_id,
        version_id=version.id,
        page_count=version.page_count,
        block_count=sum(len(s.blocks) for s in out_sections),
        sections=out_sections,
    )


@router.post("/{paper_id}/pdf", response_model=PaperCreated, status_code=201)
async def attach_paper_pdf(
    paper_id: str,
    file: UploadFile = File(..., description="给这篇文献补一个 PDF 文件"),
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> PaperCreated:
    """给已有文献补传 PDF（Zotero 只读导入的条目默认只有元数据）。

    会新建一个 paper_version 并排队解析；同一文件重复上传按 sha256 去重。
    如果这个 PDF 已经属于另一篇文献，直接 409（不替用户合并文献）。
    """
    paper = await ensure_paper_edit(session, user, paper_id)
    if file.content_type not in (None, "application/pdf", "application/octet-stream"):
        raise HTTPException(status_code=415, detail="只接受 application/pdf")

    digest, path, _size = await _stream_to_disk(file)
    existing = (
        await session.execute(select(PaperVersion).where(PaperVersion.file_sha256 == digest))
    ).scalar_one_or_none()
    if existing is not None:
        if existing.paper_id != paper_id:
            other = (
                await session.execute(select(Paper).where(Paper.id == existing.paper_id))
            ).scalar_one_or_none()
            other_title = other.title if other else existing.paper_id
            # 别人的私有文献不回显标题
            if other is not None and not await can_access_paper(session, user, other):
                raise HTTPException(
                    status_code=409,
                    detail="这个 PDF 已经属于其他用户库里的文献；需要时请让对方共享",
                )
            raise HTTPException(
                status_code=409,
                detail=f"这个 PDF 已经属于文献《{other_title}》，请在那篇里使用，或换一个文件",
            )
        pending = (
            await session.execute(
                select(Job)
                .where(Job.paper_version_id == existing.id, Job.status.in_(("queued", "running")))
                .limit(1)
            )
        ).scalar_one_or_none()
        job = pending or Job(kind="parse", paper_version_id=existing.id, payload_json={})
        if pending is None:
            session.add(job)
            await session.commit()
        return PaperCreated(
            paper_id=paper_id,
            version_id=existing.id,
            job_id=job.id,
            sha256=digest,
            deduplicated=True,
            page_count=existing.page_count,
        )

    version = PaperVersion(
        paper_id=paper_id,
        kind="uploaded",
        file_sha256=digest,
        file_path=to_storage_relative(settings.storage_dir, path),
    )
    session.add(version)
    await session.flush()
    job = Job(kind="parse", paper_version_id=version.id, payload_json={})
    session.add(job)
    paper.status = "new"
    await session.commit()
    return PaperCreated(
        paper_id=paper_id,
        version_id=version.id,
        job_id=job.id,
        sha256=digest,
    )


@router.get("/{paper_id}/figures", response_model=list[FigureOut])
async def list_paper_figures(
    paper_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[FigureOut]:
    """按文献列图 —— 「按文献」那一轴的数据源（只看最新版本）。"""
    from .figures import _to_out  # 复用序列化，避免两处字段不一致

    await ensure_paper_access(session, user, paper_id)
    version = await _latest_version(session, paper_id)
    if version is None:
        return []
    rows = (
        await session.execute(
            select(Figure, PaperVersion, Paper)
            .join(PaperVersion, Figure.paper_version_id == PaperVersion.id)
            .join(Paper, PaperVersion.paper_id == Paper.id)
            .where(Figure.paper_version_id == version.id, Figure.deleted_at.is_(None))
            .order_by(Figure.page, Figure.label_num)
        )
    ).all()
    return [_to_out(fig, ver, paper_) for fig, ver, paper_ in rows]


@router.get("/{paper_id}/tables", response_model=list[TableOut])
async def list_paper_tables(
    paper_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[TableOut]:
    """按文献列表格（只看最新版本）。

    轻量档（pdfium）抽不到表结构，所以这里的结果通常来自按需升级后的 Docling；
    没有就是空数组，不要把它当成错误。
    """
    await ensure_paper_access(session, user, paper_id)
    version = await _latest_version(session, paper_id)
    if version is None:
        return []
    rows = (
        await session.execute(
            select(TableBlock, PaperVersion, Paper)
            .join(PaperVersion, TableBlock.paper_version_id == PaperVersion.id)
            .join(Paper, PaperVersion.paper_id == Paper.id)
            .where(TableBlock.paper_version_id == version.id)
            .order_by(TableBlock.page)
        )
    ).all()
    return [
        TableOut(
            table_id=tab.id,
            paper_id=paper_.id,
            version_id=ver.id,
            page=tab.page,
            label_raw=tab.label_raw,
            label_norm=tab.label_norm,
            caption=tab.caption or "",
            quality=tab.quality,
            html=tab.html,
        )
        for tab, ver, paper_ in rows
    ]


@router.get("/{paper_id}/tables/{table_id}/latex", response_model=TableLatexOut)
async def get_table_latex(
    paper_id: str,
    table_id: str,
    width: Literal["auto", "plain"] = Query(
        "auto", description="auto=宽表自动按页宽换行；plain=全部普通 l 列（旧行为）"
    ),
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TableLatexOut:
    """把一张结构化表转成 LaTeX tabular / 浮动体（M9.3a / M9.3c，只读派生）。

    表格必须属于该文献，且是当前库里真实存在的行；没有结构化 HTML（图片表）
    或 HTML 里没有可识别行列时返回 422，不硬造一张空表。宽表默认用
    ``p{…\\linewidth}`` 列自动换行；需要旧版全 ``l`` 列时传 ``width=plain``。
    """
    await ensure_paper_access(session, user, paper_id)
    table = await session.get(TableBlock, table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="table not found")
    version = await session.get(PaperVersion, table.paper_version_id)
    if version is None or version.paper_id != paper_id:
        raise HTTPException(status_code=404, detail="table not found")
    try:
        payload = table_html_to_latex(
            table.html,
            caption=table.caption or "",
            label_raw=table.label_raw,
            label_norm=table.label_norm,
            table_id=table.id,
            width=width,
        )
    except TableLatexError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return TableLatexOut(
        table_id=table.id,
        paper_id=paper_id,
        caption=payload.caption,
        label=payload.label,
        label_key=payload.label_key,
        tabular=payload.tabular,
        code=payload.code,
        columns=payload.columns,
        rows=payload.rows,
        warnings=payload.warnings,
        wrapped=payload.wrapped,
        column_spec=payload.column_spec,
    )


@router.get("/{paper_id}/pdf")
async def get_paper_pdf(
    paper_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """把 PDF 本体交给前端 —— PDF.js 要在这里画 bbox overlay。

    路径同样过 ``resolve_within``：库里的 ``file_path`` 不能直接拿来读文件。
    """
    await ensure_paper_access(session, user, paper_id)
    version = await _latest_version(session, paper_id)
    if version is None:
        raise HTTPException(status_code=404, detail="paper has no pdf")

    resolved = resolve_within(settings.storage_dir, version.file_path)
    return FileResponse(
        resolved,
        media_type="application/pdf",
        headers={"Content-Disposition": "inline", "Cache-Control": "private, max-age=3600"},
    )
