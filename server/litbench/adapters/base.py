# SPDX-License-Identifier: AGPL-3.0-or-later
"""解析引擎的统一出口。

所有引擎都必须产出 ``ParsedDocument``——**归一化层的边界就在这里**。
往前的代码只认识 ParsedDocument，不关心底下是 Docling 还是 MinerU。

注意：AGPL 组件只能出现在 adapters 里，且必须走独立容器（见 LICENSING.md）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class ParsedBlock:
    page: int
    bbox: list[float]  # [x0, y0, x1, y1]，左上原点，单位 pt
    type: str
    text: str = ""
    order_idx: int = 0
    # 单个块的来源（混合文档：pdfium 文字层 + 轻量 OCR 补的页）；None 表示跟随 doc.engine
    engine: str | None = None


@dataclass(slots=True)
class ParsedFigure:
    page: int
    bbox: list[float]
    label_raw: str | None = None
    caption: str = ""
    image_bytes: bytes | None = None
    figure_type: str | None = None   # chart|micrograph|photo|map|diagram|structure|other
    caption_block_id: str | None = None  # 指向 caption 所在的 block，便于整段回溯
    # 图注区域（左上原点、pt）。MinerU 把图注挂在图块 content 的子块里，能给出精确 bbox；
    # 第二套 OCR（RapidOCR）靠它只裁图注小图做交叉校核（见 ocr_cross.py），不必整页重跑。
    caption_bbox: list[float] | None = None
    # 图号校核（labeling.py 规则层写入）：编号从哪来、有多可信、属于哪个子图组
    label_source: str | None = None   # text|rule_repair|rule_inherit|rule_infer|ocr|ocr_consensus|review
    label_confidence: float | None = None  # 0–1
    sublabel: str | None = None       # 子图字母 a/b/c（组内按 bbox 顺序）
    group_key: str | None = None      # 同一系列子图的组 key（version 内）


@dataclass(slots=True)
class ParsedTable:
    page: int
    bbox: list[float]
    html: str | None = None
    cells: list = field(default_factory=list)
    caption: str = ""
    quality: str = "structured"  # structured|image


@dataclass(slots=True)
class ParsedDocument:
    page_count: int
    engine: str
    engine_version: str
    blocks: list[ParsedBlock] = field(default_factory=list)
    figures: list[ParsedFigure] = field(default_factory=list)
    tables: list[ParsedTable] = field(default_factory=list)
    # False 表示这份 PDF 没有可用文字层（扫描件），需要走 OCR 档
    text_layer: bool = True
    # 文字层质量分（0–1）与判定依据；低分时 worker 会更信任 OCR（见 docs/14）
    text_quality: float | None = None
    text_quality_flags: dict = field(default_factory=dict)


@runtime_checkable
class Parser(Protocol):
    name: str

    async def parse(self, pdf_path: Path) -> ParsedDocument: ...


class ParserError(RuntimeError):
    """解析失败。worker 捕获后把 job 标 failed，不吞异常。"""
