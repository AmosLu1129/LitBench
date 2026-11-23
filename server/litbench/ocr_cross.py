# SPDX-License-Identifier: AGPL-3.0-or-later
r"""两套 OCR 的交叉校核（第二套：RapidOCR / PP-OCRv4，Apache-2.0）。

为什么要有它：MinerU flash 只跑一遍，识别错了没人知道（例如把「图 4.5.1」读成
「图 4.e.1」）。用户的要求是——**文字层不可信时，宁可相信两套 OCR 的交叉结果**。
所以这里做两件事：

1. ``cross_check_figures``：对主解析结果里每张图的**图注区域小图**（MinerU 给出的
   ``caption_bbox``），用第二套 OCR（RapidOCR PP-OCRv4，与 MinerU 的 PP-OCRv6
   是不同版本/权重）再识别一次，比对图号：
   - 两套一致 → ``ocr_consensus``（conf 0.95）；
   - 只有第二套读到 → 补上（``ocr``）；
   - 两套都有但不一致 → ``ocr_cross_conflict``，降置信，进 review 队列。
2. ``merge_text_layer``：文字层书（书 01/02 的出版社文字层本身乱码）与 OCR 结果
   按页/bbox 匹配，**文字层质量低时以 OCR 图号为准**，把文字层的错号替换掉。

许可证：RapidOCR 是 Apache-2.0、onnxruntime 是 MIT，可以直接进主镜像（与 AGPL 的
MinerU 不同；MinerU 仍然只走独立子进程）。模型随 rapidocr 包分发，不在本仓库。
"""

from __future__ import annotations

import re
import threading
import unicodedata
from pathlib import Path

from .labeling import (
    CONF_INFER,
    CONF_REPAIRED,
    CONF_REVIEW,
    _BROKEN_LABEL_RE,
    _BROKEN_LETTER_RE,
    _needs_repair,
    canonicalize_label,
)
from .labels import LABEL_RE, normalize_label

DPI = 200  # 图注小图识别，200dpi 足够；再高只是徒增耗时
_engine = None
_engine_lock = threading.Lock()


def rapidocr_available() -> bool:
    try:
        import rapidocr  # noqa: F401
    except ImportError:  # pragma: no cover — 主镜像可选依赖
        return False
    return True


def _get_engine():  # pragma: no cover — 真跑模型
    """懒加载 RapidOCR，固定用 PP-OCRv4 mobile（与 MinerU 的 v6 不同版本）。"""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from rapidocr import ModelType, OCRVersion, RapidOCR

                _engine = RapidOCR(
                    params={
                        "Det.ocr_version": OCRVersion.PPOCRV4,
                        "Rec.ocr_version": OCRVersion.PPOCRV4,
                        "Det.model_type": ModelType.MOBILE,
                        "Rec.model_type": ModelType.MOBILE,
                        "Global.log_level": "error",
                    }
                )
    return _engine


# ---------------------------------------------------------------------------
# 图注区域识别
# ---------------------------------------------------------------------------
def _render_page(pdf_path: Path, page_no: int, dpi: int):
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        if page_no < 1 or page_no > len(doc):
            return None
        page = doc[page_no - 1]
        try:
            return page.render(scale=dpi / 72.0).to_pil().convert("RGB")
        finally:
            page.close()
    finally:
        doc.close()


def _ocr_image(image) -> str:  # pragma: no cover — 真跑模型
    import numpy as np

    engine = _get_engine()
    out = engine(np.array(image))
    texts = getattr(out, "txts", None) or ()
    return "".join(str(t) for t in texts)


def read_caption(pdf_path: Path, page_no: int, bbox: list[float], dpi: int = DPI) -> str:
    """裁图注区域小图并跑第二套 OCR；失败返回空串（校核失败不该让整篇解析失败）。"""
    try:
        image = _render_page(Path(pdf_path), int(page_no), dpi)
        if image is None:
            return ""
        scale = dpi / 72.0
        x0 = max(int((float(bbox[0]) - 2) * scale), 0)
        y0 = max(int((float(bbox[1]) - 2) * scale), 0)
        x1 = min(int((float(bbox[2]) + 2) * scale), image.width)
        y1 = min(int((float(bbox[3]) + 2) * scale), image.height)
        if x1 - x0 < 8 or y1 - y0 < 8:
            return ""
        return _ocr_image(image.crop((x0, y0, x1, y1)))
    except Exception:  # noqa: BLE001 — 第二套 OCR 失败不影响主流程
        return ""


# ---------------------------------------------------------------------------
# 图号集合 / 一致性
# ---------------------------------------------------------------------------
def labels_in_text(text: str) -> set[str]:
    """从一段文本里抽所有图号（含被 OCR 识别坏的形态）。"""
    if not text:
        return set()
    found: set[str] = set()
    noisy = (
        _needs_repair(text)
        or _BROKEN_LETTER_RE.search(text) is not None
        or _BROKEN_LABEL_RE.search(text) is not None
    )
    if noisy:
        cand = canonicalize_label(text)
        if cand.label_norm:
            return {cand.label_norm}
    for m in LABEL_RE.finditer(text):
        norm = normalize_label(m.group(0))
        if norm and norm.startswith("fig"):
            found.add(norm)
    return found


# 原始编号片段（保留字母段，用于判断「两套 OCR 读到的字面量是否一致」）。
# 图 4.e.1 这种：如果两套都读成 e，那很可能是原文就这么印的（习题图），不能硬改成 5。
_SNIPPET_RE = re.compile(r"(图|表)\s*\d{1,3}(?:\s*[.\-–—]\s*[0-9A-Za-z]{1,3})*")
_DIGIT_SEGMENT_RE = re.compile(r"^[0-9]+$")


def label_snippet(text: str) -> str | None:
    m = _SNIPPET_RE.search(text or "")
    if not m:
        return None
    return re.sub(r"\s+", "", m.group(0))


def _snippet_key(snippet: str | None) -> str | None:
    if not snippet:
        return None
    s = unicodedata.normalize("NFKC", snippet)
    return re.sub(r"[–—]", "-", s)


def _has_letter_segment(snippet: str) -> bool:
    """编号里混了字母段（4.e.1 这种），没法安全归一。"""
    body = re.sub(r"^(图|表)", "", snippet)
    for seg in re.split(r"[.\-]", body):
        if seg and not _DIGIT_SEGMENT_RE.match(seg):
            return True
    return False


def _should_check(fig) -> bool:
    """只校核「值得校核」的图：没有编号的、编号带字母段的、已经低置信/可疑的。

    已经由主文本读出、能归一、置信度高的图不重复烧 OCR——交叉校核的目的是
    找「漏的/错的」，不是把每张图都重跑一遍。
    """
    if not (fig.caption and fig.caption_bbox):
        return False
    if fig.label_source in ("ocr_consensus", "ocr", "ocr_cross_conflict", "ocr_cross_unresolved"):
        return False
    if fig.label_raw and (fig.label_confidence or 0) >= 0.9:
        snippet = label_snippet(fig.caption or "")
        # 高置信且是正常数字编号 → 不校核；带字母段（图 4.e.1 这种）仍然要校核
        if snippet and not _has_letter_segment(snippet):
            return False
    if not fig.label_raw:
        return True
    snippet = label_snippet(fig.caption or "")
    return snippet is None or _has_letter_segment(snippet)


def cross_check_figures(doc, pdf_path: Path, *, dpi: int = DPI) -> dict:
    """对主解析结果的每张图：用第二套 OCR 重读图注，和主文本比对图号。

    就地修改 figure 的 ``label_source / label_confidence``（不新增一列候选，冲突记在
    ``doc.text_quality_flags["ocr_cross"]``，阶段 3 的 review UI 再消费）。
    """
    stats = {
        "checked": 0,
        "consensus": 0,
        "rescued": 0,
        "conflict": 0,
        "unresolved": 0,
        "no_second_label": 0,
        "samples": [],
    }
    page_image = None
    current_page = None
    for fig in sorted((f for f in doc.figures if _should_check(f)), key=lambda f: (f.page, f.bbox[1])):
        if current_page != fig.page:
            page_image = _render_page(Path(pdf_path), fig.page, dpi)
            current_page = fig.page
        if page_image is None:
            continue
        second = _read_on_image(page_image, fig.caption_bbox, dpi)
        stats["checked"] += 1
        main_snip = label_snippet(fig.caption)
        second_snip = label_snippet(second)
        main_cand = canonicalize_label(fig.caption) if main_snip else None
        second_cand = canonicalize_label(second) if second_snip else None

        if main_snip and second_snip:
            if _snippet_key(main_snip) == _snippet_key(second_snip):
                if _has_letter_segment(main_snip) or _has_letter_segment(second_snip):
                    # 两套都读成「图 4.e.1」：字面量一致但无法安全归一（可能是习题图编号），
                    # 保留原文、标待确认，不硬改成 4.5.1。
                    fig.label_raw = main_snip
                    fig.label_source = "ocr_cross_unresolved"
                    fig.label_confidence = CONF_INFER
                    stats["unresolved"] += 1
                elif main_cand and main_cand.label_norm:
                    fig.label_source = "ocr_consensus"
                    fig.label_confidence = CONF_REVIEW
                    stats["consensus"] += 1
                else:
                    fig.label_raw = main_snip
                    fig.label_source = "ocr_cross_unresolved"
                    fig.label_confidence = CONF_INFER
                    stats["unresolved"] += 1
                continue
            # 两套字面量不同：只有主文本本身给不出可归一编号时，才采信第二套
            if (
                (not main_cand or not main_cand.label_norm)
                and second_cand
                and second_cand.label_norm
            ):
                fig.label_raw = second_cand.label_raw or fig.label_raw
                fig.label_source = "ocr"
                fig.label_confidence = CONF_REPAIRED
                stats["rescued"] += 1
                continue
            stats["conflict"] += 1
            fig.label_source = "ocr_cross_conflict"
            fig.label_confidence = CONF_INFER
            if len(stats["samples"]) < 10:
                stats["samples"].append(
                    {
                        "page": fig.page,
                        "main": main_snip,
                        "second": second_snip,
                        "second_text": second[:80],
                    }
                )
            continue

        if second_snip and not main_snip:
            if second_cand and second_cand.label_norm:
                fig.label_raw = second_cand.label_raw or fig.label_raw
                fig.label_source = "ocr"
                fig.label_confidence = CONF_REPAIRED
                stats["rescued"] += 1
            continue
        stats["no_second_label"] += 1
    doc.text_quality_flags["ocr_cross"] = stats
    return stats


def _read_on_image(image, bbox: list[float], dpi: int) -> str:  # pragma: no cover — 真跑模型
    try:
        scale = dpi / 72.0
        x0 = max(int((float(bbox[0]) - 2) * scale), 0)
        y0 = max(int((float(bbox[1]) - 2) * scale), 0)
        x1 = min(int((float(bbox[2]) + 2) * scale), image.width)
        y1 = min(int((float(bbox[3]) + 2) * scale), image.height)
        if x1 - x0 < 8 or y1 - y0 < 8:
            return ""
        return _ocr_image(image.crop((x0, y0, x1, y1)))
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# 文字层 vs OCR 融合
# ---------------------------------------------------------------------------
def _iou(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != 4 or len(b) != 4:
        return 0.0
    x0 = max(a[0], b[0])
    y0 = max(a[1], b[1])
    x1 = min(a[2], b[2])
    y1 = min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    area_a = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1e-6, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / (area_a + area_b - inter)


def _center_dist(a: list[float], b: list[float]) -> float:
    ax, ay = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2
    bx, by = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def merge_text_layer(base_doc, ocr_doc, *, min_iou: float = 0.2) -> dict:
    """文字层结果与 OCR 结果按图块匹配，低质量文字层的错号用 OCR 替换。

    只在 bbox 对得上（IoU ≥ min_iou）或同页唯一候选时替换，避免把 A 图的号
    贴到 B 图上。替换后 ``label_source="ocr"``（阶段 3 的 review 可再确认）。
    """
    stats = {"matched": 0, "relabeled": 0, "consensus": 0, "unmatched": 0}
    by_page: dict[int, list] = {}
    for fig in ocr_doc.figures:
        by_page.setdefault(fig.page, []).append(fig)
    for fig in base_doc.figures:
        candidates = by_page.get(fig.page) or []
        if not candidates:
            stats["unmatched"] += 1
            continue
        best = max(candidates, key=lambda o: _iou(fig.bbox, o.bbox))
        iou = _iou(fig.bbox, best.bbox)
        if iou < min_iou:
            nearest = min(candidates, key=lambda o: _center_dist(fig.bbox, o.bbox))
            if _center_dist(fig.bbox, nearest.bbox) > 120:
                stats["unmatched"] += 1
                continue
            best = nearest
        stats["matched"] += 1
        ocr_cand = canonicalize_label((best.caption or "") + "\n" + (best.label_raw or ""))
        base_cand = canonicalize_label((fig.caption or "") + "\n" + (fig.label_raw or ""))
        if not ocr_cand.label_norm:
            continue
        if base_cand.label_norm == ocr_cand.label_norm:
            fig.label_source = "ocr_consensus"
            fig.label_confidence = CONF_REVIEW
            stats["consensus"] += 1
            continue
        # 文字层错号/无号：以 OCR 为准（用户要求：文字层质量低时宁信 OCR）
        fig.label_raw = ocr_cand.label_raw or fig.label_raw
        fig.label_source = "ocr"
        fig.label_confidence = CONF_REPAIRED
        if not (fig.caption or "").strip() and best.caption:
            fig.caption = best.caption
            fig.caption_bbox = best.caption_bbox
        stats["relabeled"] += 1
    base_doc.text_quality_flags["ocr_merge"] = stats
    return stats


__all__ = [
    "cross_check_figures",
    "labels_in_text",
    "merge_text_layer",
    "rapidocr_available",
    "read_caption",
]
