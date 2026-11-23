# SPDX-License-Identifier: AGPL-3.0-or-later
"""解析引擎适配器。

**AGPL 组件只能出现在这个包里**，且必须走独立容器或子进程。
主镜像的依赖清单里不允许出现 copyleft（CI 会拦，见 scripts/check_licenses.py）。

三档后端：

| backend   | 实现                | 用途                                     |
| --------- | ------------------- | ---------------------------------------- |
| pdfium    | pypdfium2           | 默认。有文字层的 PDF，快、零模型         |
| docling   | Docling（MIT）      | 增强档。版面/表格/矢量图更好，但依赖重   |
| mineru    | MinerU（AGPL 容器） | 可选。学术 PDF 最强，必须独立容器        |
"""

from .base import (
    ParsedBlock,
    ParsedDocument,
    ParsedFigure,
    ParsedTable,
    Parser,
    ParserError,
)
from .docling_adapter import DoclingParser
from .mineru_adapter import MineruParser
from .pdfium_parser import PdfiumParser


def get_parser(
    backend: str,
    *,
    mineru_url: str | None = None,
    render_images: bool = True,
    ocr: bool = False,
    ocr_lang: str = "chinese",
) -> Parser:
    """按配置选后端。默认走轻量档——绝大多数有文字层的 PDF 不需要上模型。

    ``render_images=True`` 是产品需要（图卡片没有缩略图就等于没有界面），
    指标脚本会显式传 ``False`` 来测纯解析耗时。

    ``ocr=True`` 只影响 docling（走 RapidOCR，``ocr_lang`` 控制语言模型）；
    MinerU 本身就是 OCR 引擎，它的 CLI 走独立子进程（AGPL 隔离，见 ``mineru_adapter``）。
    """
    normalized = (backend or "pdfium").strip().lower()
    if normalized == "docling":
        return DoclingParser(render_images=render_images, ocr=ocr, ocr_lang=ocr_lang)
    if normalized == "mineru":
        return MineruParser(render_images=render_images)
    return PdfiumParser(render_images=render_images)


__all__ = [
    "DoclingParser",
    "MineruParser",
    "ParsedBlock",
    "ParsedDocument",
    "ParsedFigure",
    "ParsedTable",
    "Parser",
    "ParserError",
    "PdfiumParser",
    "get_parser",
]
