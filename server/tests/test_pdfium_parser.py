# SPDX-License-Identifier: AGPL-3.0-or-later
"""pdfium 解析器的坐标契约与阅读顺序回归（2026-11-13 审查修复）。

两个曾经写反的东西：

1. pypdfium2 的 charbox 是 **PDF 原生左下原点（y 向上）**，但之前直接把行 bbox
   落库、还按 y0 升序排阅读顺序 → 整页段落倒序（第一条是页脚），并且和 figure
   的左上原点 bbox 混在一列。现在内部仍用原生坐标算几何，出口统一翻成左上原点。
2. 标题/页脚判据原来按「左上原点」写，在原生坐标里语义正好相反。

另外守住「行级块要能填满字符预算」：pdfium 一节 4 页窗口有 200+ 行级块，
旧上限 40 条会只送进不到一页正文。
"""

from __future__ import annotations

from types import SimpleNamespace

from litbench.adapters.pdfium_parser import Line, PdfiumParser
from litbench.config import settings
from litbench.summaries import _evidence_lines
from pdf_fixture import make_pdf

PAGE_W = 612.0
PAGE_H = 792.0


async def test_pdfium_block_bbox_is_top_left_and_reading_order_top_down(tmp_path) -> None:
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(make_pdf())

    doc = await PdfiumParser(render_images=False).parse(pdf)
    assert doc.text_layer
    assert doc.page_count == 1

    blocks = [b for b in doc.blocks if (b.text or "").strip()]
    assert blocks, "解析结果里应该有正文块"

    # 1) bbox 必须是左上原点：落在页面内、上边小于下边
    for b in blocks:
        x0, y0, x1, y1 = b.bbox
        assert 0.0 <= x0 < x1 <= PAGE_W + 1, f"x 越界：{b.bbox}"
        assert 0.0 <= y0 < y1 <= PAGE_H + 1, f"y 越界：{b.bbox}"

    ordered = sorted(blocks, key=lambda b: b.order_idx)

    # 2) 阅读顺序自上而下：|text| 在 PDF 里的 y（原生）是 740、700、512，
    #    对应的左上原点 y 应递增
    ys = [b.bbox[1] for b in ordered]
    assert ys == sorted(ys), f"order_idx 必须与页面自上而下一致，实际 {ys}"

    texts = [b.text or "" for b in ordered]
    assert texts[0].startswith("Supplementary notes"), f"第一条应是页面最上方的行，实际 {texts[0]!r}"
    assert texts[1].startswith("Results are shown"), f"第二条实际 {texts[1]!r}"

    caption = next(b for b in ordered if b.type == "caption")
    assert caption.text.replace(" ", "").startswith("Fig.1.")
    assert caption.order_idx > ordered[0].order_idx

    # 3) 这张 PDF 的正文在最上方、没有页脚；标题判据不能把页脚判出来
    assert not any(b.type == "footer" for b in ordered)


def test_reading_order_sorts_by_line_top_descending() -> None:
    """原生坐标里「自上而下」= 行上边 y1 降序，不是 y0 升序。"""

    def line(text: str, bottom: float, size: float = 10.0) -> Line:
        return Line(text=text, x0=10.0, y0=bottom, x1=100.0, y1=bottom + size, size=size)

    lines = [line("bottom", 10.0), line("top", 700.0), line("middle", 400.0)]
    ordered = PdfiumParser()._reading_order(lines, PAGE_W)
    assert [ln.text for ln in ordered] == ["top", "middle", "bottom"]


def test_pdfium_line_blocks_fill_char_budget() -> None:
    """行级块不应该被旧的 40 条上限卡住；字符预算才是主约束。"""
    blocks = [
        SimpleNamespace(id=f"b{i:04d}", page=1 + i // 40, text="字" * 120) for i in range(200)
    ]
    _lines, ids, texts = _evidence_lines(blocks)
    assert len(ids) > 40, f"只送了 {len(ids)} 条，行级块会被截断"
    assert sum(len(t) for t in texts) <= settings.summary_max_chars
