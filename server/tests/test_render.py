# SPDX-License-Identifier: AGPL-3.0-or-later
"""缩略图渲染的测试。

抽出来的原因很具体：Docling 只给 (page, bbox) 不给像素，升级之后图卡片会变成
「无图片」——界面上等于坏了。

更重要的是**坐标原点**：`make_pdf` 里的矩形在 PDF 原生坐标 y=520..650（页高 792），
换成左上原点就是 142..272。裁错原点不会报错，只会裁到页面另一半的正文——
这正是"缩略图是正文"的真因，所以这里的断言看的是**裁出来的图里有没有墨迹**，
不是"文件存在"。
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from litbench.adapters import ParsedFigure, get_parser
from litbench.adapters.render import attach_figure_images, crop_to_png, render_page
from pdf_fixture import make_pdf

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# make_pdf 里那个矢量矩形（100,520 - 400,650，PDF 原生左下原点）
# → 左上原点 = (100, 142) - (400, 272)，页高 792
FIGURE_BBOX = [100.0, 142.0, 400.0, 272.0]


def has_ink(data: bytes | None) -> bool:
    """裁出来的图里有没有深色像素。纯白 = 裁到空白区，等于坐标错了。"""
    if not data:
        return False
    image = Image.open(io.BytesIO(data)).convert("L")
    return image.getextrema()[0] < 100


def test_attach_images_from_vector_figure(tmp_path) -> None:
    pdf = tmp_path / "synthetic.pdf"
    pdf.write_bytes(make_pdf())
    fig = ParsedFigure(page=1, bbox=list(FIGURE_BBOX), label_raw="Fig. 1")

    assert attach_figure_images(pdf, [fig], dpi=150) == 1
    assert fig.image_bytes is not None
    assert fig.image_bytes.startswith(PNG_MAGIC)
    assert has_ink(fig.image_bytes), "框里没有墨迹：坐标原点或换算错了"

    # 300x130 pt @150dpi ≈ 625x271 px，允许几像素误差（坐标取整）
    image = Image.open(io.BytesIO(fig.image_bytes))
    assert abs(image.width - 625) <= 4, image.size
    assert abs(image.height - 271) <= 4, image.size


def test_parser_bbox_is_top_left_and_crops_the_right_region(tmp_path) -> None:
    """端到端回归：解析器给的 bbox 直接拿去裁，必须裁到真的图。

    这是"镜像 bug"的守门测试——之前 bbox 是左下原点、裁剪按左上用，
    结果每张缩略图都是从页面另一半裁出来的正文。
    """
    pdf = tmp_path / "synthetic.pdf"
    pdf.write_bytes(make_pdf())

    import asyncio

    doc = asyncio.run(get_parser("pdfium", render_images=False).parse(pdf))
    assert len(doc.figures) == 1
    bbox = doc.figures[0].bbox
    # 左上原点：矩形在页面偏下（792-650=142 到 792-520=272）
    assert 120 <= bbox[1] <= 165, bbox
    assert 250 <= bbox[3] <= 300, bbox

    import pypdfium2 as pdfium

    pd = pdfium.PdfDocument(str(pdf))
    page = pd[0]
    page_image = render_page(page, 150)
    data = crop_to_png(page_image, bbox, 150)
    page.close()
    pd.close()

    assert has_ink(data), "用解析器自己的 bbox 裁出来是空白：坐标系不一致"


def test_tiny_box_is_skipped(tmp_path) -> None:
    """小于 8px 的框没有缩略图价值，直接放弃而不是产出一张噪点图。"""
    pdf = tmp_path / "synthetic.pdf"
    pdf.write_bytes(make_pdf())
    fig = ParsedFigure(page=1, bbox=[100.0, 142.0, 101.0, 143.0])

    assert attach_figure_images(pdf, [fig]) == 0
    assert fig.image_bytes is None


def test_bad_page_number_is_ignored(tmp_path) -> None:
    """docling 偶尔给 page=0（没有 provenance）；不能因此抛异常。"""
    pdf = tmp_path / "synthetic.pdf"
    pdf.write_bytes(make_pdf())
    fig = ParsedFigure(page=0, bbox=list(FIGURE_BBOX))

    assert attach_figure_images(pdf, [fig]) == 0


def test_existing_bytes_are_not_overwritten(tmp_path) -> None:
    pdf = tmp_path / "synthetic.pdf"
    pdf.write_bytes(make_pdf())
    fig = ParsedFigure(page=1, bbox=list(FIGURE_BBOX), image_bytes=b"keep me")

    assert attach_figure_images(pdf, [fig]) == 0
    assert fig.image_bytes == b"keep me"
