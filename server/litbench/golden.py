# SPDX-License-Identifier: AGPL-3.0-or-later
"""Golden set：把人工/agent 的复核结果变成可回放的回归资产。

问题很具体：解析器每改一次（阈值、caption 合并、按需升级），**没人知道是变好还是变坏**。
68% 这个数是一次性报告里的快照，不是可回归的指标。这里补上闭环：

1. ``export_golden`` 把已复核的图连同样本出处导出成自包含 JSON；
2. ``replay`` 用指定后端重新解析同一批 PDF，逐条算「还锚得住吗 / 框还对得上吗」；
3. ``summarize`` 给出 recall（人工说该有箱子的图，现在还有多少对得上）这类可比数字。

判分规则（刻意保守，宁可报 regression 也别抹平）：

* **期望有框**的条目（``ok``，或 ``wrong`` 但人工给了修正框）：
  新解析必须仍然锚定同一个图号，且 bbox IoU ≥ 阈值 → ``pass``；
  否则锚不到 ``no_box``、对不上 ``regression``。
* **期望没框**的条目（``wrong`` 且没给修正框）：新解析不该再落回那个错框。
  仍落在同一框 → ``still_wrong``；换框或干脆没有这个图号 → ``fixed``。

``source_path`` 存的是相对 storage 根的路径（见 ``paths.to_storage_relative``），
所以同一份 golden 换机器、换挂载点都能回放。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .adapters import ParsedDocument, get_parser
from .config import settings
from .labels import normalize_label
from .models import AppUser, Figure, Paper, PaperVersion
from .paths import resolve_data_path
from .team import visible_paper_condition

SCHEMA = "litbench.golden/1"
BBOX_SCHEMA = "litbench.golden.bboxes/1"
REVIEWED = ("ok", "wrong")


def bbox_iou(a: list[float] | None, b: list[float] | None) -> float:
    """两个 [x0,y0,x1,y1] 的交并比。任一为空/非法返回 0。"""
    if not a or not b or len(a) != 4 or len(b) != 4:
        return 0.0
    ax0, ay0, ax1, ay1 = (float(v) for v in a)
    bx0, by0, bx1, by1 = (float(v) for v in b)
    inter_w = min(ax1, bx1) - max(ax0, bx0)
    inter_h = min(ay1, by1) - max(ay0, by0)
    if inter_w <= 0 or inter_h <= 0:
        return 0.0
    inter = inter_w * inter_h
    area_a = max(ax1 - ax0, 0.0) * max(ay1 - ay0, 0.0)
    area_b = max(bx1 - bx0, 0.0) * max(by1 - by0, 0.0)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _effective_bbox(fig: Figure) -> list[float]:
    """生效框：人工修正过就用 override（overlay 也是这么显示的）。"""
    raw = fig.bbox_override_json or fig.bbox_json or []
    return [float(v) for v in raw]


def _item_of(fig: Figure, ver: PaperVersion, paper: Paper) -> dict:
    return {
        "item_id": fig.id,
        "paper_id": paper.id,
        "paper_title": paper.title,
        "year": paper.year,
        "doi": paper.doi,
        "arxiv_id": paper.arxiv_id,
        "version_id": ver.id,
        "version_kind": ver.kind,
        "parser": ver.parser,
        "parser_version": ver.parser_version,
        # 相对 storage 根：golden 换机器也能回放
        "source_path": ver.file_path,
        "cite_key": fig.cite_key,
        "label_raw": fig.label_raw,
        "label_norm": fig.label_norm,
        "page": fig.page,
        "bbox": _effective_bbox(fig),
        "bbox_parsed": [float(v) for v in (fig.bbox_json or [])],
        "bbox_override": [float(v) for v in (fig.bbox_override_json or [])] or None,
        "caption": fig.caption or "",
        "figure_type": fig.figure_type,
        "review_status": fig.review_status,
        "review_note": fig.review_note,
        "reviewed_by": fig.reviewed_by,
        "reviewed_at": fig.reviewed_at.isoformat() if fig.reviewed_at else None,
    }


async def export_golden(
    session: AsyncSession, *, include_pending: bool = False, user: AppUser | None = None
) -> dict:
    """导出 golden set。默认只含已复核（ok/wrong）的条目。

    认证打开时只导出当前用户可见文献的图：golden 里带标题/DOI/图注/裁剪路径，
    不能把别人私有库的标注当公共数据集发出去（MCP 内部令牌走 system = 全库）。
    """
    rows = await _figure_rows(session, include_pending=include_pending, user=user)
    items = [_item_of(fig, ver, paper) for fig, ver, paper in rows]
    return {
        "schema": SCHEMA,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "include_pending": include_pending,
        "item_count": len(items),
        "items": items,
    }


async def _figure_rows(
    session: AsyncSession, *, include_pending: bool, user: AppUser | None = None
) -> list[tuple[Figure, PaperVersion, Paper]]:
    statuses = (*REVIEWED, "pending") if include_pending else REVIEWED
    stmt = (
        select(Figure, PaperVersion, Paper)
        .join(PaperVersion, Figure.paper_version_id == PaperVersion.id)
        .join(Paper, PaperVersion.paper_id == Paper.id)
        .where(Figure.review_status.in_(statuses))
        # 软删除的图不进 golden：用户已经说这张图不该存在，拿它当正样本会污染回放。
        .where(Figure.deleted_at.is_(None))
        .where(visible_paper_condition(user))
        .order_by(Paper.title, Figure.page, Figure.label_num, Figure.cite_key)
    )
    return list((await session.execute(stmt)).all())


def _bbox_item_of(fig: Figure, ver: PaperVersion, paper: Paper) -> dict | None:
    """bbox 标注集的一条：只留「图框回归/校准」需要的字段。

    没有框的图不导出（bbox 集里一条空框没有校准意义），由调用方计入 skipped。
    """
    bbox = _effective_bbox(fig)
    if len(bbox) != 4:
        return None
    override = [float(v) for v in (fig.bbox_override_json or [])]
    return {
        "item_id": fig.id,
        "paper_id": paper.id,
        "paper_title": paper.title,
        "version_id": ver.id,
        "source_path": ver.file_path,
        "page": fig.page,
        "bbox": bbox,
        "bbox_source": "override" if override else "parsed",
        "bbox_parsed": [float(v) for v in (fig.bbox_json or [])],
        "bbox_override": override or None,
        "label_raw": fig.label_raw,
        "label_norm": fig.label_norm,
        "cite_key": fig.cite_key,
        "image_path": fig.image_path,
        "review_status": fig.review_status,
        "reviewed_by": fig.reviewed_by,
    }


async def export_golden_bboxes(
    session: AsyncSession, *, include_pending: bool = False, user: AppUser | None = None
) -> dict:
    """只导出已复核图框（M9.4）：给框检测/阈值校准用的轻量标注集。

    与完整 golden 的区别：不带 caption/摘要/回放元数据，保留 source_path + page +
    生效 bbox（人工 override 优先）和裁剪图路径；消费者可以自己渲染页面算 IoU。
    """
    rows = await _figure_rows(session, include_pending=include_pending, user=user)
    items: list[dict] = []
    skipped = 0
    for fig, ver, paper in rows:
        item = _bbox_item_of(fig, ver, paper)
        if item is None:
            skipped += 1
            continue
        items.append(item)
    return {
        "schema": BBOX_SCHEMA,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "include_pending": include_pending,
        "item_count": len(items),
        "skipped_no_bbox": skipped,
        "items": items,
    }


def _matching_figures(doc: ParsedDocument, label_norm: str | None) -> list[tuple[list[float], str]]:
    if not label_norm:
        return []
    found: list[tuple[list[float], str]] = []
    for fig in doc.figures:
        if not fig.label_raw:
            continue
        norm = normalize_label(fig.label_raw)
        if norm == label_norm:
            found.append(([float(v) for v in fig.bbox], norm))
    return found


def expects_box(item: dict) -> bool:
    """这条 golden 是不是「应该给出这个框」。

    * 复核为 ``ok`` → 是；
    * 复核为 ``wrong`` **但给了修正框** → 也是。人工画了正确的框，这就是最值钱的正样本，
      不该退化成「只要不是那个错框就算过」。
    * 复核为 ``wrong`` 且没给框 → 不是（理想情况是这里根本不该出图）。
    """
    return item.get("review_status") == "ok" or bool(item.get("bbox_override"))


def score_item(
    doc: ParsedDocument,
    item: dict,
    *,
    iou_threshold: float = 0.5,
    corrected_threshold: float = 0.9,
) -> dict:
    """把一条 golden 判成 pass / regression / fixed / still_wrong。

    ``corrected_threshold`` 专门给「人工画了修正框」的条目：人工连框都改了，
    说明 20pt 的偏移对他重要，这时候还按 0.5 判 pass 就是在骗自己。
    """
    matches = _matching_figures(doc, item.get("label_norm"))
    golden_box = item.get("bbox") or []
    best_iou = max((bbox_iou(box, golden_box) for box, _ in matches), default=0.0)
    expects = expects_box(item)
    corrected = item.get("review_status") == "wrong" and bool(item.get("bbox_override"))
    required = corrected_threshold if corrected else iou_threshold

    if expects:
        if not matches:
            verdict = "no_box"
        elif best_iou >= required:
            verdict = "pass"
        else:
            verdict = "regression"
    else:
        if not matches:
            verdict = "fixed"  # 不再给一个错的框，算修好
        elif best_iou >= iou_threshold:
            verdict = "still_wrong"
        else:
            verdict = "fixed"

    return {
        "item_id": item.get("item_id"),
        "cite_key": item.get("cite_key"),
        "paper_title": item.get("paper_title"),
        "label_norm": item.get("label_norm"),
        "review_status": item.get("review_status"),
        "corrected": corrected,
        "expects_box": expects,
        "iou_threshold": required,
        "anchored": bool(matches),
        "iou": round(best_iou, 4),
        "verdict": verdict,
    }


def summarize(results: list[dict]) -> dict:
    """汇总成几个能横向比较的数。"""
    errors = [r for r in results if r["verdict"] == "error"]
    positive = [r for r in results if r.get("expects_box") and r["verdict"] != "error"]
    negative = [r for r in results if not r.get("expects_box") and r["verdict"] != "error"]

    passed = sum(1 for r in positive if r["verdict"] == "pass")
    still_wrong = sum(1 for r in negative if r["verdict"] == "still_wrong")
    fixed = sum(1 for r in negative if r["verdict"] in ("fixed", "no_box"))
    ious = [r["iou"] for r in positive]

    return {
        "total": len(results),
        "errors": len(errors),
        # 「人工说这里该有这张图」的条目，现在还有多少对得上——最该盯住的数
        "positive_total": len(positive),
        "positive_passed": passed,
        "recall": round(passed / len(positive), 4) if positive else None,
        # 人工说「这里本来就不该有框」的条目
        "negative_total": len(negative),
        "negative_still_wrong": still_wrong,
        "negative_fixed": fixed,
        "mean_iou_positive": round(sum(ious) / len(ious), 4) if ious else None,
    }


async def replay(
    golden: dict,
    *,
    backend: str | None = None,
    iou_threshold: float = 0.5,
    corrected_threshold: float = 0.9,
    parser_factory=None,  # noqa: ANN001 — 测试注入用
    storage_root: Path | None = None,
    limit: int | None = None,
) -> dict:
    """按 golden 重新解析并判分。

    ``backend`` 为空时用每条自己的 ``parser``（同后端重放 = 幂等性检查）；
    显式指定（如 ``pdfium``）就是「把现役结果拿回基线解析器上重放」，
    能直接量出按需升级带来的收益。
    """
    root = storage_root or settings.storage_dir
    factory = parser_factory or (lambda name: get_parser(name, render_images=False))
    items = golden.get("items") or []
    if limit:
        items = items[:limit]

    cache: dict[tuple[str, str], ParsedDocument | Exception] = {}
    results: list[dict] = []
    for item in items:
        name = (backend or item.get("parser") or "pdfium").strip().lower()
        key = (name, str(item.get("source_path") or ""))
        doc = cache.get(key)
        if doc is None:
            try:
                pdf_path = resolve_data_path(root, str(item.get("source_path") or ""))
                doc = await factory(name).parse(pdf_path)
            except Exception as exc:  # noqa: BLE001 — 一条坏样本不该毁掉整份报告
                doc = exc
            cache[key] = doc

        if isinstance(doc, Exception):
            results.append(
                {
                    "item_id": item.get("item_id"),
                    "cite_key": item.get("cite_key"),
                    "paper_title": item.get("paper_title"),
                    "label_norm": item.get("label_norm"),
                    "review_status": item.get("review_status"),
                    "anchored": False,
                    "iou": 0.0,
                    "verdict": "error",
                    "error": f"{type(doc).__name__}: {doc}",
                }
            )
            continue
        results.append(
            score_item(
                doc, item, iou_threshold=iou_threshold, corrected_threshold=corrected_threshold
            )
        )

    return {
        "schema": SCHEMA,
        "replayed_at": datetime.now(timezone.utc).isoformat(),
        "backend": backend or "（按条目原后端）",
        "iou_threshold": iou_threshold,
        "corrected_threshold": corrected_threshold,
        "summary": summarize(results),
        "results": results,
    }
