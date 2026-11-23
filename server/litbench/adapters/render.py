# SPDX-License-Identifier: AGPL-3.0-or-later
"""图区域截图（缩略图），两个解析后端共用。

为什么单独抽出来：Docling 只给 ``(page, bbox)``，不给像素；而界面上没有缩略图
就等于这个产品不存在。把「整页渲染 + PIL 裁剪」放在一处，两个后端就共用同一套
坐标语义（左上原点、单位 pt）。

**不要用 ``page.render(crop=...)``**：pypdfium2 5.x 的 crop 语义与直觉不同
（给 150x150 的框会返回 724x1084 的位图），容易拿到错位的图。整页渲染一次 +
按 bbox 裁剪，多花一点内存，结果确定。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from .base import ParsedFigure

MIN_SIDE_PX = 8  # 比这还小的框直接放弃，缩略图没有意义


def render_page(page: Any, dpi: int):  # noqa: ANN201 — 返回 PIL.Image，避免在这里 import PIL
    """整页渲染成 PIL 图像；失败返回 None（渲染失败不该让整篇解析失败）。"""
    try:
        return page.render(scale=dpi / 72.0).to_pil().convert("RGB")
    except Exception:  # noqa: BLE001
        return None


def crop_to_png(page_image: Any, bbox: list[float], dpi: int) -> bytes | None:
    """按 bbox（左上原点、pt）裁一块并编码成 PNG。"""
    scale = dpi / 72.0
    x0, y0, x1, y1 = bbox
    box = (
        max(int(x0 * scale), 0),
        max(int(y0 * scale), 0),
        min(int(x1 * scale), page_image.width),
        min(int(y1 * scale), page_image.height),
    )
    if box[2] - box[0] < MIN_SIDE_PX or box[3] - box[1] < MIN_SIDE_PX:
        return None
    buf = io.BytesIO()
    try:
        page_image.crop(box).save(buf, format="PNG", optimize=True)
    except Exception:  # noqa: BLE001
        return None
    return buf.getvalue()


def attach_figure_images(pdf_path: Path, figures: list[ParsedFigure], *, dpi: int = 150) -> int:
    """给还没有 ``image_bytes`` 的图补缩略图，返回补齐的数量。

    每页只渲染一次（一篇 20 页的论文最多渲染 20 次，不是每张图一次），
    所以 Docling 那种「图很多」的结果也不会把耗时拉爆。
    """
    targets = [
        f for f in figures if f.image_bytes is None and f.page > 0 and len(f.bbox) == 4
    ]
    if not targets:
        return 0

    try:
        import pypdfium2 as pdfium
    except ImportError:  # pragma: no cover — 主镜像必装
        return 0
    try:
        doc = pdfium.PdfDocument(str(pdf_path))
    except Exception:  # noqa: BLE001
        return 0

    done = 0
    try:
        for page_no in sorted({f.page for f in targets}):
            if page_no < 1 or page_no > len(doc):
                continue
            page = doc[page_no - 1]
            try:
                page_image = render_page(page, dpi)
                if page_image is None:
                    continue
                for fig in (f for f in targets if f.page == page_no):
                    data = crop_to_png(page_image, fig.bbox, dpi)
                    if data:
                        fig.image_bytes = data
                        done += 1
            finally:
                page.close()
    finally:
        doc.close()
    return done
