# SPDX-License-Identifier: AGPL-3.0-or-later
"""M3c 图定位框复核（多模态，2026-11-13）。

与 M3a/M3b 共用「提案 + 审计 + 任务」地基：只产出 ``figure_proposal(kind="figure_bbox")``，
人工采纳后才写 ``figure.bbox_override_json``（再重裁缩略图）；默认**不自动写**。

为什么要三张图：只看裁图看不出「框错在页面哪里」；只看整页又看不清框内是否缺了图例/坐标轴。
整页 + 红框标注页 + 目标裁图一起给模型，才能判断漏框/多框/框错位置。

坐标：模型输出归一化 [0,1]（左上原点、y 向下），服务端转成 pt 并做页内/最小尺寸/IoU/
caption 锚点四道校验；不合法直接 422，不落 proposal。
"""

from __future__ import annotations

import asyncio
import io
import math
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .auto_accept import auto_accept_proposal
from .config import settings
from .figure_review import proposal_to_dict
from .llm import (
    LLMClient,
    LLMError,
    complete_with_budget_retry,
    effective_config,
    get_client,
    vision_message_multi,
)
from .models import Block, Figure, FigureProposal, Paper, PaperVersion
from .paths import resolve_data_path
from .prompts import load_prompt, render

PROMPT_NAME = "bbox_review"
# caption 与框的最大垂直间隙（pt）：超过认为框和 caption 不在一张图上，自动接受要拦
CAPTION_GAP_MAX = 110.0
# 最小合法框边长（pt）：比这小基本是框错了
MIN_BOX_PT = 20.0
# 自动接受只认「与现有框 IoU 极低或极高」：低=明显框错，高=微调/一致
IOU_LOW = 0.2
IOU_HIGH = 0.9
_VERDICTS = {"ok", "wrong"}


class BboxReviewError(RuntimeError):
    """框复核的可读错误（图不存在 / 没图 / 模型输出不可用）。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iou(a: list[float], b: list[float]) -> float:
    """两个 [x0,y0,x1,y1] 的交并比（0–1）。"""
    if len(a) != 4 or len(b) != 4:
        return 0.0
    ax0, ay0, ax1, ay1 = (float(v) for v in a)
    bx0, by0, bx1, by1 = (float(v) for v in b)
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = area_a + area_b - inter
    return round(inter / union, 4) if union > 0 else 0.0


def normalize_bbox(raw, page_w: float, page_h: float) -> list[float]:
    """把模型给的框归一化到 pt 坐标（左上原点）。

    兼容两种写法：值都 ≤ 1.5 视为归一化 [0,1]；否则视为已经是 pt。越界会 clamp，
    但宽/高不足 ``MIN_BOX_PT`` 视为无效（说明模型没在真的描述图的位置）。
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise BboxReviewError("bbox 必须是 4 个数 [x0,y0,x1,y1]")
    try:
        values = [float(v) for v in raw]
    except (TypeError, ValueError) as exc:
        raise BboxReviewError("bbox 必须是数字") from exc
    if not all(math.isfinite(v) for v in values):
        raise BboxReviewError("bbox 含 NaN/Inf")
    if max(values) <= 1.5:
        x0, y0, x1, y1 = values
        x0, x1 = x0 * page_w, x1 * page_w
        y0, y1 = y0 * page_h, y1 * page_h
    else:
        x0, y0, x1, y1 = values
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    x0 = max(0.0, min(x0, page_w))
    x1 = max(0.0, min(x1, page_w))
    y0 = max(0.0, min(y0, page_h))
    y1 = max(0.0, min(y1, page_h))
    if x1 - x0 < MIN_BOX_PT or y1 - y0 < MIN_BOX_PT:
        raise BboxReviewError(
            f"bbox 太小或越界（{x1 - x0:.0f}×{y1 - y0:.0f}pt，最小 {MIN_BOX_PT:.0f}）"
        )
    return [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)]


def _label_digits(label_norm: str | None) -> str:
    return re.sub(r"\D", "", label_norm or "")


async def caption_anchor(session: AsyncSession, fig: Figure, bbox: list[float]) -> dict:
    """检查框与同页 caption 的相对位置：caption 应在框上/下方且水平有重叠。

    返回 ``{ok, block_id, gap, caption}``；没有 caption 块时 ``ok=False``（自动接受会拦）。
    """
    rows = (
        await session.execute(
            select(Block).where(
                Block.paper_version_id == fig.paper_version_id,
                Block.page == fig.page,
                Block.type == "caption",
            )
        )
    ).scalars().all()
    if not rows:
        return {"ok": False, "block_id": None, "gap": None, "caption": None}
    digits = _label_digits(fig.label_norm)
    best = None
    for block in rows:
        text = (block.text or "").strip()
        if not text:
            continue
        score = 0
        if digits and digits in re.sub(r"\D", "", text[:80]):
            score += 2
        if fig.caption and text[:20] and text[:20] in (fig.caption or ""):
            score += 1
        cap_bbox = list(block.bbox_json or [])
        if len(cap_bbox) != 4:
            continue
        cx0, cy0, cx1, cy1 = (float(v) for v in cap_bbox)
        bx0, by0, bx1, by1 = bbox
        if cy0 >= by1:
            gap = cy0 - by1
        elif cy1 <= by0:
            gap = by0 - cy1
        else:
            gap = 0.0
        if min(cx1, bx1) <= max(cx0, bx0):
            continue  # 水平不重叠：不是这张图的 caption
        candidate = (gap, -score, block, text)
        if best is None or candidate < best:
            best = candidate
    if best is None:
        return {"ok": False, "block_id": None, "gap": None, "caption": None}
    gap, _neg, block, text = best
    return {
        "ok": gap <= CAPTION_GAP_MAX,
        "block_id": block.id,
        "gap": round(gap, 1),
        "caption": text[:200],
    }


def _png_bytes(image, max_dim: int) -> bytes:
    """下采样并编码 PNG（控制 token / 请求体）。"""

    work = image
    if max(work.size) > max_dim:
        work = work.copy()
        work.thumbnail((max_dim, max_dim))
    buf = io.BytesIO()
    work.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _annotate_page(page_image, bbox: list[float], dpi: int):
    """在整页副本上把当前框画成红框（返回 PIL.Image）。"""
    from PIL import ImageDraw  # noqa: PLC0415

    image = page_image.copy()
    scale = dpi / 72.0
    x0, y0, x1, y1 = (float(v) * scale for v in bbox)
    draw = ImageDraw.Draw(image)
    width = max(2, round(image.width / 400))
    draw.rectangle([x0, y0, x1, y1], outline=(220, 40, 40), width=width)
    return image


def _validated(
    payload: dict | None,
    *,
    fig: Figure,
    page_w: float,
    page_h: float,
    anchor: dict,
) -> dict:
    """把模型输出收敛成可落库的 proposal；不合法抛 BboxReviewError。"""
    if not isinstance(payload, dict):
        raise BboxReviewError("模型输出不是 JSON 对象")
    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in _VERDICTS:
        raise BboxReviewError(f"verdict 只支持 ok / wrong，模型给了 {verdict!r}")
    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reason = re.sub(r"\s+", " ", str(payload.get("reason") or "")).strip()[:500]
    current = [float(v) for v in (fig.bbox_json or [])]
    bbox_pts: list[float] | None = None
    bbox_norm: list[float] | None = None
    iou = 0.0
    needs_change = False
    if verdict == "wrong":
        raw = payload.get("bbox") or payload.get("bbox_norm") or []
        bbox_pts = normalize_bbox(raw, page_w, page_h)
        if len(current) == 4:
            iou = _iou(current, bbox_pts)
        else:
            iou = 0.0
        bbox_norm = [
            round(bbox_pts[0] / page_w, 4),
            round(bbox_pts[1] / page_h, 4),
            round(bbox_pts[2] / page_w, 4),
            round(bbox_pts[3] / page_h, 4),
        ]
        # 模型说 wrong 但给的新框与旧框几乎一样：没有可执行改动，按 no_change 处理
        needs_change = iou < IOU_HIGH
    return {
        "verdict": verdict,
        "needs_change": needs_change,
        "bbox": bbox_norm,
        "bbox_points": bbox_pts,
        "current_bbox": current,
        "iou": iou,
        "page": fig.page,
        "page_size": [round(page_w, 1), round(page_h, 1)],
        "caption_anchor": anchor,
        "confidence": confidence,
        "reason": reason,
        "mode": "vision",
    }


async def _load_figure(session: AsyncSession, figure_id: str):
    row = (
        await session.execute(
            select(Figure, PaperVersion, Paper)
            .join(PaperVersion, Figure.paper_version_id == PaperVersion.id)
            .join(Paper, PaperVersion.paper_id == Paper.id)
            .where(Figure.id == figure_id, Figure.deleted_at.is_(None))
        )
    ).first()
    if row is None:
        raise BboxReviewError("figure not found")
    return row


def _render_pages(pdf_path, page_no: int, dpi: int):
    """线程池里执行：返回 (整页 PIL, 宽 pt, 高 pt)。"""
    try:
        import pypdfium2 as pdfium  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - 主镜像必装
        raise BboxReviewError("pypdfium2 未安装") from exc
    try:
        doc = pdfium.PdfDocument(str(pdf_path))
    except Exception as exc:  # noqa: BLE001
        raise BboxReviewError(f"打不开 PDF：{exc}") from exc
    try:
        if page_no < 1 or page_no > len(doc):
            raise BboxReviewError(f"页码越界：p{page_no}")
        page = doc[page_no - 1]
        try:
            width, height = page.get_size()
            image = page.render(scale=dpi / 72.0).to_pil().convert("RGB")
        finally:
            page.close()
        return image, float(width), float(height)
    finally:
        doc.close()


async def ai_review_bbox(
    session: AsyncSession,
    figure_id: str,
    *,
    provider: str | None = None,
    client: LLMClient | None = None,
) -> dict:
    """对一张图跑一次框复核，产出 pending ``figure_bbox`` proposal（不改图）。"""
    fig, version, paper = await _load_figure(session, figure_id)
    if len(fig.bbox_json or []) != 4:
        raise BboxReviewError("这张图没有有效 bbox，无法复核")
    try:
        pdf_path = resolve_data_path(settings.storage_dir, version.file_path)
    except (PermissionError, ValueError) as exc:
        raise BboxReviewError("PDF 路径非法") from exc
    if not pdf_path.is_file():
        raise BboxReviewError("PDF 不在磁盘上")

    dpi = max(72, int(settings.bbox_review_dpi))
    loop = asyncio.get_running_loop()
    page_image, page_w, page_h = await loop.run_in_executor(
        None, _render_pages, pdf_path, fig.page, dpi
    )
    current_bbox = [float(v) for v in fig.bbox_json]
    annotated = _annotate_page(page_image, current_bbox, dpi)
    scale = dpi / 72.0
    crop_box = (
        max(0, int(current_bbox[0] * scale)),
        max(0, int(current_bbox[1] * scale)),
        min(page_image.width, int(current_bbox[2] * scale)),
        min(page_image.height, int(current_bbox[3] * scale)),
    )
    crop_image = page_image.crop(crop_box) if crop_box[2] > crop_box[0] and crop_box[3] > crop_box[1] else None
    images = [
        (_png_bytes(page_image, settings.bbox_review_max_image_dim), "image/png", "图1：整页原图"),
        (
            _png_bytes(annotated, settings.bbox_review_max_image_dim),
            "image/png",
            "图2：标注页（红框 = 解析器当前框）",
        ),
    ]
    if crop_image is not None:
        images.append(
            (_png_bytes(crop_image, settings.bbox_review_max_image_dim), "image/png", "图3：当前框内裁图")
        )

    anchor = await caption_anchor(session, fig, current_bbox)
    neighbors = (
        await session.execute(
            select(Figure).where(
                Figure.paper_version_id == version.id,
                Figure.page == fig.page,
                Figure.deleted_at.is_(None),
                Figure.id != fig.id,
            )
        )
    ).scalars().all()
    neighbor_labels = ", ".join(
        f"{f.label_raw or '（无编号）'}" for f in neighbors[:8]
    ) or "（无）"

    prompt = load_prompt(PROMPT_NAME)
    prompt_text = render(
        prompt,
        paper_title=paper.title,
        page=fig.page,
        page_w=round(page_w, 1),
        page_h=round(page_h, 1),
        current_label=fig.label_raw or "（无编号）",
        current_caption=(fig.caption or "（无图注）")[:400],
        current_bbox=current_bbox,
        neighbor_labels=neighbor_labels,
    )
    messages = vision_message_multi(prompt_text, images)
    if client is None:
        source, _cfg = effective_config()
        if source == "mock":
            raise BboxReviewError("还没有配置模型：请先在右上角「设置」里填 base_url / model / API key")
    try:
        llm = client or get_client(provider)
    except LLMError as exc:
        raise BboxReviewError(str(exc)) from exc
    try:
        result = await complete_with_budget_retry(
            llm,
            messages,
            json_schema={"type": "object"},
            meta={
                "task": "bbox_review",
                "figure_id": fig.id,
                "page": fig.page,
                "images": len(images),
            },
            max_tokens=settings.bbox_review_max_tokens,
        )
    except LLMError as exc:
        raise BboxReviewError(str(exc)) from exc

    proposed = _validated(
        result.parsed, fig=fig, page_w=page_w, page_h=page_h, anchor=anchor
    )
    # caption 锚点按「建议框」再算一次：自动接受要判断的是新框是否仍贴着同一张图的 caption；
    # 用旧框算会把「旧框本来就错」的图误拦（旧框离 caption 很远 → anchor=False）。
    if proposed.get("bbox_points"):
        proposed["caption_anchor"] = await caption_anchor(
            session, fig, proposed["bbox_points"]
        )

    # 同一张图旧 pending 框建议作废（UI 只看最新一条）
    old = (
        await session.execute(
            select(FigureProposal).where(
                FigureProposal.figure_id == fig.id,
                FigureProposal.kind == "figure_bbox",
                FigureProposal.status == "pending",
            )
        )
    ).scalars().all()
    for row in old:
        row.status = "superseded"

    proposal = FigureProposal(
        figure_id=fig.id,
        paper_version_id=version.id,
        kind="figure_bbox",
        proposed_json=proposed,
        evidence_json=[
            {"type": "page_image", "value": f"dpi={dpi}"},
            {"type": "annotated_page", "value": f"dpi={dpi}"},
            {"type": "caption_block", "value": anchor.get("block_id")},
            {"type": "iou", "value": proposed["iou"]},
        ],
        confidence=float(proposed.get("confidence") or 0.0),
        status="pending" if proposed.get("needs_change") else "no_change",
        provider=result.provider,
        model=result.model,
        prompt_version=prompt.version,
        mode="vision",
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cost=result.cost,
        latency_ms=result.latency_ms,
    )
    session.add(proposal)
    await session.commit()
    await session.refresh(proposal)

    if proposal.status == "pending":
        await auto_accept_proposal(session, proposal, fig)
        await session.refresh(proposal)
        await session.refresh(fig)
    return proposal_to_dict(proposal, fig=fig, paper=paper)


__all__ = [
    "BboxReviewError",
    "CAPTION_GAP_MAX",
    "IOU_HIGH",
    "IOU_LOW",
    "MIN_BOX_PT",
    "PROMPT_NAME",
    "ai_review_bbox",
    "caption_anchor",
    "normalize_bbox",
]
