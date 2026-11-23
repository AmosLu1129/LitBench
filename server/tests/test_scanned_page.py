# SPDX-License-Identifier: AGPL-3.0-or-later
"""扫描页（整页位图）的测试。

背景：真实影印课本每页就是**一张覆盖 100% 的位图** + 一层 OCR 文字，没有 vector path。
轻量档原来把「面积 >90% 的对象」一律当版面底纹丢掉，结果 5 本真实课本 5 中 0。
这里用合成扫描页回归：扫图页要能用 caption 锚点切出图，且裁出来有墨迹。
"""

from __future__ import annotations

import asyncio
import io

from PIL import Image

from litbench.adapters import get_parser
from pdf_fixture import make_scanned_pdf

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _parse(data: bytes, tmp_path):
    pdf = tmp_path / "scanned.pdf"
    pdf.write_bytes(data)
    return pdf, asyncio.run(get_parser("pdfium", render_images=True).parse(pdf))


def test_scanned_page_keeps_figures(tmp_path) -> None:
    """整页是位图的页，也要能按 caption 切出图（以前是 figures=0）。"""
    _pdf, doc = _parse(make_scanned_pdf(), tmp_path)

    assert doc.page_count == 1
    assert doc.text_layer is True  # 有 OCR 文字层
    assert len(doc.figures) >= 1, "扫描页仍然没出图：整页位图又被当底纹丢了？"

    fig = doc.figures[0]
    assert fig.label_raw and fig.label_raw.lower().startswith("fig")
    x0, y0, x1, y1 = fig.bbox
    assert 0 <= x0 < x1 <= 469 and 0 <= y0 < y1 <= 649
    # 图注在页高 45% 处，图应该切在它上方（左上原点 → y1 小于 page_h*0.55）
    assert y1 < 649 * 0.55, f"图区域跑到图注下方了：{fig.bbox}"
    assert y1 - y0 >= 50, f"带子太矮，可能又切到标题：{fig.bbox}"


def test_scanned_figure_thumbnail_has_ink(tmp_path) -> None:
    """裁出来的缩略图必须真有内容（合成图中间有一条黑带）。"""
    _pdf, doc = _parse(make_scanned_pdf(), tmp_path)
    fig = doc.figures[0]
    assert fig.image_bytes and fig.image_bytes.startswith(PNG_MAGIC)

    image = Image.open(io.BytesIO(fig.image_bytes)).convert("L")
    assert image.getextrema()[0] < 100, "缩略图全白：坐标或裁剪窗口不对"


def test_page_margin_is_clipped(tmp_path) -> None:
    """页眉页脚（页边 4%）不能留在图框里。"""
    _pdf, doc = _parse(make_scanned_pdf(), tmp_path)
    fig = doc.figures[0]
    _x0, y0, _x1, y1 = fig.bbox
    assert y0 >= 649 * 0.04 - 0.5
    assert y1 <= 649 * 0.96 + 0.5
