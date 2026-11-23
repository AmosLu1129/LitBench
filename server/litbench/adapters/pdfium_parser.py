# SPDX-License-Identifier: AGPL-3.0-or-later
"""轻量解析后端（pypdfium2，Apache-2.0/BSD-3）。

**它是路由里的「有文字层」那一支**，不是 Docling 的替代品：
快、零模型、零 GPU；阅读顺序按「先分栏、再自上而下」近似；
表结构识别和复杂版面留给增强档。

三个已经踩过坑、写死在实现里的决定：

1. **按字符垂直中心分行，不按基线。**
   带下伸部的 g/p/q/y 的 glyph 底部比基线低 2–3pt。按 bottom 分行会把它们
   甩到相邻行，正文随机少字母（"Comparison" -> "Com arison"），而且这些
   错误肉眼极难发现，却会污染所有下游文本。

2. **图不是从「image 对象」来的。**
   arXiv 上大量论文的图是 matplotlib 出的矢量图（一篇里几千个 path），
   根本没有 image 对象。所以把 path(2) 与 image(3) 一起当绘图基元。

3. **以 caption 为锚点生长图区域，而不是先检图再配对。**
   用户要的是「按 Fig. 3 找到那张图」，所以每个图号必须对应一个 bbox。
   先找图再配 caption 会陷入「多子图拆碎 / 一张 caption 只能绑一张图」的死结。
   反过来：以 caption 为起点，向最近的绘图基元连通域生长，天然处理多子图。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from ..labels import LABEL_RE, looks_like_caption, normalize_label
from .base import ParsedBlock, ParsedDocument, ParsedFigure, ParserError
from .render import crop_to_png, render_page

# pypdfium2 对象类型：1=text 2=path 3=image 5=shading
_TEXT = 1
_PATH = 2
_IMAGE = 3

# 扫描页（整页是一张位图）的判据与切图参数
_SCANNED_IMAGE_RATIO = 0.9  # 单张图像覆盖页面 ≥90% → 这是扫描页，不是矢量正文
_SCAN_PAD = 3.0  # 图框与正文/图注之间留的空隙（pt）
_SCAN_MIN_HEIGHT = 50.0  # 比这还矮的「图」多半是标题/横线，不硬造
_SCAN_MARGIN = 24.0  # 页面找不到正文可参照时用的页边距（pt）
_SCAN_MIN_AREA_RATIO = 0.02  # 图区域至少占页面 2%
_SCAN_MAX_TEXT_COVER = 0.2  # 框内文字覆盖率超过它就当裁到正文/标题，宁可不出图
# 章节编号行（"7.7 热管简介" / "10.4.5 V 带传动张紧装置设计"）——这类带子不是图。
# OCR 常把 "7.7" 读成 "7 7"，所以数字分隔符要容忍空格/点/全角点。
_SECTION_HEAD_RE = re.compile(r"^\d{1,3}(?:[\s.．·]\d{1,3}){1,3}\s*\D")


@dataclass(slots=True)
class Line:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2


class PdfiumParser:
    name = "pdfium"

    def __init__(
        self,
        *,
        min_figure_area_ratio: float = 0.01,
        merge_gap: float = 25.0,
        render_dpi: int = 150,
        render_images: bool = False,
    ) -> None:
        self.min_figure_area_ratio = min_figure_area_ratio
        self.merge_gap = merge_gap
        self.render_dpi = render_dpi
        self.render_images = render_images

    async def parse(self, pdf_path: Path) -> ParsedDocument:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._parse_sync, Path(pdf_path))

    # ------------------------------------------------------------------
    def _parse_sync(self, pdf_path: Path) -> ParsedDocument:
        try:
            import pypdfium2 as pdfium
        except ImportError as exc:  # pragma: no cover
            raise ParserError("pypdfium2 未安装：pip install pypdfium2") from exc
        if not pdf_path.exists():
            raise ParserError(f"文件不存在：{pdf_path}")

        doc = pdfium.PdfDocument(str(pdf_path))
        blocks: list[ParsedBlock] = []
        figures: list[ParsedFigure] = []
        total_chars = 0
        page_count = len(doc)

        try:
            for pno in range(page_count):
                page = doc[pno]
                width, height = page.get_size()
                textpage = page.get_textpage()

                lines = self._extract_lines(textpage)
                total_chars += sum(len(line.text) for line in lines)
                page_blocks = self._to_blocks(
                    self._reading_order(lines, width), pno + 1, len(blocks), height
                )

                primitives = self._drawing_primitives(page, width, height)
                scanned = self._is_scanned_page(page, width, height)
                used: set[int] = set()
                emitted: list[list[float]] = []
                text_blocks = [b for b in page_blocks if b.type in ("text", "section", "title")]
                for pos, block in enumerate(page_blocks):
                    if block.type != "caption":
                        continue
                    label = _label_from_text(block.text)
                    if not label or not label[1].startswith("fig"):
                        continue  # 表题交给增强档
                    if scanned:
                        # 扫描页整页是一张位图，靠绘图基元生长没有意义；
                        # 用「caption + 正文行」的空白带切出图区域。
                        box = self._resolve_scanned_box(block, page_blocks, width, height)
                    else:
                        box = self._resolve_figure_box(
                            block, primitives, width, height, used, text_blocks
                        )
                    if box is None:
                        continue
                    box = _to_top_left(box, height)
                    if any(_box_iou(box, other) > 0.9 for other in emitted):
                        continue  # 同一页两张图注切到同一块，只留一个
                    emitted.append(box)
                    figures.append(
                        ParsedFigure(
                            page=pno + 1,
                            bbox=box,
                            label_raw=label[0],
                            caption=self._merge_caption(page_blocks, block),
                        )
                    )

                if self.render_images:
                    self._attach_images(
                        page, [f for f in figures if f.page == pno + 1], pno + 1, width, height
                    )

                # 内部几何（图框生长 / 扫描页空白带 / caption 合并）都在 PDF 原生
                # 左下原点里算完，最后对外的 ParsedBlock 必须统一成左上原点
                # （adapters/base.py 的契约；PDF.js overlay / 裁剪 / golden 都按左上用）。
                for block in page_blocks:
                    block.bbox = _to_top_left(block.bbox, height)
                blocks.extend(page_blocks)

                textpage.close()
                page.close()
        finally:
            doc.close()

        return ParsedDocument(
            page_count=page_count,
            engine=self.name,
            engine_version=_pdfium_version(),
            blocks=blocks,
            figures=figures,
            tables=[],
            text_layer=total_chars > 100,
        )

    # ------------------------------------------------------------------
    # 文本
    # ------------------------------------------------------------------
    def _extract_lines(self, textpage) -> list[Line]:
        chars: list[tuple[str, float, float, float, float]] = []
        try:
            count = textpage.count_chars()
        except Exception:  # noqa: BLE001
            return []
        for i in range(count):
            ch = textpage.get_text_range(i, 1)
            if not ch or ch in ("\r", "\n", "\x00", "\ufffe", "\u200b"):
                continue
            try:
                left, bottom, right, top = textpage.get_charbox(i)
            except Exception:  # noqa: BLE001
                continue
            chars.append((ch, left, bottom, right, top))
        if not chars:
            return []

        heights = [c[4] - c[2] for c in chars if c[4] > c[2]]
        size = _median(heights) or 10.0
        tol = max(1.5, size * 0.30)
        chars.sort(key=lambda c: -((c[4] + c[2]) / 2))

        lines: list[Line] = []
        bucket: list[tuple[str, float, float, float, float]] = []
        center: float | None = None
        for item in chars:
            cur = (item[4] + item[2]) / 2
            if center is None or abs(cur - center) <= tol:
                bucket.append(item)
                center = cur if center is None else (center + cur) / 2
            else:
                lines.append(_make_line(bucket))
                bucket = [item]
                center = cur
        if bucket:
            lines.append(_make_line(bucket))
        return [ln for ln in lines if ln.text.strip()]

    def _reading_order(self, lines: list[Line], page_width: float) -> list[Line]:
        """近似阅读顺序：判断是否双栏，跨栏元素按「上下」插回。

        **内部坐标是 PDF 原生左下原点（y 向上）**：自上而下 = 行的上边 ``y1``
        降序。不能按 ``y0`` 升序排——那等于从页底往上读（这是修复前的老 bug，
        表现为整页段落倒序、第一条证据是页脚）。
        """
        if not lines:
            return []
        spanning = [ln for ln in lines if ln.width > page_width * 0.55]
        body = [ln for ln in lines if ln.width <= page_width * 0.55]
        if len(body) < 6:
            return sorted(lines, key=lambda ln: (-ln.y1, ln.x0))

        mid = page_width / 2
        left = [ln for ln in body if ln.cx < mid]
        right = [ln for ln in body if ln.cx >= mid]
        if len(left) < 4 or len(right) < 4:
            return sorted(lines, key=lambda ln: (-ln.y1, ln.x0))

        left.sort(key=lambda ln: -ln.y1)
        right.sort(key=lambda ln: -ln.y1)
        spanning.sort(key=lambda ln: -ln.y1)

        merged: list[Line] = []
        li = ri = 0
        for sp in spanning:
            # 行整体在跨栏元素**上方**：行底边 y0 ≥ 跨栏块上边 y1（原生坐标）
            while li < len(left) and left[li].y0 >= sp.y1:
                merged.append(left[li])
                li += 1
            while ri < len(right) and right[ri].y0 >= sp.y1:
                merged.append(right[ri])
                ri += 1
            merged.append(sp)
        merged.extend(left[li:])
        merged.extend(right[ri:])
        return merged

    def _merge_caption(self, blocks: list[ParsedBlock], caption: ParsedBlock) -> str:
        """把 caption 的后续行拼回去。

        解析器是行级的，一条 caption 往往占 2–6 行；只取第一行会让卡片摘要
        变成半句（"FIG. 3: Aluminium at low temperature. Left: abso..."）。

        **不能用「阅读顺序里的下一个 block」**：阅读顺序会把页脚、其他栏的首行
        插进来，一进循环就断。这里改成按几何找下一行：
        紧贴在上一行下方（行间 gap ≤ 3pt）、左边界对齐（同一栏）。
        """
        page_text = [b for b in blocks if b.type == "text"]
        parts = [caption.text]
        prev = caption
        used: set[str] = set()
        while len(parts) < 10 and sum(len(p) for p in parts) < 1200:
            candidates = [
                b
                for b in page_text
                if b.text not in used
                # 原生坐标（y 向上）：下一行的**上边**紧贴上一行的**下边**
                and -3.0 <= (prev.bbox[1] - b.bbox[3]) <= 3.5
                and abs(b.bbox[0] - caption.bbox[0]) <= 18
            ]
            if not candidates:
                break
            nxt = min(candidates, key=lambda b: -b.bbox[3])
            used.add(nxt.text)
            parts.append(nxt.text)
            prev = nxt
        return " ".join(p.strip() for p in parts if p.strip())

    def _to_blocks(
        self, lines: list[Line], page: int, base_idx: int, page_height: float
    ) -> list[ParsedBlock]:
        blocks: list[ParsedBlock] = []
        max_size = max((ln.size for ln in lines), default=0.0)
        for offset, line in enumerate(lines):
            raw = line.text.strip()
            kind = "text"
            if looks_like_caption(raw):
                kind = "caption"
            # 原生坐标（y 向上）：页脚贴近页底 → 行的上边 y1 小于 15% 页高；
            # 标题贴近页顶 → y1 大于 65% 页高。原来按「左上原点」写的判据在这里正好反了。
            elif raw.isdigit() and len(raw) <= 3 and line.y1 < page_height * 0.15:
                kind = "footer"
            elif max_size and line.size >= max_size * 0.95 and line.y1 > page_height * 0.65:
                kind = "title"
            blocks.append(
                ParsedBlock(
                    page=page,
                    bbox=[round(line.x0, 2), round(line.y0, 2), round(line.x1, 2), round(line.y1, 2)],
                    type=kind,
                    text=raw,
                    order_idx=base_idx + offset,
                )
            )
        return blocks

    # ------------------------------------------------------------------
    # 图
    # ------------------------------------------------------------------
    def _drawing_primitives(
        self, page, page_width: float, page_height: float
    ) -> list[tuple[float, float, float, float]]:
        page_area = page_width * page_height
        primitives: list[tuple[float, float, float, float]] = []
        seen: set[tuple[float, float, float, float]] = set()
        for obj in page.get_objects(max_depth=10):
            if obj.type not in (_PATH, _IMAGE):
                continue
            box = self._page_bbox(obj)
            if box is None:
                continue
            # 裁到页面内：出血图、带偏移的 XObject 会略微超出页面
            left = max(box[0], 0.0)
            bottom = max(box[1], 0.0)
            right = min(box[2], page_width)
            top = min(box[3], page_height)
            w, h = right - left, top - bottom
            if w < 3 or h < 3:
                continue                      # 细线：表格横线 / 下划线
            if w * h > page_area * 0.9:
                continue                      # 几乎整页 → 版面底纹，不是图
            key = (round(left), round(bottom), round(right), round(top))
            if key in seen:
                continue                      # 同一 Form 里的多张图会给出同一个框
            seen.add(key)
            primitives.append((left, bottom, right, top))
        return primitives

    def _is_scanned_page(self, page, page_width: float, page_height: float) -> bool:
        """这一页是不是「整页就是一张位图」（扫描页 / 影印课本）。

        判据只看对象构成，不看文字量：扫描版的 OCR 文字层也会有几百个 text 对象，
        用「文字少」判会漏。arXiv 论文的插图是页内小图，不会有一张图盖住整页。
        """
        page_area = page_width * page_height
        if page_area <= 0:
            return False
        for obj in page.get_objects(max_depth=10):
            if obj.type != _IMAGE:
                continue
            box = self._page_bbox(obj)
            if box is None:
                continue
            w = min(box[2], page_width) - max(box[0], 0.0)
            h = min(box[3], page_height) - max(box[1], 0.0)
            if w > 0 and h > 0 and w * h >= page_area * _SCANNED_IMAGE_RATIO:
                return True
        return False

    def _resolve_scanned_box(
        self,
        caption: ParsedBlock,
        page_blocks: list[ParsedBlock],
        page_width: float,
        page_height: float,
    ) -> list[float] | None:
        """扫描页的图区域：以 caption 为锚点，找它上/下方的空白带。

        扫描页没有可用的矢量基元（整页就一张位图），唯一的结构信息是
        OCR 文字层的位置。所以：图注在一侧，图在另一侧，图的高度＝
        「图注到最近正文行」的距离。取上下两侧空隙更大的一侧作为图区域。

        坐标全程是 PDF 原生左下原点（y 越大越靠上），返回前统一转左上原点。
        """
        cap = caption.bbox
        others = [
            b
            for b in page_blocks
            if b.type in ("text", "section", "title") and (b.text or "").strip()
        ]

        # 只有「像正文行」的块才能挡住图区域。图内部的标注文字（"P₁"/"e"/"α" 这种
        # 短标签）也在文字层里，如果拿它们当边界，图会被切成一两行高的小条。
        def is_body_line(block: ParsedBlock) -> bool:
            width = block.bbox[2] - block.bbox[0]
            return len((block.text or "").strip()) >= 12 or width >= page_width * 0.5

        blockers = [b for b in others if is_body_line(b)]

        def x_overlap(bb: list[float]) -> bool:
            return min(bb[2], cap[2]) > max(bb[0], cap[0])

        above = [b.bbox[1] for b in blockers if b.bbox[1] >= cap[3] - 1 and x_overlap(b.bbox)]
        below = [b.bbox[3] for b in blockers if b.bbox[3] <= cap[1] + 1 and x_overlap(b.bbox)]

        y_top_limit = (min(above) - _SCAN_PAD) if above else (page_height - _SCAN_MARGIN)
        y_bottom_limit = (max(below) + _SCAN_PAD) if below else _SCAN_MARGIN
        gap_above = y_top_limit - cap[3]
        gap_below = cap[1] - y_bottom_limit

        left = max(min((b.bbox[0] for b in others), default=_SCAN_MARGIN) - 2, 0.0)
        right = min(
            max((b.bbox[2] for b in others), default=page_width - _SCAN_MARGIN) + 2,
            page_width,
        )
        if right - left < 40:  # 这一页几乎没有文字可参照，退回页边距
            left, right = _SCAN_MARGIN, page_width - _SCAN_MARGIN

        # 选哪一侧放图：**有正文挡住的那一侧**才更可能是真图区域（另一侧只有页边距）。
        # 但优先侧也可能不合法（预算太小/切到正文），所以两侧都试，优先侧先来。
        if above and below:
            prefer_above = gap_above >= gap_below
        elif above:
            prefer_above = True
        elif below:
            prefer_above = False
        else:
            prefer_above = gap_above >= gap_below

        def candidate_above() -> list[float] | None:
            if gap_above < _SCAN_MIN_HEIGHT:
                return None
            return [left, cap[3] + _SCAN_PAD, right, y_top_limit]

        def candidate_below() -> list[float] | None:
            if gap_below < _SCAN_MIN_HEIGHT:
                return None
            return [left, y_bottom_limit, right, cap[1] - _SCAN_PAD]

        makers = (candidate_above, candidate_below) if prefer_above else (candidate_below, candidate_above)
        for make in makers:
            box = make()
            if box is None:
                continue
            validated = self._validate_scanned_box(box, page_width, page_height, others)
            if validated is not None:
                return validated
        return None

    @staticmethod
    def _validate_scanned_box(
        box: list[float],
        page_width: float,
        page_height: float,
        text_blocks: list[ParsedBlock],
    ) -> list[float] | None:
        """扫描页候选图框的三道否决。坐标是 PDF 原生左下原点。"""
        # 页眉/页脚不属于图：页边 4% 内一律裁掉（课本页眉常印「第10章 带传动与链传动 209」）
        box[1] = max(box[1], page_height * 0.04)
        box[3] = min(box[3], page_height * 0.96)
        if box[3] - box[1] < _SCAN_MIN_HEIGHT:
            return None

        area = (box[2] - box[0]) * (box[3] - box[1])
        if area < page_width * page_height * _SCAN_MIN_AREA_RATIO:
            return None
        # 红线：不许把整页当一张「图」。真到这一步说明这一页的版面没法切，宁可不给。
        if box[3] - box[1] > page_height * 0.92:
            return None
        # 带里大半是正文行 → 这是标题/段落，不是图（课本里「10.4.5 张紧装置设计」这种节奏）
        if _text_cover_ratio_native(box, text_blocks) > _SCAN_MAX_TEXT_COVER:
            return None
        # 带里只有一个「7.7 热管简介」这样的章节编号行 → 那是节标题，不是图。
        # 光看文字覆盖率判不出来（标题行很短），所以按编号形态再挡一道。
        inside = [
            b
            for b in text_blocks
            if box[0] <= (b.bbox[0] + b.bbox[2]) / 2 <= box[2]
            and box[1] <= (b.bbox[1] + b.bbox[3]) / 2 <= box[3]
        ]
        if any(_SECTION_HEAD_RE.match((b.text or "").strip()) for b in inside):
            return None
        return box

    @staticmethod
    def _page_bbox(obj) -> tuple[float, float, float, float] | None:
        """取对象在**页面坐标系**下的 bbox。

        pypdfium2 的坑：图像对象的 ``get_bounds()`` 是**图像像素坐标**
        （一张 1652x606 的 PNG 会返回 (0,0,1652,606)），不是页面位置。
        真正的位置在它所属的 Form XObject（type=5）上。所以嵌套对象要一路
        爬到最外层容器，用容器的 bounds。
        """
        node = obj
        guard = 0
        while getattr(node, "level", 0) and node.container is not None and guard < 5:
            node = node.container
            guard += 1
        try:
            left, bottom, right, top = node.get_bounds()
        except Exception:  # noqa: BLE001
            return None
        return (left, bottom, right, top)

    def _resolve_figure_box(
        self,
        caption: ParsedBlock,
        primitives: list[tuple[float, float, float, float]],
        page_width: float,
        page_height: float,
        used: set[int],
        text_blocks: list[ParsedBlock],
    ) -> list[float] | None:
        """以 caption 为锚点，向最近的一族绘图基元生长，返回图区域。

        返回的是 **PDF 原生左下原点**坐标；调用方在出口统一转成左上原点
        （见 ``_to_top_left``）。几何判断一律在原生坐标里做，因为 caption、
        正文块、绘图基元都来自同一套坐标系。

        ``used`` 记录已经被别的 caption 占掉的基元：同一页两张图不能长到同一块。

        ``text_blocks`` 用来做**文字密度否决**：有些 PDF 把正文也画成 path
        （Type3 字体、文字底纹），光看绘图基元会把整段正文认成图。
        我们比较「框内正文行覆盖面积 / 框面积」，超过阈值就说明这更像正文。
        """
        page_area = page_width * page_height
        cap = caption.bbox

        for direction in ("above", "below"):
            candidates: list[tuple[int, tuple[float, float, float, float]]] = []
            for idx, p in enumerate(primitives):
                if idx in used:
                    continue
                if min(p[2], cap[2]) <= max(p[0], cap[0]):
                    continue  # 水平方向完全不重叠
                if direction == "above":
                    if p[3] <= cap[1] + 2 and cap[1] - p[3] <= 500:
                        candidates.append((idx, p))
                else:
                    if p[1] >= cap[3] - 2 and p[1] - cap[3] <= 500:
                        candidates.append((idx, p))
            if not candidates:
                continue

            if direction == "above":
                candidates.sort(key=lambda item: -item[1][3])   # 由近及远（越往上越远）
                group = [candidates[0]]
                frontier = candidates[0][1][1]
                for item in candidates[1:]:
                    if item[1][3] >= frontier - self.merge_gap:
                        group.append(item)
                        frontier = min(frontier, item[1][1])
                    else:
                        break
            else:
                candidates.sort(key=lambda item: item[1][1])
                group = [candidates[0]]
                frontier = candidates[0][1][3]
                for item in candidates[1:]:
                    if item[1][1] <= frontier + self.merge_gap:
                        group.append(item)
                        frontier = max(frontier, item[1][3])
                    else:
                        break

            boxes = [p for _, p in group]
            box = [
                max(min(p[0] for p in boxes), 0.0),
                max(min(p[1] for p in boxes) - 2, 0.0),
                min(max(p[2] for p in boxes), page_width),
                min(max(p[3] for p in boxes) + 2, page_height),
            ]
            width = box[2] - box[0]
            height = box[3] - box[1]
            area = width * height
            if area < page_area * self.min_figure_area_ratio:
                continue
            if height > page_height * 0.9:
                continue
            if _text_cover_ratio(box, text_blocks) > 0.30:
                continue  # 框里大半是正文行 —— 这是正文，不是图
            used.update(idx for idx, _ in group)
            return [round(v, 1) for v in box]
        return None

    def _attach_images(
        self,
        page,
        figures: list[ParsedFigure],
        page_no: int,
        page_width: float,
        page_height: float,
    ) -> None:
        """把图区域渲染成 PNG（矢量图也能拿到，这是关键）。实现见 ``adapters/render.py``。"""
        targets = [f for f in figures if f.page == page_no and f.image_bytes is None]
        if not targets:
            return
        page_image = render_page(page, self.render_dpi)
        if page_image is None:
            return
        for fig in targets:
            fig.image_bytes = crop_to_png(page_image, fig.bbox, self.render_dpi)


# ----------------------------------------------------------------------
def _text_cover_ratio_native(box: list[float], text_blocks: list[ParsedBlock]) -> float:
    """框内正文块的覆盖比例（PDF 原生左下原点）。

    和 ``_text_cover_ratio`` 的区别：那个按行覆盖算，轻量档的扫描页只需要
    「这块区域是不是被文字占满了」这一个判断，所以直接算交集面积和。
    """
    bw = box[2] - box[0]
    bh = box[3] - box[1]
    if bw <= 0 or bh <= 0:
        return 0.0
    covered = 0.0
    for block in text_blocks:
        bb = block.bbox
        iw = min(box[2], bb[2]) - max(box[0], bb[0])
        ih = min(box[3], bb[3]) - max(box[1], bb[1])
        if iw > 0 and ih > 0:
            covered += iw * ih
    return covered / (bw * bh)


def _to_top_left(box: list[float], page_height: float) -> list[float]:
    """PDF 原生左下原点 → 左上原点。

    pypdfium2 的 charbox / get_bounds 都是 PDF 原生坐标系（y 向上）。
    而裁剪（PIL）、PDF.js overlay、golden set 全都按左上原点用。
    这个转换必须在**出口**做：内部按原生坐标算几何，对外一律左上原点。
    """
    x0, y0, x1, y1 = box
    return [
        round(x0, 1),
        round(page_height - y1, 1),
        round(x1, 1),
        round(page_height - y0, 1),
    ]


def _box_iou(a: list[float], b: list[float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    inter_w = min(ax1, bx1) - max(ax0, bx0)
    inter_h = min(ay1, by1) - max(ay0, by0)
    if inter_w <= 0 or inter_h <= 0:
        return 0.0
    inter = inter_w * inter_h
    union = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return inter / union if union > 0 else 0.0


def _make_line(items: list[tuple[str, float, float, float, float]]) -> Line:
    items = sorted(items, key=lambda c: c[1])
    widths = [c[3] - c[1] for c in items if c[3] > c[1]]
    avg_width = sum(widths) / len(widths) if widths else 5.0
    parts: list[str] = []
    prev_right: float | None = None
    for ch, left, _bottom, right, _top in items:
        if (
            prev_right is not None
            and ch not in " \t"
            and parts
            and parts[-1] not in " \t"
            and left - prev_right > avg_width * 0.5
        ):
            parts.append(" ")
        parts.append(ch)
        prev_right = right
    return Line(
        text="".join(parts),
        x0=min(c[1] for c in items),
        y0=min(c[2] for c in items),
        x1=max(c[3] for c in items),
        y1=max(c[4] for c in items),
        size=max(c[4] for c in items) - min(c[2] for c in items),
    )


def _label_from_text(text: str) -> tuple[str, str] | None:
    match = LABEL_RE.match(text.strip())
    if not match:
        return None
    normalized = normalize_label(match.group(0))
    if not normalized:
        return None
    return match.group(0).strip(), normalized


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def _text_cover_ratio(box: list[float], text_blocks: list[ParsedBlock]) -> float:
    """框内被正文行覆盖的面积比例。

    正文行的 bbox 基本铺满栏宽，而图里只有零星的坐标轴标签，
    所以这个比例能有效区分「图区域」和「把正文当成图」。
    """
    area = max(box[2] - box[0], 0.0) * max(box[3] - box[1], 0.0)
    if area <= 0:
        return 0.0
    covered = 0.0
    for block in text_blocks:
        b = block.bbox
        ix = min(box[2], b[2]) - max(box[0], b[0])
        iy = min(box[3], b[3]) - max(box[1], b[1])
        if ix > 0 and iy > 0:
            covered += ix * iy
    return min(covered / area, 1.0)


def _pdfium_version() -> str:
    try:
        import importlib.metadata as md

        return md.version("pypdfium2")
    except Exception:  # noqa: BLE001
        return "unknown"


__all__ = ["PdfiumParser"]
