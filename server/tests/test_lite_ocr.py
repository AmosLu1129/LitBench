# SPDX-License-Identifier: AGPL-3.0-or-later
"""轻量 OCR 的纯函数单测；不加载真实 RapidOCR 模型。"""

from __future__ import annotations

from litbench.adapters.base import ParsedBlock, ParsedDocument
from litbench.lite_ocr import LiteOcrLine, LiteOcrPage, merge_into_document, pages_with_text


def _doc() -> ParsedDocument:
    return ParsedDocument(
        page_count=3,
        engine="pdfium",
        engine_version="test",
        blocks=[ParsedBlock(page=1, bbox=[0, 0, 10, 10], type="text", text="第一页正文", order_idx=0)],
        text_layer=False,
    )


def test_pages_with_text_ignores_empty_and_non_text_blocks() -> None:
    doc = _doc()
    doc.blocks.append(ParsedBlock(page=2, bbox=[0, 0, 1, 1], type="caption", text="  "))
    doc.blocks.append(ParsedBlock(page=3, bbox=[0, 0, 1, 1], type="figure", text=""))
    assert pages_with_text(doc) == {1}


def test_merge_into_document_marks_engine_and_text_layer() -> None:
    doc = _doc()
    pages = [
        LiteOcrPage(
            page=2,
            width=595,
            height=842,
            lines=[
                LiteOcrLine(text="OCR 第一行", bbox=[10, 20, 100, 30], confidence=0.9),
                LiteOcrLine(text="OCR 第二行", bbox=[10, 35, 120, 45], confidence=0.8),
            ],
        )
    ]
    added = merge_into_document(doc, pages)
    assert added == 2
    assert doc.text_layer is True
    assert [block.engine for block in doc.blocks[-2:]] == ["liteocr", "liteocr"]
    assert [block.order_idx for block in doc.blocks[-2:]] == [1, 2]
    assert pages[0].text == "OCR 第一行\nOCR 第二行"
