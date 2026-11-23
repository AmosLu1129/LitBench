# SPDX-License-Identifier: AGPL-3.0-or-later
"""后台 worker。

    python -m litbench.workers.run

消费 job 表。三个必须守住的性质：
1. **幂等**：重复执行同一个 job 不产生重复数据（落库前先按 version 删旧行）。
2. **可断点续跑**：进程被杀后，带 ``running`` 状态的 job 会被回收重跑。
3. **失败可见**：异常写进 ``job.error`` 并把状态置为 failed，绝不静默吞掉。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .. import lite_ocr, ocr_cross_validate
from ..adapters import ParsedDocument, get_parser
from ..config import settings
from ..db import dispose_engine, get_sessionmaker
from ..escalate import can_escalate, find_weakness
from ..figure_filter import drop_qr_figures
from ..labeling import TextQuality, apply_label_rules, assess_text_layer, needs_text_layer_audit
from ..labels import normalize_label, split_label
from ..models import (
    Block,
    Figure,
    FigureProposal,
    Job,
    OcrProposal,
    Paper,
    PaperVersion,
    TableBlock,
)
from ..ocr_cross import cross_check_figures, merge_text_layer, rapidocr_available
from ..paths import resolve_data_path, to_storage_relative
from ..reparse import reconcile_ai_products, snapshot_ai_products
from ..tasks import recover_expired_tasks
from .tasks import run_task_once

log = logging.getLogger("litbench.worker")

STALE_RUNNING_MINUTES = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def make_cite_key(paper_id: str, version_idx: int, label_norm: str | None, seq: int) -> str:
    """稳定的图引用键。形如 ``1a2b3c-v1-f3``。

    为什么不用 page：重排版会变；为什么不用自增 id：换库会变。
    ``(paper, version, label)`` 才是用户和 agent 都能稳定引用的身份。
    """
    short = paper_id[:6]
    tail = (label_norm or f"x{seq}").replace(":", "")
    return f"{short}-v{version_idx}-{tail}"


def backend_of(job: Job) -> str:
    """这个 job 该用哪个后端：payload 里指定优先（按需升级/OCR 会指定），否则用全局默认。"""
    return str((job.payload_json or {}).get("backend") or settings.parser_backend)


def ocr_available(backend: str) -> tuple[bool, str]:
    """OCR 后端现在能不能用。返回 ``(可用, 不可用原因)``。

    没有可用后端时宁可停在 ``needs_ocr`` 并留下原因，也不排一个注定失败的 job。
    """
    if backend == "mineru":
        cmd = settings.mineru_cmd
        if not (Path(cmd).is_file() or shutil.which(cmd)):
            return False, f"mineru CLI 不存在：{cmd}"
        return True, ""
    if backend == "docling":
        if importlib.util.find_spec("docling") is None:
            return False, "docling 未安装"
        if importlib.util.find_spec("onnxruntime") is None:
            return False, "onnxruntime 未安装（docling OCR 需要 RapidOCR 的 ONNX 引擎）"
        return True, ""
    return False, f"未知 OCR 后端：{backend or '（空）'}"


async def claim_job(db: AsyncSession) -> Job | None:
    """原子地领一个排队中的 job；没有就返回 None。

    行锁不是每个库都有：Postgres 有 ``FOR UPDATE SKIP LOCKED``，**SQLite 会静默忽略**
    FOR UPDATE（编译出来的 SQL 里根本没有这个子句，也不报错）。只靠它的话，两个
    worker 在 SQLite 上会领到同一个 job、把同一篇论文解析两遍。

    所以这里再加一次乐观 CAS：``UPDATE ... WHERE id=? AND status='queued'``，
    谁的 rowcount 是 1 谁才拿到。两个库上都只有一个赢家。
    """
    stmt = select(Job).where(Job.status == "queued").order_by(Job.created_at).limit(1)
    if db.sync_session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)  # 先锁住，少一次 CAS 冲突
    job = (await db.execute(stmt)).scalar_one_or_none()
    if job is None:
        return None

    result = await db.execute(
        update(Job)
        .where(Job.id == job.id, Job.status == "queued")
        .values(
            status="running",
            started_at=_now(),
            attempts=func.coalesce(Job.attempts, 0) + 1,
        )
    )
    await db.commit()
    if not result.rowcount:
        log.info("job %s 已被另一个 worker 领走", job.id)
        return None

    await db.refresh(job)
    return job


async def _clear_version_rows(db: AsyncSession, version_id: str) -> None:
    for model in (Block, Figure, TableBlock):
        await db.execute(delete(model).where(model.paper_version_id == version_id))


async def _snapshot_reviews(db: AsyncSession, version_id: str) -> dict[str, dict]:
    """重解析前把人工/agent 的复核结果记下来。

    「先删后插」保证幂等，但 figure 行里有 review_status / bbox_override —— 那是人工看过
    的结论，重解析一次就没了。用户点过「定位正确」，第二天系统自己升级重解析又变回未审核，
    这条产品线就废了。按 ``cite_key`` 迁移（cite_key 由 paper/version/label 决定，重解析稳定）。
    """
    rows = (
        await db.execute(
            select(Figure).where(
                Figure.paper_version_id == version_id, Figure.review_status != "pending"
            )
        )
    ).scalars().all()
    return {
        f.cite_key: {
            "review_status": f.review_status,
            "review_note": f.review_note,
            "bbox_override_json": f.bbox_override_json,
            "reviewed_by": f.reviewed_by,
            "reviewed_at": f.reviewed_at,
        }
        for f in rows
    }


async def _snapshot_manual_figures(db: AsyncSession, version_id: str) -> list[dict]:
    """重解析前把用户手动添加的图记下来（origin=manual）。

    「先删后插」会把它们一起删掉，不恢复的话用户框选加的图重解析一次就没了。
    """
    rows = (
        await db.execute(
            select(Figure).where(
                Figure.paper_version_id == version_id,
                Figure.origin == "manual",
                Figure.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    return [
        {
            "page": f.page,
            "bbox_json": f.bbox_json,
            "label_raw": f.label_raw,
            "label_norm": f.label_norm,
            "label_num": f.label_num,
            "sublabel": f.sublabel,
            "caption": f.caption,
            "figure_type": f.figure_type,
            "license_hint": f.license_hint,
            "cite_key": f.cite_key,
            "image_path": f.image_path,
            "label_source": f.label_source,
            "label_confidence": f.label_confidence,
            "group_key": f.group_key,
            "origin": "manual",
            "review_status": f.review_status,
            "review_note": f.review_note,
            "bbox_override_json": f.bbox_override_json,
            "reviewed_by": f.reviewed_by,
            "reviewed_at": f.reviewed_at,
        }
        for f in rows
    ]


async def _snapshot_deleted_keys(db: AsyncSession, version_id: str) -> set[str]:
    """重解析前记下用户删掉的图（软删除），解析完按 cite_key 把删除标记压回去。"""
    rows = (
        await db.execute(
            select(Figure.cite_key).where(
                Figure.paper_version_id == version_id, Figure.deleted_at.isnot(None)
            )
        )
    ).scalars().all()
    return set(rows)


def _bbox_iou(a: list[float], b: list[float]) -> float:
    if len(a) != 4 or len(b) != 4:
        return 0.0
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


async def _snapshot_label_reviews(db: AsyncSession, version_id: str) -> list[dict]:
    """重解析前记下人工改过的图号（label_source=review）。

    为什么不能只按 cite_key：改图号会改 label_norm，cite_key 也跟着变
    （比如用户把「图 4-500」改成「图 4-50」），下次解析出来的 cite_key 对不上。
    所以按「page + bbox IoU」匹配回填——解析器切图位置是稳定的。
    """
    rows = (
        await db.execute(
            select(Figure).where(
                Figure.paper_version_id == version_id,
                Figure.label_source == "review",
                Figure.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    return [
        {
            "page": f.page,
            "bbox": [float(v) for v in (f.bbox_json or [])],
            "label_raw": f.label_raw,
            "label_norm": f.label_norm,
            "label_num": f.label_num,
            "sublabel": f.sublabel,
            "reviewed_by": f.reviewed_by,
            "reviewed_at": f.reviewed_at,
        }
        for f in rows
    ]


async def persist(db: AsyncSession, version: PaperVersion, doc: ParsedDocument) -> int:
    """把归一化后的解析结果写库。先删后插，保证幂等。

    复核结果（review_status / bbox_override）按 cite_key 迁移过去——**解析可以重跑，
    人工的判断不能丢**。用户手动加的图（origin=manual）同样快照回填；
    用户删掉的图（deleted_at）按 cite_key 重新压回新解析结果上。
    摘要证据 / 图纠错提案 / 写作卡证据锚在旧 block/figure/table id 上，按稳定身份重锚
    （见 reparse.py）；映射不回来的写作卡证据标 ``evidence_stale``，卡片本身不删。
    """
    saved_reviews = await _snapshot_reviews(db, version.id)
    saved_manual = await _snapshot_manual_figures(db, version.id)
    saved_labels = await _snapshot_label_reviews(db, version.id)
    deleted_keys = await _snapshot_deleted_keys(db, version.id)
    # 摘要证据 / 图纠错提案锚在 block.id / figure.id 上，必须在删旧行之前快照（见 reparse.py）。
    ai_snapshot = await snapshot_ai_products(db, version.id)
    # 显式先删提案：SQLite 打开 FK 后删 figure 会级联删掉 proposal，而我们要按 cite_key
    # 把它们重建到新 figure 上；PG 也统一走这一条，不依赖 CASCADE 的时序。
    await db.execute(
        delete(FigureProposal).where(FigureProposal.paper_version_id == version.id)
    )
    await _clear_version_rows(db, version.id)

    block_rows: list[Block] = []
    for idx, b in enumerate(doc.blocks, 1):
        row = Block(
            paper_version_id=version.id,
            page=b.page,
            bbox_json=list(b.bbox),
            type=b.type,
            text=b.text,
            order_idx=b.order_idx,
            engine=b.engine or doc.engine,
        )
        db.add(row)
        block_rows.append(row)
        # 分批 flush：一篇 20 页论文有上千个 block，一次性 flush 会拼出
        # 上万参数的 INSERT，SQLite / 某些驱动会在这里翻车，也容易长时间占锁。
        if idx % 200 == 0:
            await db.flush()

    version_idx = await _version_ordinal(db, version)
    used_keys: set[str] = set()
    settings.image_dir.mkdir(parents=True, exist_ok=True)

    new_figure_rows: list[Figure] = []
    for i, fig in enumerate(doc.figures):
        # 「两套 OCR 一致但编号无法安全归一」的（如习题图 图 4.e.1）不写 label_norm，
        # 否则 normalize_label 会退化成 fig:4，污染检索；label_raw 保留供 review。
        norm = (
            None
            if fig.label_source == "ocr_cross_unresolved"
            else (normalize_label(fig.label_raw) if fig.label_raw else None)
        )
        num, _ = split_label(norm) if norm else (None, None)
        key = make_cite_key(version.paper_id, version_idx, norm, i)
        if key in used_keys:  # label 重复时保底，避免撞唯一约束
            key = f"{key}-{i}"
        used_keys.add(key)

        image_path = None
        if fig.image_bytes:
            target = settings.image_dir / f"{key}.png"
            target.write_bytes(fig.image_bytes)
            image_path = to_storage_relative(settings.storage_dir, target)

        row = Figure(
            paper_version_id=version.id,
            page=fig.page,
            bbox_json=list(fig.bbox),
            label_raw=fig.label_raw,
            label_norm=norm,
            label_num=num,
            sublabel=fig.sublabel,
            caption=fig.caption,
            figure_type=fig.figure_type,
            cite_key=key,
            image_path=image_path,
            label_source=fig.label_source,
            label_confidence=fig.label_confidence,
            group_key=fig.group_key,
        )
        db.add(row)
        new_figure_rows.append(row)

    restored = 0
    for row in new_figure_rows:
        saved = saved_reviews.get(row.cite_key)
        if not saved:
            continue
        row.review_status = saved["review_status"]
        row.review_note = saved["review_note"]
        row.bbox_override_json = saved["bbox_override_json"]
        row.reviewed_by = saved["reviewed_by"]
        row.reviewed_at = saved["reviewed_at"]
        restored += 1
    if restored:
        log.info("保留 %d 条复核记录（version %s）", restored, version.id)

    # 人工改过的图号：按 page + bbox IoU 回填（改号会改 cite_key，不能只按 key 匹配）
    label_restored = 0
    for saved in saved_labels:
        best: Figure | None = None
        best_iou = 0.0
        for row in new_figure_rows:
            if row.page != saved["page"]:
                continue
            iou = _bbox_iou(list(row.bbox_json or []), saved["bbox"])
            if iou > best_iou:
                best, best_iou = row, iou
        if best is None or best_iou < 0.5:
            continue
        best.label_raw = saved["label_raw"]
        best.label_norm = saved["label_norm"]
        best.label_num = saved["label_num"]
        best.sublabel = saved["sublabel"]
        best.label_source = "review"
        best.label_confidence = 1.0
        best.reviewed_by = saved["reviewed_by"]
        best.reviewed_at = saved["reviewed_at"]
        label_restored += 1
    if label_restored:
        log.info("保留 %d 条人工改号（version %s）", label_restored, version.id)

    # 用户手动加的图：重解析不能把它们删掉（框/图号/缩略图原样回填）
    manual_restored = 0
    manual_rows: list[Figure] = []
    for data in saved_manual:
        if data["cite_key"] in used_keys:
            continue
        row = Figure(paper_version_id=version.id, **data)
        db.add(row)
        manual_rows.append(row)
        used_keys.add(data["cite_key"])
        manual_restored += 1
    if manual_restored:
        log.info("保留 %d 张手动添加的图（version %s）", manual_restored, version.id)

    # 用户删掉的图：解析器又把它产出来时，重新压上删除标记（否则删了会复活）
    tombstoned = 0
    for row in new_figure_rows:
        if row.cite_key in deleted_keys:
            row.deleted_at = _now()
            tombstoned += 1
    if tombstoned:
        log.info("重新压回 %d 个删除标记（version %s）", tombstoned, version.id)

    # 重建 AI 产物要用「新解析结果」的章节切分（重解析可能换了 backend）。
    version.parser = doc.engine
    version.parser_version = doc.engine_version
    version.page_count = doc.page_count
    version.parsed_at = _now()
    version.text_quality = doc.text_quality
    version.text_quality_flags_json = doc.text_quality_flags or {}

    table_rows: list[TableBlock] = []
    for t in doc.tables:
        row = TableBlock(
            paper_version_id=version.id,
            page=t.page,
            html=t.html,
            cells_json=t.cells,
            caption=t.caption,
            quality=t.quality,
        )
        db.add(row)
        table_rows.append(row)

    # 新 block / figure / table（含 manual 回填）的 id 要先落出来，
    # 摘要证据 / 提案 / 写作卡证据才挂得上。
    await db.flush()
    ai_stats = await reconcile_ai_products(
        db, version, ai_snapshot, block_rows, [*new_figure_rows, *manual_rows], table_rows
    )
    if any(ai_stats.values()):
        log.info("重解析 AI 产物迁回（version %s）：%s", version.id, ai_stats)

    paper = await db.get(Paper, version.paper_id)
    if paper:
        # 没有文字层的扫描件要显式标出来，别让下游以为「解析成功但内容为空」
        paper.status = "parsed" if doc.text_layer else "needs_ocr"
    return len(doc.blocks) + len(doc.figures) + len(doc.tables)


async def _version_ordinal(db: AsyncSession, version: PaperVersion) -> int:
    """同一篇文献里的第几个版本（1 起）。cite_key 里要用它区分 v1 / v2。"""
    stmt = select(func.count()).select_from(PaperVersion).where(
        PaperVersion.paper_id == version.paper_id,
        PaperVersion.created_at <= version.created_at,
    )
    return int((await db.execute(stmt)).scalar() or 1)


async def _maybe_escalate(
    db: AsyncSession, job: Job, version: PaperVersion, doc: ParsedDocument
) -> Job | None:
    """轻量档解析完发现明显不够时，排一个增强档 job。返回新 job（没排就 None）。

    两条必须守住的约束：

    - **只升一级**：增强档自己解析完不再升级。否则每次失败都再排一次，队列会自己繁殖。
    - **同一版本只排一次**：重复上传同一份 PDF 会重解析，但没必要反复花那 60 秒。

    这是「按需」的全部含义：默认仍然是秒级的 pdfium，只有量化判据说不够时才上模型。
    """
    payload = job.payload_json or {}
    if payload.get("escalated_from") or payload.get("ocr") or payload.get("lite_ocr"):
        # 已经是增强档 / OCR / 轻量 OCR 结果，不再往上叠
        return None

    target = (settings.escalate_backend or "").strip().lower()
    available, why = can_escalate(target, backend_of(job), mineru_url=settings.mineru_url)
    if not available:
        log.info("跳过增强解析：%s", why)
        return None

    weakness = find_weakness(
        doc,
        min_missing=settings.escalate_min_missing,
        min_caption_chars=settings.escalate_min_caption_chars,
    )
    if weakness is None:
        return None

    siblings = (
        await db.execute(
            select(Job).where(
                Job.paper_version_id == version.id,
                Job.id != job.id,
                Job.status.in_(("queued", "running", "done")),
            )
        )
    ).scalars().all()
    if any((s.payload_json or {}).get("backend") == target for s in siblings):
        log.info("版本 %s 已有增强档 job，跳过重复升级", version.id)
        return None

    child = Job(
        kind="parse",
        paper_version_id=version.id,
        payload_json={
            "backend": target,
            "escalated_from": job.id,
            "code": weakness.code,
            "reason": weakness.reason,
        },
    )
    db.add(child)
    await db.flush()  # child.id 要落出来，写回父 job 的 payload 里
    job.payload_json = {
        **payload,
        "escalated_to": child.id,
        "escalate_reason": weakness.reason,
    }
    log.info("按需升级 → %s：%s（job %s → %s）", target, weakness.reason, job.id, child.id)
    return child


async def _maybe_ocr(
    db: AsyncSession, job: Job, version: PaperVersion, doc: ParsedDocument
) -> Job | None:
    """没有文字层的扫描件 → 排一个 OCR job（默认 MinerU）。

    和按需升级同构，守住同样的三条：只排一次、同一版本不重复排、后端不可用就
    停在 ``needs_ocr`` 并留下原因（不能装成功让人以为在跑）。
    """
    if doc.text_layer:
        return None

    payload = job.payload_json or {}
    if payload.get("ocr_from"):
        return None  # 这一轮已经是 OCR 的结果

    target = (settings.ocr_backend or "").strip().lower()
    if not target:
        log.info("扫描件没有文字层，但 OCR 后端未配置（OCR_BACKEND 为空）")
        return None

    available, why = ocr_available(target)
    if not available:
        log.warning("扫描件需要 OCR，但不可用：%s", why)
        return None

    siblings = (
        await db.execute(
            select(Job).where(
                Job.paper_version_id == version.id,
                Job.id != job.id,
                Job.status.in_(("queued", "running", "done")),
            )
        )
    ).scalars().all()
    already = any(
        (s.payload_json or {}).get("ocr") and (s.payload_json or {}).get("backend") == target
        for s in siblings
    )
    if already:
        log.info("版本 %s 已有 OCR job，跳过重复排", version.id)
        return None

    child = Job(
        kind="parse",
        paper_version_id=version.id,
        payload_json={"backend": target, "ocr": True, "ocr_from": job.id},
    )
    db.add(child)
    await db.flush()  # child.id 要落出来写回父 job
    job.payload_json = {**payload, "ocr_to": child.id}
    log.info("没有文字层 → 排 OCR job（%s）：%s → %s", target, job.id, child.id)
    return child


async def _maybe_lite_ocr(
    job: Job, version: PaperVersion, doc: ParsedDocument, pdf_path: Path
) -> list[tuple[int, str]]:
    """扫描页 → RapidOCR 文字块。返回 ``[(页码, 该页 OCR 全文)]``。

    只补没有文字块的页；不排子 job，直接合并进当前文档。OCR 不可用时返回空，
    后续仍走原有 ``_maybe_ocr``（如果配置了重后端）。
    """
    if not settings.lite_ocr_enabled:
        return []
    if not lite_ocr.lite_ocr_available():
        log.info("轻量 OCR 不可用（rapidocr/pypdfium2 未安装），跳过")
        return []
    loop = asyncio.get_running_loop()
    try:
        pages = await loop.run_in_executor(
            None,
            lambda: lite_ocr.ocr_missing_pages(
                pdf_path,
                doc,
                dpi=settings.lite_ocr_dpi,
                max_pages=settings.lite_ocr_max_pages,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — OCR 失败不能拖垮整篇
        log.warning("job %s 轻量 OCR 失败（保留原解析）：%s", job.id, exc)
        return []
    if not pages:
        return []
    added = lite_ocr.merge_into_document(doc, pages)
    job.payload_json = {
        **(job.payload_json or {}),
        "lite_ocr": True,
        "lite_ocr_pages": len(pages),
    }
    log.info(
        "job %s 轻量 OCR：%s 页 / 新增 %s 个文字块（version %s）",
        job.id,
        len(pages),
        added,
        version.id,
    )
    return [(page.page, page.text) for page in pages if page.text.strip()]


async def _cross_validate_lite_ocr(
    pdf_path: Path, ocr_pages: list[tuple[int, str]]
) -> list[tuple[int, ocr_cross_validate.OcrCrossCheck]]:
    """OCR 页逐页交给视觉模型校对；没配置模型时返回空（保留 OCR 结果）。"""
    if not settings.ocr_cross_validate or not ocr_pages:
        return []
    try:
        return await ocr_cross_validate.cross_check_pages(
            pdf_path,
            ocr_pages,
            limit=settings.ocr_cross_validate_max_pages,
        )
    except Exception as exc:  # noqa: BLE001 — 校对失败保留 OCR
        log.warning("轻量 OCR 交叉校验失败（保留 OCR 结果）：%s", exc)
        return []


async def _save_ocr_proposals(
    db: AsyncSession,
    version: PaperVersion,
    results: list[tuple[int, ocr_cross_validate.OcrCrossCheck]],
    ocr_pages: list[tuple[int, str]],
) -> int:
    """模型校对有出入的页 → 待用户决定的提案；一致的不打扰用户。"""
    ocr_by_page = {page: text for page, text in ocr_pages}
    created = 0
    for page, checked in results:
        if checked.same:
            continue
        db.add(
            OcrProposal(
                paper_version_id=version.id,
                page=page,
                ocr_text=ocr_by_page.get(page, ""),
                model_text=checked.model_text,
                diff_json=checked.differences,
                status="pending",
                provider=checked.provider,
                model=checked.model,
                tokens_in=checked.tokens_in,
                tokens_out=checked.tokens_out,
                cost=checked.cost,
                latency_ms=checked.latency_ms,
            )
        )
        created += 1
    if created:
        await db.flush()
        log.info("version %s 生成 %s 条 OCR 校对提案", version.id, created)
    return created


async def _maybe_cross_check(job: Job, doc: ParsedDocument, pdf_path: Path) -> None:
    """阶段 2：双 OCR 交叉校核（见 docs/14）。

    两类触发：
    * 主解析本身就是 OCR（MinerU）→ RapidOCR 对图注小图二次识别，比对图号；
    * 主解析来自文字层且质量低/图注有噪声 → 再跑一套 MinerU OCR，用 OCR 图号校正文字层。

    失败只记日志、不改变主结果——宁可保留可疑标签交 review，也不能让整篇解析失败。
    """
    if (job.payload_json or {}).get("lite_ocr"):
        # 轻量 OCR 的结果由 ocr_cross_validate 做视觉校验；这里不再跑重 OCR/MinerU。
        return
    if not settings.ocr_cross_enabled or not rapidocr_available():
        return
    loop = asyncio.get_running_loop()
    try:
        primary_is_ocr = bool((job.payload_json or {}).get("ocr")) or backend_of(job) == "mineru"
        if primary_is_ocr:
            stats = await loop.run_in_executor(
                None, lambda: cross_check_figures(doc, pdf_path, dpi=settings.ocr_cross_dpi)
            )
            log.info(
                "job %s 双 OCR 校核（RapidOCR）：%s",
                job.id,
                {k: v for k, v in stats.items() if k != "samples"},
            )
            return

        # 文字层书：如果这个 job 之后会被按需升级到 Docling，让增强档 job 去做交叉，
        # 否则白跑一遍（结果的最终版本会被 Docling 覆盖）。
        will_escalate = (
            doc.text_layer
            and can_escalate((settings.escalate_backend or "").strip().lower(), backend_of(job))[0]
            and find_weakness(doc) is not None
        )
        if will_escalate:
            return
        quality = TextQuality(score=doc.text_quality if doc.text_quality is not None else 1.0)
        if not needs_text_layer_audit(doc, quality, min_score=settings.text_quality_min):
            return
        ocr_doc = await get_parser("mineru", render_images=False).parse(pdf_path)
        await loop.run_in_executor(
            None, lambda: cross_check_figures(ocr_doc, pdf_path, dpi=settings.ocr_cross_dpi)
        )
        merge_stats = await loop.run_in_executor(None, lambda: merge_text_layer(doc, ocr_doc))
        log.info("job %s 文字层→OCR 融合：%s", job.id, merge_stats)
    except Exception as exc:  # noqa: BLE001 — 校核失败不能拖垮解析
        log.warning("job %s 双 OCR 校核失败（保留原结果）：%s", job.id, exc)


async def handle(db: AsyncSession, job: Job) -> None:
    if job.kind != "parse" or not job.paper_version_id:
        raise ValueError(f"unsupported job kind: {job.kind}")

    version = await db.get(PaperVersion, job.paper_version_id)
    if version is None:
        raise ValueError(f"paper_version not found: {job.paper_version_id}")

    pdf_path = resolve_data_path(settings.storage_dir, version.file_path)
    if not pdf_path.is_file():
        # 库和文件是两份状态，对不上时要明确报出来，而不是抛一个看不懂的 PDF 解析异常
        raise FileNotFoundError(f"PDF 不在磁盘上：{version.file_path}")

    parser = get_parser(
        backend_of(job),
        mineru_url=settings.mineru_url,
        render_images=settings.render_figure_images,
        ocr=bool((job.payload_json or {}).get("ocr")),
        ocr_lang=settings.docling_ocr_lang,
    )
    job.progress = 0.1
    await db.commit()

    doc = await parser.parse(pdf_path)

    # 扫描页：先用轻量 OCR 补文字，用户无需额外操作；重后端 OCR 只在轻量路径
    # 不可用时才走原来的 _maybe_ocr（见 handle 末尾）。
    ocr_pages = await _maybe_lite_ocr(job, version, doc, pdf_path)
    if ocr_pages:
        job.progress = 0.45
        await db.commit()

    # 教材版面里的「扫码看视频/课件」二维码会被版面模型当成图片块，进库只会污染
    # 图号/检索/子图归组。解析完就地剔除（见 figure_filter.py 的判据与实测数据）。
    if settings.filter_qr_figures:
        dropped = drop_qr_figures(doc.figures)
        if dropped:
            log.info(
                "job %s 过滤二维码图 %d 张：%s",
                job.id,
                len(dropped),
                [(fig.page, [round(v) for v in fig.bbox]) for fig in dropped[:8]],
            )

    job.progress = 0.8
    await db.commit()

    # 图号校核的规则层：补 caption 里漏提的编号、给系列子图归组/继承父图号、
    # 记录文字层质量。规则层不重新识别图像，只处理已有文本（见 docs/14）。
    quality = assess_text_layer(doc)
    doc.text_quality = quality.score
    doc.text_quality_flags = quality.flags
    label_stats = apply_label_rules(doc)
    await _maybe_cross_check(job, doc, pdf_path)
    # 交叉校核可能改了标签/图注，重新归组与继承（group_key 需要按最终标签重算）
    label_stats = apply_label_rules(doc)
    log.info(
        "job %s 规则层：labeled=%s unlabeled=%s groups=%s repaired=%s "
        "inherited=%s text_quality=%.2f",
        job.id,
        label_stats.get("labeled"),
        label_stats.get("unlabeled"),
        label_stats.get("groups"),
        label_stats.get("repaired"),
        label_stats.get("inherited"),
        quality.score,
    )

    await persist(db, version, doc)
    # 本次解析成功，旧的 OCR 校对提案已过期；重新判断/生成。
    await db.execute(delete(OcrProposal).where(OcrProposal.paper_version_id == version.id))
    # OCR 页逐页让视觉模型校对；一致的直接采用，有出入才写提案交用户决定。
    cross_checked: list[tuple[int, ocr_cross_validate.OcrCrossCheck]] = []
    if ocr_pages:
        job.progress = 0.9
        await db.commit()
        cross_checked = await _cross_validate_lite_ocr(pdf_path, ocr_pages)
    proposal_count = 0
    if cross_checked:
        proposal_count = await _save_ocr_proposals(
            db, version, cross_checked, ocr_pages
        )
    await _maybe_escalate(db, job, version, doc)
    await _maybe_ocr(db, job, version, doc)
    job.progress = 1.0
    job.status = "done"
    job.finished_at = _now()
    await db.commit()
    if proposal_count:
        log.info("job %s OCR 校对完成：%s 条待用户决定", job.id, proposal_count)


async def recover_stale(db: AsyncSession) -> int:
    """把卡在 running 太久的 job 重新排队（进程被杀、容器重启）。"""
    cutoff = _now() - timedelta(minutes=STALE_RUNNING_MINUTES)
    stmt = select(Job).where(Job.status == "running", Job.started_at < cutoff)
    jobs = (await db.execute(stmt)).scalars().all()
    for job in jobs:
        job.status = "queued"
    if jobs:
        await db.commit()
    return len(jobs)


async def run_once() -> str | None:
    """处理一个 job。

    返回 ``None`` 表示队列空，``"done"`` / ``"failed"`` 表示这一轮的结局。
    """
    async with get_sessionmaker()() as db:
        job = await claim_job(db)
        if job is None:
            return None
        job_id = job.id  # 先取出来：rollback 会让 ORM 对象过期，之后访问 job.id 要再查库
        try:
            await handle(db, job)
            log.info("job %s done", job.id)
        except Exception as exc:  # noqa: BLE001 — worker 必须兜住所有异常
            await db.rollback()
            job = await db.get(Job, job_id) or job
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"[:2000]
            job.finished_at = _now()
            await db.commit()
            log.exception("job %s failed", job.id)
            return "failed"
        return "done"


async def main(poll_interval: float = 2.0, *, once: bool = False) -> int:
    """worker 主循环。返回失败的任务数（``--once`` 拿它当退出码）。

    每轮先消费解析 job，队列空时再领取一个 agent 任务（M3）；两者都空才轮询等待。
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    async with get_sessionmaker()() as db:
        n = await recover_stale(db)
        if n:
            log.info("requeued %d stale jobs", n)
        n = await recover_expired_tasks(db)
        if n:
            log.info("recovered %d expired agent tasks", n)
    log.info("worker 启动：backend=%s storage=%s", settings.parser_backend, settings.storage_dir)

    failed = 0
    while True:
        outcome = await run_once()
        if outcome is not None:
            if outcome == "failed":
                failed += 1
            if once:
                return failed
            continue
        task_outcome = await run_task_once()
        if task_outcome is not None:
            if task_outcome == "failed":
                failed += 1
            if once:
                return failed
            continue
        if once:
            log.info("队列空，退出")
            return failed
        await asyncio.sleep(poll_interval)


async def _serve(poll_interval: float, once: bool) -> int:
    """单事件循环内跑完并释放引擎（分两次 ``asyncio.run`` 会关掉连接池所在的循环）。"""
    try:
        return await main(poll_interval, once=once)
    finally:
        await dispose_engine()


def _cli() -> None:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m litbench.workers.run", description="消费 job 表（目前只有 parse）"
    )
    ap.add_argument("--once", action="store_true", help="最多处理一个 job 就退出（E2E / cron）")
    ap.add_argument("--poll", type=float, default=2.0, help="空队列时的轮询间隔（秒）")
    args = ap.parse_args()
    failures = asyncio.run(_serve(args.poll, args.once))
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    _cli()
