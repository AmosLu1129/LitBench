# SPDX-License-Identifier: AGPL-3.0-or-later
"""图表检索与详情。

三条接口的分工：

* ``GET /api/figures``            —— 检索（出处 / 语义 / 浏览）
* ``GET /api/figures/{id}``       —— 单图详情
* ``GET /api/figures/{id}/image`` —— 图本身（前端 <img> 直接引）

图片接口做了路径约束：只允许读 ``settings.image_dir`` 下的文件。
这里的 ``image_path`` 来自解析器写库，理论上可信，但**凡是把库里的路径
直接变成文件读取的接口，都要再校验一次**——库可以被导入、被迁移、被人工改。
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters.render import crop_to_png, render_page
from ..audit import write_audit
from ..auth import current_user, is_superuser
from ..auto_accept import snapshot_figure
from ..config import settings
from ..db import get_session
from ..labeling import LOW_CONFIDENCE
from ..labels import LABEL_RE, normalize_label, parse_query, split_label
from ..models import AppUser, Collection, Figure, Paper, PaperVersion
from ..paths import resolve_data_path, resolve_within_subdir, to_storage_relative
from ..query_expand import expand_query, should_expand
from ..schemas import FigureCreateIn, FigureOut, FigureSearchOut, GroupIn, LabelIn, ReviewIn
from ..search import (
    FIGURE_TYPES,
    LABEL_STATES,
    classify,
    normalize_label_filter,
    search_figures,
)
from ..team import (
    ensure_figure_access,
    ensure_figure_edit,
    ensure_paper_access,
    ensure_paper_edit,
    get_workgroup,
    membership,
    private_paper_condition,
    scoped_user,
    workgroup_paper_condition,
)

router = APIRouter(prefix="/figures", tags=["figures"])

log = logging.getLogger("litbench.figures")

_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}

_INTERNAL_ID = re.compile(r"^[0-9a-fA-F]{32}$")


async def _resolve_paper_ids(session: AsyncSession, paper: str) -> list[str] | None:
    """把 ``paper`` 参数解析成 paper id 列表；解析不出来返回 None。

    以前这里把 ``paper`` 原样拼进 ``q`` 走文本检索：32 位内部 id 和裸
    ``cite_key`` 都会被语义检索当成任意字符串，结果恒为 0 条。现在：

    * 32 位 hex → 直接当 ``Paper.id``（内部 id）；
    * 否则按 ``cite_key`` / ``doi`` / ``arxiv_id`` 精确匹配（一次查询，可能多篇）；
    * 都不中返回 None，由调用方把原文并回 ``q``（保留标题片段语义）。
    """
    text = (paper or "").strip()
    if not text:
        return None
    if _INTERNAL_ID.fullmatch(text):
        return [text.lower()]
    rows = (
        await session.execute(
            select(Paper.id).where(
                or_(
                    Paper.cite_key == text,
                    Paper.cite_key == text.lower(),
                    Paper.doi == text,
                    Paper.arxiv_id == text,
                )
            )
        )
    ).all()
    ids = [row[0] for row in rows]
    return ids or None


def _render_crop_to_storage(
    bbox: list[float], page_no: int, cite_key: str, file_path: str
) -> str | None:
    """按给定框从 PDF 某页裁一块图写到 storage，返回新的 image_path（相对 storage 根）。

    两种用途共用：纠错时按修正框重裁缩略图；用户手动「框选加图」时生成图片。
    为什么放这里而不是排一个后台 job：一次只渲染一页 + 裁一块，几十毫秒；
    用户修正完回到卡片却看到旧缩略图，是比慢更糟的体验。
    失败也只是缩略图没更新，不能连带让纠错/加图请求失败（调用方按需处理 None）。

    只传值不传 ORM 对象：这个函数跑在线程池里，不该碰 session 里的实例。
    """
    if len(bbox) != 4:
        return None
    try:
        pdf_path = resolve_data_path(settings.storage_dir, file_path)
    except (PermissionError, ValueError):
        return None
    if not pdf_path.is_file():
        return None

    try:
        import pypdfium2 as pdfium
    except ImportError:  # pragma: no cover — 主镜像必装
        return None

    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        if page_no < 1 or page_no > len(doc):
            return None
        page = doc[page_no - 1]
        try:
            image = render_page(page, 150)
            if image is None:
                return None
            data = crop_to_png(image, [float(v) for v in bbox], 150)
            if not data:
                return None
        finally:
            page.close()
    finally:
        doc.close()

    target = settings.image_dir / f"{cite_key}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return to_storage_relative(settings.storage_dir, target)


def _to_out(fig: Figure, ver: PaperVersion, paper: Paper) -> FigureOut:
    # 有修正框就用修正框：overlay 立刻变对
    effective = fig.bbox_override_json or fig.bbox_json or []
    image_url = None
    if fig.image_path:
        image_url = f"/api/figures/{fig.id}/image"
        # 复核过（可能重裁过缩略图）就带一个版本参数：图片响应有 24h 缓存，
        # 不带的话用户修完框回到卡片看到的还是旧图。
        if fig.reviewed_at is not None:
            image_url = f"{image_url}?v={int(fig.reviewed_at.timestamp())}"
    return FigureOut(
        figure_id=fig.id,
        cite_key=fig.cite_key,
        paper_id=paper.id,
        paper_title=paper.title,
        year=paper.year,
        version_id=ver.id,
        version_kind=ver.kind,
        page=fig.page,
        label_raw=fig.label_raw,
        label_norm=fig.label_norm,
        sublabel=fig.sublabel,
        caption=fig.caption,
        figure_type=fig.figure_type,
        license_hint=fig.license_hint,
        bbox=effective,
        image_url=image_url,
        label_source=fig.label_source,
        label_confidence=fig.label_confidence,
        group_key=fig.group_key,
        origin=fig.origin,
        review_status=fig.review_status,
        review_note=fig.review_note,
        bbox_override=fig.bbox_override_json,
    )


@router.get("", response_model=FigureSearchOut)
async def list_figures(
    q: str | None = Query(None, description="自由查询：标题 / 作者+年份 / DOI / Fig. 3"),
    paper: str | None = Query(None, description="文献 id / DOI / arXiv / citekey / 标题片段"),
    label: str | None = Query(None, description="图号，如 'Fig. 3'、'图3'、'fig:3'"),
    figure_type: str | None = Query(
        None, description="chart|micrograph|photo|map|diagram|structure|other"
    ),
    review: Literal["pending", "ok", "wrong"] | None = Query(
        None, description="pending|ok|wrong —— 只看某类纠错状态"
    ),
    label_state: str | None = Query(
        None,
        description="图号校核筛选：review|unlabeled|low_confidence|conflict|unresolved",
    ),
    favorite: bool | None = Query(None, description="true=只看收藏文献里的图"),
    collection_id: str | None = Query(None, description="只看某集合（含子集合）文献里的图"),
    workgroup_id: str | None = Query(
        None, description="工作组视图：只看共享到这个组的文献里的图（M7）"
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FigureSearchOut:
    if figure_type and figure_type not in FIGURE_TYPES:
        raise HTTPException(status_code=422, detail=f"figure_type 只支持 {FIGURE_TYPES}")
    if label_state and label_state not in LABEL_STATES:
        raise HTTPException(status_code=422, detail=f"label_state 只支持 {LABEL_STATES}")

    current = scoped_user(user)

    # 图表列表和文献列表用同一套「个人 / 工作组」范围（M7）
    scope_condition = None
    if workgroup_id:
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
        scope_condition = workgroup_paper_condition(workgroup_id)
    elif current is not None:
        scope_condition = private_paper_condition(current)

    # paper 参数先解析成确定的 paper id；解析不出来才退回文本检索（E1）。
    paper_text = (paper or "").strip()
    paper_ids = await _resolve_paper_ids(session, paper_text) if paper_text else None
    parts = [p for p in (q, paper_text if paper_ids is None else None) if p]
    pq = parse_query(" ".join(parts))

    norm_label = normalize_label_filter(label)
    if norm_label:
        pq.label = norm_label

    # M9.1b-lite：中文查询先让已配置的 chat 模型改写成英文术语，再走 BM25；
    # 这是词面检索辅助，不是 embedding 向量召回。失败/未配置时静默用原查询。
    expansion = None
    search_pq = pq
    if classify(pq) == "semantic" and should_expand(pq.text):
        expansion = await expand_query(pq.text)
        if expansion is not None:
            search_pq = replace(pq, text=expansion.text)

    collection_ids: list[str] | None = None
    if collection_id:
        if current is not None:
            collection = await session.get(Collection, collection_id)
            if collection is None or not (
                is_superuser(current) or collection.owner_user_id in (None, current.id)
            ):
                raise HTTPException(status_code=404, detail="collection not found")
        # 与文献列表一致：选父集合 = 看整棵子树
        all_cols = (
            await session.execute(select(Collection.id, Collection.parent_id))
        ).all()
        children: dict[str | None, list[str]] = {}
        for cid, pid in all_cols:
            children.setdefault(pid, []).append(cid)
        ids: list[str] = [collection_id]
        stack = [collection_id]
        while stack:
            for child in children.get(stack.pop(), []):
                if child not in ids:
                    ids.append(child)
                    stack.append(child)
        collection_ids = ids

    mode, degraded, engine, total, rows = await search_figures(
        session,
        search_pq,
        figure_type=figure_type,
        review_status=review,
        label_state=label_state,
        favorite=favorite,
        collection_ids=collection_ids,
        paper_ids=paper_ids,
        scope_condition=scope_condition,
        favorite_user_id=current.id if current is not None else None,
        limit=limit,
        offset=offset,
    )

    items = [_to_out(fig, ver, paper_) for fig, ver, paper_ in rows]

    hint = None
    if expansion is not None:
        terms = "、".join(expansion.keywords[:6]) or expansion.english_query
        if items:
            hint = (
                f"查询已由 {expansion.provider}/{expansion.model} 改写为英文术语"
                f"（{terms}），再走 BM25 词面排序；不是向量召回"
            )
        else:
            hint = f"已用模型改写查询（{terms}），仍无词面命中；换更具体的术语再试"
    elif engine == "bm25" and not items:
        hint = "BM25 词面检索没有匹配；换关键词再试（向量召回 / BYO embedding 尚未接入）"
    elif degraded:
        hint = "语义检索为旧 ILIKE 基线（仅评估用）"
    elif mode == "provenance" and not items:
        if paper_ids and not pq.label:
            hint = "这篇文献没有匹配的图（还没有解析出图，或图已被删除）"
        else:
            hint = "出处检索没有命中。请检查图号是否与被引版本一致（预印本与正式版编号常不同）"

    return FigureSearchOut(
        mode=mode,
        degraded=degraded,
        engine=engine,
        hint=hint,
        total=total,
        items=items,
        query_expanded=expansion is not None,
        expanded_keywords=list(expansion.keywords) if expansion is not None else [],
        expansion_provider=expansion.provider if expansion is not None else None,
        expansion_model=expansion.model if expansion is not None else None,
    )


@router.post("/{figure_id}/review", response_model=FigureOut)
async def review_figure(
    figure_id: str,
    body: ReviewIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FigureOut:
    """登记一次纠错判定。人工点和 agent 调用走同一条通道。

    ``verdict=wrong`` 时可以顺带给修正框；修正框会立刻用于 overlay。
    """
    row = (
        await session.execute(
            select(Figure, PaperVersion, Paper)
            .join(PaperVersion, Figure.paper_version_id == PaperVersion.id)
            .join(Paper, PaperVersion.paper_id == Paper.id)
            .where(Figure.id == figure_id, Figure.deleted_at.is_(None))
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="figure not found")
    fig, ver, paper = row
    # 可见性 + 写权限：看不见的图统一 404（不能用 403 确认它存在）
    await ensure_figure_edit(session, user, figure_id)
    current = scoped_user(user)
    actor = current.username if current is not None else (body.reviewer or "anonymous")

    before = snapshot_figure(fig)
    if body.bbox is not None:
        if len(body.bbox) != 4:
            raise HTTPException(status_code=422, detail="bbox 必须是 4 个数 [x0,y0,x1,y1]")
        x0, y0, x1, y1 = (float(v) for v in body.bbox)
        if x1 <= x0 or y1 <= y0:
            raise HTTPException(status_code=422, detail="bbox 需要 x1>x0 且 y1>y0")
        fig.bbox_override_json = [x0, y0, x1, y1]

    fig.review_status = body.verdict
    fig.review_note = body.note
    fig.reviewed_by = actor
    fig.reviewed_at = datetime.now(timezone.utc)
    write_audit(
        session,
        actor=actor,
        action="figure.review",
        target_type="figure",
        target_id=fig.id,
        before=before,
        after=snapshot_figure(fig),
        note=body.note,
    )

    if fig.bbox_override_json:
        # 缩略图也跟着修正框重裁一次，闭环才算闭上：overlay 对、卡片也对
        try:
            new_path = await asyncio.get_running_loop().run_in_executor(
                None,
                _render_crop_to_storage,
                [float(v) for v in fig.bbox_override_json],
                fig.page,
                fig.cite_key,
                ver.file_path,
            )
            if new_path:
                fig.image_path = new_path
        except Exception:  # noqa: BLE001 — 重裁失败不能让纠错请求失败
            log.exception("figure %s 缩略图重裁失败", fig.id)

    await session.commit()
    await session.refresh(fig)
    return _to_out(fig, ver, paper)


@router.get("/label-stats")
async def label_stats(
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """各「需要人工看一眼」的图号状态计数，给前端顶栏的待确认徽标用。"""
    current = scoped_user(user)

    async def count(*conds) -> int:
        stmt = (
            select(func.count())
            .select_from(Figure)
            .join(PaperVersion, Figure.paper_version_id == PaperVersion.id)
            .join(Paper, PaperVersion.paper_id == Paper.id)
            .where(Figure.deleted_at.is_(None))
        )
        if current is not None:
            stmt = stmt.where(private_paper_condition(current))
        for cond in conds:
            stmt = stmt.where(cond)
        return int((await session.execute(stmt)).scalar() or 0)

    review_total = await count(
        or_(
            Figure.label_raw.is_(None),
            Figure.label_confidence < LOW_CONFIDENCE,
            Figure.label_source.in_(("ocr_cross_conflict", "ocr_cross_unresolved")),
        )
    )
    return {
        "unlabeled": await count(Figure.label_raw.is_(None)),
        "low_confidence": await count(
            Figure.label_confidence.isnot(None), Figure.label_confidence < LOW_CONFIDENCE
        ),
        "conflict": await count(Figure.label_source == "ocr_cross_conflict"),
        "unresolved": await count(Figure.label_source == "ocr_cross_unresolved"),
        "review_total": review_total,
    }


@router.post("/{figure_id}/label", response_model=FigureOut)
async def set_figure_label(
    figure_id: str,
    body: LabelIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FigureOut:
    """人工/agent 改图号（阶段 3 校核入口）。

    * 传 ``label=""`` = 确认这张图没有编号；
    * 归一化成功 → 写 label_raw/label_norm/num/sublabel，来源 ``review``、置信 1.0；
    * 归一化失败但原文有内容 → 保留原文、label_norm 置空（例如习题图「图 4.e.1」
      本来就不适合塞进 fig:x 体系，但人工确认的写法要留着）。
    """
    row = (
        await session.execute(
            select(Figure, PaperVersion, Paper)
            .join(PaperVersion, Figure.paper_version_id == PaperVersion.id)
            .join(Paper, PaperVersion.paper_id == Paper.id)
            .where(Figure.id == figure_id, Figure.deleted_at.is_(None))
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="figure not found")
    fig, ver, paper = row
    # 可见性 + 写权限：看不见的图统一 404（共享文献只读 → 403）
    await ensure_figure_edit(session, user, figure_id)
    current = scoped_user(user)
    actor = current.username if current is not None else (body.reviewer or "human")

    before = snapshot_figure(fig)
    raw = (body.label or "").strip()
    if raw:
        # 「图 4.E.1」这类字母段编号不塞进 fig:x 体系（normalize 会退化成 fig:4）
        letter_seg = re.search(r"(图|表)\s*\d{1,3}\s*[.\-]\s*[A-Za-z]", raw)
        norm = None if letter_seg else normalize_label(raw)
        if norm and not norm.startswith("fig"):
            raise HTTPException(status_code=422, detail="这里只接受图号（图 / Fig）")
        if norm is None and not LABEL_RE.search(raw):
            raise HTTPException(
                status_code=422, detail="不像图号（需要「图 4-2」/「Fig. 3」这类写法）"
            )
        fig.label_raw = raw
        fig.label_norm = norm
        num, _ = split_label(norm) if norm else (None, None)
        fig.label_num = num
        # sublabel 只存子图字母（a/b/c）。「图 4.2.1」的编号尾段 2-1 不是子图字母；
        # 「图 4.2.1a」末尾的 a 才是。不能用 split_label 的 sub（它按第一个 - 切，会得到 2-1）。
        letter = re.search(r"([a-z])$", norm or "")
        fig.sublabel = letter.group(1) if letter else None
        fig.label_confidence = 1.0
        # 人工改号也算一次「人工判断」，落审计字段（重解析按页面+bbox 回填时会一起保留）
        fig.reviewed_by = actor
        fig.reviewed_at = datetime.now(timezone.utc)
    else:
        fig.label_raw = None
        fig.label_norm = None
        fig.label_num = None
        fig.sublabel = None
        fig.label_confidence = 0.0
    fig.label_source = "review"
    write_audit(
        session,
        actor=actor,
        action="figure.label.set",
        target_type="figure",
        target_id=fig.id,
        before=before,
        after=snapshot_figure(fig),
        note=f"label={raw!r}",
    )
    log.info("figure %s label → %r（by %s）", fig.id, raw or "（清空）", actor)

    await session.commit()
    await session.refresh(fig)
    return _to_out(fig, ver, paper)


@router.post("/{figure_id}/group", response_model=FigureOut)
async def group_figure(
    figure_id: str,
    body: GroupIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FigureOut:
    """系列子图的手工归并/拆开。

    * ``split``：把这张图从它所在的系列组里拆出来（子图归组是几何启发式，误报时用）；
    * ``merge``：把这张图和 ``member_ids``（同一版本的图）归到一个新组，按阅读顺序标 a/b/c。
    """
    row = (
        await session.execute(
            select(Figure, PaperVersion, Paper)
            .join(PaperVersion, Figure.paper_version_id == PaperVersion.id)
            .join(Paper, PaperVersion.paper_id == Paper.id)
            .where(Figure.id == figure_id, Figure.deleted_at.is_(None))
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="figure not found")
    fig, ver, paper = row
    # 可见性 + 写权限：看不见的图统一 404；共享只读 → 403
    await ensure_figure_edit(session, user, figure_id)

    if body.action == "split":
        fig.group_key = None
        fig.sublabel = None
    else:
        ids = [figure_id] + [i for i in body.member_ids if i != figure_id]
        rows = (
            await session.execute(
                select(Figure).where(
                    Figure.id.in_(ids),
                    Figure.paper_version_id == fig.paper_version_id,
                    Figure.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        if len(rows) != len(ids):
            raise HTTPException(status_code=422, detail="member_ids 里有不存在或跨版本的图")
        ordered = sorted(
            rows,
            key=lambda f: (
                f.page,
                (f.bbox_json or [0, 0])[1] or 0,
                (f.bbox_json or [0, 0])[0] or 0,
            ),
        )
        key = f"manual-{uuid.uuid4().hex[:8]}"
        for idx, item in enumerate(ordered):
            item.group_key = key
            item.sublabel = chr(ord("a") + idx) if idx < 26 else None

    await session.commit()
    await session.refresh(fig)
    return _to_out(fig, ver, paper)


@router.get("/{figure_id}", response_model=FigureOut)
async def get_figure(
    figure_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FigureOut:
    row = (
        await session.execute(
            select(Figure, PaperVersion, Paper)
            .join(PaperVersion, Figure.paper_version_id == PaperVersion.id)
            .join(Paper, PaperVersion.paper_id == Paper.id)
            .where(Figure.id == figure_id, Figure.deleted_at.is_(None))
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="figure not found")
    await ensure_paper_access(session, user, row[2].id)
    return _to_out(*row)


@router.get("/{figure_id}/image")
async def get_figure_image(
    figure_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    fig = (
        await session.execute(
            select(Figure).where(Figure.id == figure_id, Figure.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if fig is None or not fig.image_path:
        raise HTTPException(status_code=404, detail="figure image not found")
    await ensure_figure_access(session, user, figure_id)

    resolved = resolve_within_subdir(settings.storage_dir, fig.image_path, "images")
    media = _MEDIA.get(resolved.suffix.lower(), "application/octet-stream")
    # 图跟文献一样是私有内容：共享反代/共享浏览器缓存不能把它当公共资源缓存
    return FileResponse(
        resolved,
        media_type=media,
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.post("", response_model=FigureOut, status_code=201)
async def create_figure(
    body: FigureCreateIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FigureOut:
    """用户框选添加一张图（补解析器漏掉的图）。

    从文献**最新版本**的 PDF 对应页裁图入库，``origin=manual``。
    worker 重解析时会快照并回填 manual 图（见 workers/run.py），不会被冲掉。
    """
    paper = await ensure_paper_edit(session, user, body.paper_id)
    ver = (
        await session.execute(
            select(PaperVersion)
            .where(PaperVersion.paper_id == paper.id)
            .order_by(PaperVersion.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if ver is None:
        raise HTTPException(status_code=404, detail="paper has no version")
    if ver.page_count and body.page > ver.page_count:
        raise HTTPException(status_code=422, detail=f"页码超出范围（1–{ver.page_count}）")

    x0, y0, x1, y1 = (float(v) for v in body.bbox)
    if x1 <= x0 or y1 <= y0:
        raise HTTPException(status_code=422, detail="bbox 需要 x1>x0 且 y1>y0")
    if min(x1 - x0, y1 - y0) < 8:
        raise HTTPException(status_code=422, detail="框太小了（短边 < 8pt）")

    raw = (body.label or "").strip()
    norm = normalize_label(raw) if raw else None
    if raw and norm is None and not LABEL_RE.search(raw):
        raise HTTPException(status_code=422, detail="不像图号（需要「图 4-2」/「Fig. 3」这类写法）")

    cite_key = f"man-{uuid.uuid4().hex[:10]}"
    image_path = await asyncio.get_running_loop().run_in_executor(
        None,
        _render_crop_to_storage,
        [x0, y0, x1, y1],
        body.page,
        cite_key,
        ver.file_path,
    )
    if image_path is None:
        raise HTTPException(status_code=422, detail="裁图失败：页码或框可能落在页面外")

    num, _ = split_label(norm) if norm else (None, None)
    letter = re.search(r"([a-z])$", norm or "")
    fig = Figure(
        paper_version_id=ver.id,
        page=body.page,
        bbox_json=[x0, y0, x1, y1],
        label_raw=raw or None,
        label_norm=norm,
        label_num=num,
        sublabel=letter.group(1) if letter else None,
        caption=(body.caption or "").strip(),
        license_hint="unknown",
        cite_key=cite_key,
        label_source="review" if raw else "none",
        label_confidence=1.0 if raw else 0.0,
        origin="manual",
        review_status="pending",
        image_path=image_path,
    )
    session.add(fig)
    await session.commit()
    await session.refresh(fig)
    log.info("figure %s 手动添加（paper %s p%s）", fig.id, paper.id, fig.page)
    return _to_out(fig, ver, paper)


@router.delete("/{figure_id}")
async def delete_figure(
    figure_id: str,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """软删除一张图（解析器误报 / 用户加错了）。

    不物理删：worker 重解析会按 cite_key 把删除标记重新压回新解析结果上，
    否则用户删掉的图会在下一次重解析后全部复活。所有列表/检索/计数都过滤 deleted_at。
    """
    row = (
        await session.execute(
            select(Figure, PaperVersion)
            .join(PaperVersion, Figure.paper_version_id == PaperVersion.id)
            .where(Figure.id == figure_id, Figure.deleted_at.is_(None))
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="figure not found")
    fig, _ver = row
    await ensure_figure_edit(session, user, fig.id)
    fig.deleted_at = datetime.now(timezone.utc)
    await session.commit()
    log.info("figure %s 删除（origin=%s）", fig.id, fig.origin)
    return {"figure_id": fig.id, "deleted": True}
