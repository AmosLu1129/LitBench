# SPDX-License-Identifier: AGPL-3.0-or-later
"""轻量 OCR（RapidOCR / PP-OCR mobile）：只补扫描页的文字层，不引入重型后端。

定位：
* 有文字层的 PDF 继续用 pdfium（零模型、秒级）；
* 没有文字层的页才走这里：整页渲染 → RapidOCR → 文字行 + bbox → 合并进
  ``ParsedDocument``，后续规则层/入库/检索完全复用现有链路；
* 表格结构不在这里硬做：先用 OCR 文字 + 现有规则，复杂页面交给可选的视觉大模型
  交叉校验（见 ``ocr_cross_validate.py``）。

模型固定 PP-OCRv4 mobile（与 MinerU 的 PP-OCRv6 不同版本），Apache-2.0；
``rapidocr`` 包本身带 det/rec/cls 权重，不额外下载。
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .adapters.base import ParsedBlock, ParsedDocument

log = logging.getLogger("litbench.lite_ocr")

MIN_CONFIDENCE = 0.35
MAX_LONG_SIDE = 1800


@dataclass(slots=True)
class LiteOcrLine:
    text: str
    bbox: list[float]  # pt, 左上原点
    confidence: float


@dataclass(slots=True)
class LiteOcrPage:
    page: int
    width: float
    height: float
    lines: list[LiteOcrLine] = field(default_factory=list)
    engine: str = "liteocr"

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines if line.text.strip())


def lite_ocr_available() -> bool:
    """RapidOCR + pypdfium2 + numpy/Pillow 都可用才启用。"""
    try:
        import numpy  # noqa: F401
        import pypdfium2  # noqa: F401
        import rapidocr  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    return True


def _render_page(pdf_path: Path, page_no: int, dpi: int):
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        if page_no < 1 or page_no > len(doc):
            return None, None
        page = doc[page_no - 1]
        try:
            width, height = page.get_size()
            image = page.render(scale=dpi / 72.0).to_pil().convert("RGB")
            return image, (float(width), float(height))
        finally:
            page.close()
    finally:
        doc.close()


def _box_to_pt(box, dpi: int) -> list[float] | None:
    """RapidOCR 的 box 是 [[x,y] * 4] 像素坐标；转 pt（左上原点）。"""
    try:
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
    except (TypeError, ValueError, IndexError):
        return None
    scale = 72.0 / float(dpi)
    return [min(xs) * scale, min(ys) * scale, max(xs) * scale, max(ys) * scale]


def ocr_page(pdf_path: Path, page_no: int, *, dpi: int = 200) -> LiteOcrPage | None:
    """识别单页；失败返回 None（调用方保留原解析结果，不拖垮整篇）。"""
    try:
        image, size = _render_page(Path(pdf_path), int(page_no), int(dpi))
        if image is None or size is None:
            return None
        import numpy as np

        from .ocr_cross import _get_engine  # 复用同一份懒加载引擎

        result = _get_engine()(np.asarray(image))
        lines: list[LiteOcrLine] = []
        boxes = getattr(result, "boxes", None)
        txts = getattr(result, "txts", None) or ()
        scores = getattr(result, "scores", None) or ()
        for idx, text in enumerate(txts):
            value = str(text or "").strip()
            if not value:
                continue
            confidence = float(scores[idx]) if idx < len(scores) else 0.0
            if confidence and confidence < MIN_CONFIDENCE:
                continue
            bbox = _box_to_pt(boxes[idx], int(dpi)) if boxes is not None else None
            if bbox is None:
                continue
            lines.append(LiteOcrLine(text=value, bbox=bbox, confidence=confidence))
        # 按阅读顺序：先纵后横；RapidOCR 通常已按检测顺序返回，这里再稳一遍
        lines.sort(key=lambda line: (round(line.bbox[1], 1), line.bbox[0]))
        return LiteOcrPage(
            page=int(page_no),
            width=size[0],
            height=size[1],
            lines=lines,
        )
    except Exception as exc:  # noqa: BLE001 — OCR 失败不能拖垮整篇解析
        log.warning("轻量 OCR 第 %s 页失败：%s", page_no, exc)
        return None


def pages_with_text(doc: ParsedDocument) -> set[int]:
    return {
        block.page
        for block in doc.blocks
        if block.text
        and block.text.strip()
        and block.type in ("text", "title", "section", "caption")
    }


def ocr_missing_pages(
    pdf_path: Path,
    doc: ParsedDocument,
    *,
    dpi: int = 200,
    max_pages: int = 80,
) -> list[LiteOcrPage]:
    """只 OCR 没有文字块的页；最多 max_pages 页，避免一本扫描书无限烧 CPU。"""
    if not pages_with_text(doc) and doc.page_count <= max_pages:
        targets = list(range(1, doc.page_count + 1))
    else:
        have = pages_with_text(doc)
        targets = [p for p in range(1, doc.page_count + 1) if p not in have][:max_pages]
    if not targets:
        return []
    log.info("轻量 OCR：%s 页无文字层，准备识别（最多 %s 页）", len(targets), max_pages)
    pages: list[LiteOcrPage] = []
    for page_no in targets:
        page = ocr_page(Path(pdf_path), page_no, dpi=dpi)
        if page is not None and page.lines:
            pages.append(page)
    return pages


def merge_into_document(doc: ParsedDocument, pages: list[LiteOcrPage]) -> int:
    """把 OCR 行合并成文字块；返回新增块数。"""
    next_order = max((block.order_idx for block in doc.blocks), default=-1) + 1
    added = 0
    for page in pages:
        for line in page.lines:
            doc.blocks.append(
                ParsedBlock(
                    page=page.page,
                    bbox=list(line.bbox),
                    type="text",
                    text=line.text,
                    order_idx=next_order,
                    engine="liteocr",
                )
            )
            next_order += 1
            added += 1
    if pages:
        doc.text_layer = True
    return added


def page_image_jpeg(
    pdf_path: Path, page_no: int, *, dpi: int = 150, max_long_side: int = MAX_LONG_SIDE
) -> bytes | None:
    """给视觉交叉校验用的整页 JPEG（控制尺寸，避免一张图烧掉太多 token）。"""
    try:
        image, _size = _render_page(Path(pdf_path), int(page_no), int(dpi))
        if image is None:
            return None
        if max(image.size) > max_long_side:
            image.thumbnail((max_long_side, max_long_side))
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        log.warning("渲染第 %s 页给模型失败：%s", page_no, exc)
        return None


__all__ = [
    "LiteOcrLine",
    "LiteOcrPage",
    "lite_ocr_available",
    "merge_into_document",
    "ocr_missing_pages",
    "ocr_page",
    "page_image_jpeg",
    "pages_with_text",
]
