# SPDX-License-Identifier: AGPL-3.0-or-later
r"""Docling（MIT）适配器 —— 增强解析档。

默认档是 `pdfium`（零模型、快）。Docling 用在：
- 版面复杂（通栏图 + 双栏正文混排、跨页表格）；
- 需要表格结构（cell 级）；
- 需要真正的阅读顺序。

**已知的两个环境坑**（都踩过）：

1. **Windows 中文路径会挂。** `docling-parse` 的 C++ 层打不开含非 ASCII 的路径，
   报错形如 ``filename does not exists: ...\agent??\...``。项目目录名里带中文时，
   本地只能用目录联接（``mklink /J C:\\litbench D:\\中文目录\\litbench``）绕开；
   Docker 容器里是 Linux 路径，没有这个问题。

2. **首次运行要下模型**（layout / tableformer），默认走 HuggingFace。
   国内网络需要 ``HF_ENDPOINT=https://hf-mirror.com``，否则会在
   ``snapshot_download`` 上 ConnectTimeout。

字段映射（Docling DoclingDocument -> 本项目 ParsedDocument）：

    item.label            ->  ParsedBlock.type
    ------------------------------------------------
    section_header        ->  section
    title                 ->  title
    caption               ->  caption
    formula               ->  formula
    page_header/footer    ->  header / footer
    table                 ->  table（同时进 ParsedTable）
    picture               ->  figure（同时进 ParsedFigure）

图片的 caption 由 Docling 的 ``pic.captions``（RefItem 列表）给出，但**不保证有**；
没有时回退到「同页最近的 caption block」几何绑定。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .base import (
    ParsedBlock,
    ParsedDocument,
    ParsedFigure,
    ParsedTable,
    ParserError,
)
from .render import attach_figure_images

_LABEL_MAP = {
    "section_header": "section",
    "title": "title",
    "caption": "caption",
    "formula": "formula",
    "page_header": "header",
    "page_footer": "footer",
    "list_item": "text",
    "code": "text",
    "text": "text",
    "reference": "text",
    "footnote": "text",
    "checkbox_selected": "text",
    "checkbox_unselected": "text",
}

_CAPTION_GAP = 90.0


class DoclingParser:
    name = "docling"

    def __init__(
        self,
        *,
        max_pages: int | None = None,
        render_images: bool = True,
        render_dpi: int = 150,
        ocr: bool = False,
        ocr_lang: str = "chinese",
        table_structure: bool = True,
    ) -> None:
        self.max_pages = max_pages
        self.render_images = render_images
        self.render_dpi = render_dpi
        # 注意：docling 的 PdfPipelineOptions.do_ocr 默认是 True。主镜像以前没装 OCR 引擎所以
        # 它一直静默跳过；装上 onnxruntime 之后会真的跑 OCR，解析时间翻几倍。必须显式设置。
        self.ocr = ocr
        self.ocr_lang = ocr_lang
        self.table_structure = table_structure

    async def parse(self, pdf_path: Path) -> ParsedDocument:
        try:
            from docling.document_converter import (  # noqa: PLC0415
                DocumentConverter,
                PdfFormatOption,
            )
            from docling.datamodel.base_models import InputFormat  # noqa: PLC0415
            from docling.datamodel.pipeline_options import (  # noqa: PLC0415
                PdfPipelineOptions,
                RapidOcrOptions,
            )
        except ImportError as exc:  # pragma: no cover
            raise ParserError(
                "docling 未安装。pip install docling，或改用 PARSER_BACKEND=pdfium。"
            ) from exc

        options = PdfPipelineOptions()
        options.do_ocr = self.ocr
        options.do_table_structure = self.table_structure
        if self.ocr:
            try:
                options.ocr_options = RapidOcrOptions(lang=[self.ocr_lang])
            except Exception as exc:  # noqa: BLE001
                raise ParserError(f"docling OCR 引擎不可用（缺 onnxruntime？）：{exc}") from exc

        converter = DocumentConverter(
            allowed_formats=[InputFormat.PDF],
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
        )

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._parse_sync, Path(pdf_path), converter
        )

    # ------------------------------------------------------------------
    def _parse_sync(self, pdf_path: Path, converter) -> ParsedDocument:  # pragma: no cover
        if not pdf_path.exists():
            raise ParserError(f"文件不存在：{pdf_path}")

        try:
            result = converter.convert(str(pdf_path))
        except Exception as exc:  # noqa: BLE001
            raise ParserError(f"Docling 解析失败：{type(exc).__name__}: {exc}") from exc

        doc = result.document
        blocks: list[ParsedBlock] = []
        figures: list[ParsedFigure] = []
        tables: list[ParsedTable] = []
        order = 0

        for item, _level in doc.iterate_items():
            label = str(getattr(item, "label", "")).lower().strip()
            page, bbox = _page_and_bbox(item, doc)
            if label == "picture":
                figures.append(
                    ParsedFigure(page=page, bbox=bbox, caption=_item_caption(item, doc))
                )
                continue
            if label == "table":
                table_html = _table_html(item, doc)
                tables.append(
                    ParsedTable(
                        page=page,
                        bbox=bbox,
                        html=table_html,
                        cells=[],
                        # 表题要的是 caption，不是整张表。item.text 对 table 不存在，
                        # 回退到 export_to_markdown 会得到整张表——不要那样用。
                        caption=_item_caption(item, doc),
                        # 拿不到 grid 的表只能当图片表：UI / MCP 都靠 quality 区分
                        quality="structured" if table_html else "image",
                    )
                )
                continue

            blocks.append(
                ParsedBlock(
                    page=page,
                    bbox=bbox,
                    type=_LABEL_MAP.get(label, "text"),
                    text=_text_of(item),
                    order_idx=order,
                )
            )
            order += 1

        _bind_captions(figures, blocks)
        _apply_figure_labels(figures)
        if self.render_images:
            # Docling 只给 (page, bbox)，不给像素。缩略图是产品需要（图卡片没有图就是坏的），
            # 所以用 pdfium 按同一套左上原点坐标补渲染——两个后端共用 render.py。
            attach_figure_images(pdf_path, figures, dpi=self.render_dpi)

        return ParsedDocument(
            page_count=len(getattr(doc, "pages", []) or []),
            engine=self.name,
            engine_version=_docling_version(),
            blocks=blocks,
            figures=figures,
            tables=tables,
            text_layer=sum(len(b.text) for b in blocks) > 100,
        )


# ----------------------------------------------------------------------
def _page_and_bbox(item, doc) -> tuple[int, list[float]]:
    """把 Docling 的 provenance 转成本项目的 (page, [x0,y0,x1,y1])。

    坑：Docling 的 bbox 是 **BOTTOMLEFT 原点**，且只给 l/t/r/b。
    要换成左上原点必须知道**页高**，不能取负号了事 ——
    取负会得到负数坐标，overlay 直接画到页面外。
    """
    prov = getattr(item, "prov", None) or []
    if not prov:
        return 0, [0.0, 0.0, 0.0, 0.0]
    p = prov[0]
    page = int(getattr(p, "page_no", 0) or 0)
    bbox = getattr(p, "bbox", None)
    if bbox is None:
        return page, [0.0, 0.0, 0.0, 0.0]

    left = float(getattr(bbox, "l", 0.0))
    top = float(getattr(bbox, "t", 0.0))
    right = float(getattr(bbox, "r", 0.0))
    bottom = float(getattr(bbox, "b", 0.0))
    origin = str(getattr(bbox, "coord_origin", "")).lower()

    height = _page_height(doc, page)
    if "bottom" in origin and height > 0:
        y0, y1 = height - top, height - bottom
    else:
        y0, y1 = top, bottom

    return page, [
        round(min(left, right), 1),
        round(min(y0, y1), 1),
        round(max(left, right), 1),
        round(max(y0, y1), 1),
    ]


def _page_height(doc, page_no: int) -> float:
    try:
        page = doc.pages[page_no]
        return float(page.size.height)
    except Exception:  # noqa: BLE001
        return 0.0


def _item_caption(item, doc) -> str:
    """图/表自带的 caption 链接，优先用它；不行再退回几何绑定（只对图有意义）。"""
    fn = getattr(item, "caption_text", None)
    if callable(fn):
        try:
            text = fn(doc)
            if text:
                return str(text)
        except Exception:  # noqa: BLE001
            pass
    for ref in getattr(item, "captions", None) or []:
        text = _resolve_text_ref(doc, ref)
        if text:
            return text
    return ""


def _resolve_text_ref(doc, ref) -> str:
    cref = getattr(ref, "cref", "") or ""
    if not cref.startswith("#/texts/"):
        return ""
    try:
        index = int(cref.rsplit("/", 1)[1])
        return str(getattr(doc.texts[index], "text", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def _text_of(item) -> str:
    text = getattr(item, "text", None)
    if isinstance(text, str):
        return text
    try:
        return str(item.export_to_markdown()) if hasattr(item, "export_to_markdown") else ""
    except Exception:  # noqa: BLE001
        return ""


def _table_html(item, doc=None) -> str | None:
    """把 Docling 的 table item 导出成 HTML；Markdown 兜底。

    坑（2026-11-13 实测 docling 2.150 + docling-core）：``export_to_html()`` /
    ``export_to_markdown()`` 都新增了可选参数 ``doc``，而**不传 doc 时不再报错，
    而是返回空串**（旧代码只 catch 异常，于是 19 张表的 html 全被存成空串）。
    所以这里必须显式把 doc 传进去，并且把「返回空串」也当成失败、继续尝试兜底。
    """
    exporters = ("export_to_html", "export_to_markdown")
    for meth in exporters:
        fn = getattr(item, meth, None)
        if fn is None:
            continue
        try:
            text = fn(doc=doc) if doc is not None else fn()
        except Exception:  # noqa: BLE001
            continue
        text = str(text or "").strip()
        if text:
            return text
    return None


def _bind_captions(figures: list[ParsedFigure], blocks: list[ParsedBlock]) -> None:
    """Docling 不保证给出 picture 的 caption，这里用几何兜底绑一次。"""
    captions = [b for b in blocks if b.type == "caption"]
    used: set[int] = set()
    for fig in figures:
        if fig.caption or fig.label_raw:
            continue
        best: tuple[float, int] | None = None
        for idx, cap in enumerate(captions):
            if idx in used or cap.page != fig.page:
                continue
            if min(fig.bbox[2], cap.bbox[2]) <= max(fig.bbox[0], cap.bbox[0]):
                continue
            if fig.bbox[3] <= cap.bbox[1]:
                gap = cap.bbox[1] - fig.bbox[3]
            elif cap.bbox[3] <= fig.bbox[1]:
                gap = fig.bbox[1] - cap.bbox[3]
            else:
                continue
            if gap > _CAPTION_GAP:
                continue
            if best is None or gap < best[0]:
                best = (gap, idx)
        if best is None:
            continue
        used.add(best[1])
        cap = captions[best[1]]
        from ..labels import normalize_label  # noqa: PLC0415

        norm = normalize_label(cap.text)
        if not norm or not norm.startswith("fig"):
            continue
        fig.caption = cap.text
        fig.label_raw = cap.text.strip()[:32]


def _apply_figure_labels(figures: list[ParsedFigure]) -> None:
    """从 caption 文本里抽出图号（Docling 不给 label_raw）。"""
    from ..labels import LABEL_RE, normalize_label  # noqa: PLC0415

    for fig in figures:
        if not fig.caption:
            continue
        match = LABEL_RE.match(fig.caption.strip())
        if not match:
            continue
        norm = normalize_label(match.group(0))
        if norm and norm.startswith("fig"):
            fig.label_raw = match.group(0).strip()


def _docling_version() -> str:
    try:
        import importlib.metadata as md

        return md.version("docling")
    except Exception:  # noqa: BLE001
        return "unknown"


# MinerU 的实现搬到 mineru_adapter.py：它必须走独立子进程（AGPL 隔离），
# 不能和 docling 共用一个文件、更不能被主进程 import。
