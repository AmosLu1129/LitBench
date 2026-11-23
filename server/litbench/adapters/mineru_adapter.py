# SPDX-License-Identifier: AGPL-3.0-or-later
r"""MinerU 适配器（AGPL，**只能走独立子进程**）。

许可证纪律（`docs/02`）：MinerU 是 AGPL-3.0，主进程绝不能 import 它。
这里的做法是：把 PDF 复制到 ASCII 工作目录，用独立 venv 里的 ``mineru-kit`` CLI
解析成 ``middle_json``，我们只读它产出的 JSON。主镜像里既没有 MinerU 的代码，
也没有它的依赖。

为什么必须 ASCII 路径：MinerU 的解析层和 docling-parse 一样是 C++ 实现，
非 ASCII 路径会直接打不开文件（见 docs/09 §11）。所以输入先复制到
``MINERU_WORKDIR/input/<sha>.pdf``，输出也写在同一棵 ASCII 目录下。

输出协议：DocVortex ``docvortex.middle`` v2（`schema` + `schema_version` 两个键）。
页号 ``page_idx`` 从 0 开始，和 CLI 的 PDF 页码（1 起）不同，这里统一 +1。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from ..config import settings
from ..labels import LABEL_RE, looks_like_caption, normalize_label
from .base import (
    ParsedBlock,
    ParsedDocument,
    ParsedFigure,
    ParsedTable,
    ParserError,
)

# DocVortex 的块类型 → 本项目的块类型
_TYPE_MAP = {
    "text": "text",
    "paragraph": "text",
    "list": "text",
    "list_item": "text",
    "title": "title",
    "doc_title": "title",
    "paragraph_title": "section",
    "section_header": "section",
    "heading": "section",
    "caption": "caption",
    "table_caption": "caption",
    "figure_caption": "caption",
    "image_caption": "caption",
    "equation": "formula",
    "formula": "formula",
    "interline_equation": "formula",
    "footer": "footer",
    "page_footer": "footer",
    "header": "header",
    "page_header": "header",
}

_FIGURE_TYPES = {"image", "figure", "picture", "chart"}
_TABLE_TYPES = {"table"}

# DocVortex 把图注/表题挂在图块**自己**的 content 里（``image_caption`` /
# ``chart_caption`` / ``table_caption`` 子块），不像 Docling 那样单独给 caption 块。
# 不往这里收，等于把整本书的图注全丢掉——整章 104 张图只有 6 张能对上号就是这么来的。
_CAPTION_CHILD_TYPES = {
    "caption",
    "image_caption",
    "chart_caption",
    "figure_caption",
    "picture_caption",
    "table_caption",
}
_TABLE_BODY_CHILD_TYPES = {"table_body", "table"}


class MineruParser:
    """通过 CLI 子进程调用 MinerU（AGPL 隔离）。"""

    name = "mineru"

    def __init__(
        self,
        *,
        cmd: str | None = None,
        workdir: Path | None = None,
        model_source: str | None = None,
        timeout_s: int | None = None,
        pages: str = "all",
        render_images: bool = True,
        render_dpi: int = 150,
        tier: str | None = None,
    ) -> None:
        self.cmd = cmd or settings.mineru_cmd
        self.workdir = Path(workdir or settings.mineru_workdir)
        self.model_source = model_source or settings.mineru_model_source
        self.timeout_s = timeout_s or settings.mineru_timeout_s
        self.pages = pages
        # 默认用配置档位（生产是 flash：只要文本）。评估脚本会显式传 standard
        # 来拿结构化表格（M9.3b）；不要把 standard 设成默认，它要下 VLM 权重。
        self.tier = tier or settings.mineru_tier
        self.home = Path(settings.mineru_home)
        self.render_images = render_images
        self.render_dpi = render_dpi

    async def parse(self, pdf_path: Path) -> ParsedDocument:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._parse_sync, Path(pdf_path))

    # ------------------------------------------------------------------
    def _parse_sync(self, pdf_path: Path) -> ParsedDocument:  # pragma: no cover — 真跑 CLI
        if not pdf_path.is_file():
            raise ParserError(f"文件不存在：{pdf_path}")
        src = self._stage_input(pdf_path)
        outdir = self.workdir / "out" / src.stem
        if outdir.exists():
            shutil.rmtree(outdir, ignore_errors=True)
        outdir.mkdir(parents=True, exist_ok=True)

        cmd = [
            self.cmd,
            "parse",
            str(src),
            "--format",
            "middle_json",
            "-o",
            str(outdir),
            "--pages",
            self.pages,
            "--tier",
            self.tier,
        ]
        env = os.environ.copy()
        env["MINERU_MODEL_SOURCE"] = self.model_source
        env["MINERU_HOME"] = str(self.home)
        # 子进程只能写工作区内的路径：把临时目录也钉进去
        tmp = self.workdir / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        env["TEMP"] = str(tmp)
        env["TMP"] = str(tmp)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            proc = subprocess.run(  # noqa: S603 — 参数是我们自己拼的，不经过 shell
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
                env=env,
                cwd=str(self.workdir),
            )
        except FileNotFoundError as exc:
            raise ParserError(f"MinerU CLI 不存在：{self.cmd}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ParserError(f"MinerU 超时（{self.timeout_s}s）") from exc

        if proc.returncode != 0:
            tail = ((proc.stderr or "") + (proc.stdout or ""))[-1200:]
            raise ParserError(f"MinerU 退出码 {proc.returncode}：{tail}")

        payload = _find_middle_json(outdir)
        data = json.loads(payload.read_text(encoding="utf-8"))
        doc = to_parsed_document(
            data,
            render_images=self.render_images,
            source_pdf=pdf_path,
            render_dpi=self.render_dpi,
        )
        if doc.engine_version == "unknown":
            doc.engine_version = _mineru_version(data)
        return doc

    def _stage_input(self, pdf_path: Path) -> Path:
        """复制到 ASCII 工作目录（MinerU 解析层打不开非 ASCII 路径）。"""
        data = pdf_path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()[:16]
        target = self.workdir / "input" / f"{digest}.pdf"
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return target


# ----------------------------------------------------------------------
def _find_middle_json(outdir: Path) -> Path:
    files = sorted(outdir.rglob("*.json"))
    if not files:
        raise ParserError(f"MinerU 没有产出 JSON：{outdir}")
    for path in files:
        if "middle" in path.name.lower():
            return path
    return files[0]


def _mineru_version(data: dict) -> str:
    try:
        return str(data["metadata"]["producer"]["version"])
    except Exception:  # noqa: BLE001
        return "unknown"


def _flatten_content(node) -> str:  # noqa: ANN001 — content 可能是 str / dict / 嵌套 list
    """把 DocVortex 的 content 递归展平成纯文本。

    content 的形态在不同块类型间不统一：文本块是 ``[{"type":"text","content":"..."}]``，
    表格/公式块里 ``content`` 还可能再嵌一层 list。不递归展平就会静默丢字。
    """
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_flatten_content(item) for item in node)
    if isinstance(node, dict):
        text = node.get("content")
        if text is None:
            text = node.get("text")
        if text is None:
            return ""
        return _flatten_content(text)
    return ""


def _block_text(block: dict) -> str:
    """把 DocVortex 的 content spans 拼成纯文本。"""
    content = block.get("content")
    if content is not None:
        return _flatten_content(content)
    text = block.get("text") or ""
    return text if isinstance(text, str) else ""


def _child_blocks(block: dict):
    """遍历块内嵌套的子块（DocVortex 的图注、表体都在这里）。"""
    for child in block.get("content") or []:
        if isinstance(child, dict):
            yield child


def _figure_caption(
    block: dict, page_size: tuple[float, float] | None = None
) -> tuple[str | None, list[float] | None]:
    """取图块内嵌的图注文本与图注区域 bbox（多行用换行连接）。

    真实输出里一张图可能挂多条 caption（主图注 + "(a) ...(b) ..." 分图说明），
    全部留着；图号从整段里抽。bbox 是各 caption 子块的并集（pt），
    第二套 OCR 只裁这一小块做交叉校核。
    """
    parts: list[str] = []
    boxes: list[list[float]] = []
    for child in _child_blocks(block):
        kind = str(child.get("type") or "").lower()
        if kind not in _CAPTION_CHILD_TYPES:
            continue
        text = _block_text(child).strip()
        if not text:
            continue
        parts.append(text)
        box = _block_bbox(child, page_size)
        if box:
            boxes.append(box)
    text = "\n".join(parts) if parts else None
    bbox = None
    if boxes:
        bbox = [
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        ]
    return text, bbox


def _table_parts(block: dict) -> tuple[str | None, str | None]:
    """表块拆成 ``(表题, 表体文本)``。flash 档只给 markdown 形态的表体，没有 HTML。"""
    captions: list[str] = []
    bodies: list[str] = []
    for child in _child_blocks(block):
        kind = str(child.get("type") or "").lower()
        text = _block_text(child).strip()
        if not text:
            continue
        if kind in _CAPTION_CHILD_TYPES:
            captions.append(text)
        elif kind in _TABLE_BODY_CHILD_TYPES:
            bodies.append(text)
    return (
        "\n".join(captions) if captions else None,
        "\n".join(bodies) if bodies else None,
    )


def _block_bbox(
    block: dict, page_size: tuple[float, float] | None = None
) -> list[float] | None:
    """取块的 bbox，统一成 [x0,y0,x1,y1]（左上原点，pt）。

    两个坑：

    * 字段名在不同版本里可能是 ``bbox`` / ``geom.bbox`` / ``position.bbox``，都认；
    * **DocVortex 的 bbox 是归一化 [0,1] 坐标**（不是 pt），要乘页面宽高。
      判据：四个值都落在 [0,1] 且知道页尺寸 → 归一化；否则按 pt 处理（兼容老版本/测试）。
    """
    value = None
    for key in ("bbox", "geom", "position", "box"):
        value = block.get(key)
        if isinstance(value, dict):
            value = value.get("bbox") or value.get("box")
        if isinstance(value, (list, tuple)) and len(value) == 4:
            break
        value = None
    if value is None:
        return None
    try:
        coords = [float(v) for v in value]
    except (TypeError, ValueError):
        return None

    if page_size and all(-0.01 <= v <= 1.01 for v in coords):
        w, h = page_size
        if w > 0 and h > 0:
            return [
                round(coords[0] * w, 1),
                round(coords[1] * h, 1),
                round(coords[2] * w, 1),
                round(coords[3] * h, 1),
            ]
    return coords


def _page_sizes(data: dict) -> dict[int, tuple[float, float]]:
    sizes: dict[int, tuple[float, float]] = {}
    layout = (data.get("extensions") or {}).get("docvortex_layout") or {}
    for page in layout.get("pages") or []:
        try:
            idx = int(page.get("page_idx", 0))
            w = float(page.get("width_pt") or page.get("width") or 0)
            h = float(page.get("height_pt") or page.get("height") or 0)
        except (TypeError, ValueError):
            continue
        if w > 0 and h > 0:
            sizes[idx] = (w, h)
    return sizes


def _bind_caption(figure: ParsedFigure, captions: list[ParsedBlock], used: set[int]) -> None:
    """给没有图注的图绑最近的图注块（同页、水平有重叠、垂直距离最近）。"""
    best: tuple[float, int] | None = None
    for idx, cap in enumerate(captions):
        if idx in used or cap.page != figure.page:
            continue
        if min(figure.bbox[2], cap.bbox[2]) <= max(figure.bbox[0], cap.bbox[0]):
            continue
        if figure.bbox[3] <= cap.bbox[1]:
            gap = cap.bbox[1] - figure.bbox[3]
        elif cap.bbox[3] <= figure.bbox[1]:
            gap = figure.bbox[1] - cap.bbox[3]
        else:
            gap = 0.0
        if gap > 90:
            continue
        if best is None or gap < best[0]:
            best = (gap, idx)
    if best is None:
        return
    used.add(best[1])
    cap = captions[best[1]]
    figure.caption = cap.text
    match = LABEL_RE.match(cap.text.strip())
    if match:
        norm = normalize_label(match.group(0))
        if norm and norm.startswith("fig"):
            figure.label_raw = match.group(0).strip()


def to_parsed_document(
    data: dict,
    *,
    render_images: bool = True,
    source_pdf: Path | None = None,
    render_dpi: int = 150,
) -> ParsedDocument:
    """把 DocVortex ``middle_json`` 映射成本项目的 ParsedDocument。

    纯函数（不跑 CLI、不写盘），所以能用合成的 JSON 单测；真实 CLI 输出只是在字段上校准。
    """
    if not isinstance(data, dict) or "pages" not in data:
        raise ParserError("MinerU 输出不是预期结构（缺 pages）")

    pages = data.get("pages") or []
    sizes = _page_sizes(data)
    blocks: list[ParsedBlock] = []
    figures: list[ParsedFigure] = []
    tables: list[ParsedTable] = []
    order = 0

    for page in pages:
        try:
            page_no = int(page.get("page_idx", 0)) + 1
        except (TypeError, ValueError):
            continue
        page_size = sizes.get(int(page.get("page_idx", -1)))
        for raw in page.get("blocks") or []:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("type") or "").lower()
            bbox = _block_bbox(raw, page_size)
            text = _block_text(raw).strip()

            if kind in _FIGURE_TYPES:
                if bbox:
                    figure = ParsedFigure(
                        page=page_no,
                        bbox=bbox,
                        figure_type="chart" if kind == "chart" else "other",
                    )
                    caption, caption_bbox = _figure_caption(raw, page_size)
                    if caption:
                        figure.caption = caption
                        figure.caption_bbox = caption_bbox
                        match = LABEL_RE.search(caption)
                        norm = normalize_label(match.group(0)) if match else None
                        # 只认图号（fig）；表题的「表 4.1」不能挂到图上
                        if norm and norm.startswith("fig"):
                            figure.label_raw = match.group(0).strip()
                    figures.append(figure)
                continue
            if kind in _TABLE_TYPES:
                if bbox:
                    table_caption, table_body = _table_parts(raw)
                    html = _table_html(raw)
                    if html is None and table_body and table_body.lstrip().startswith("<"):
                        html = table_body
                    tables.append(
                        ParsedTable(
                            page=page_no,
                            bbox=bbox,
                            html=html,
                            cells=[],
                            caption=table_caption or text,
                            quality="structured" if (html or table_body) else "image",
                        )
                    )
                continue

            block_type = _TYPE_MAP.get(kind, "text")
            # MinerU 的 middle_json 不给图注块（flash 档只有 text/title/image 等），
            # 但图注是产品的一等对象，所以按文本形态补一次判定。
            if block_type == "text" and looks_like_caption(text):
                block_type = "caption"
            if not text and block_type not in ("footer", "header"):
                continue
            blocks.append(
                ParsedBlock(
                    page=page_no,
                    bbox=bbox or [0.0, 0.0, 0.0, 0.0],
                    type=block_type,
                    text=text,
                    order_idx=order,
                )
            )
            order += 1

    captions = [b for b in blocks if b.type == "caption"]
    used: set[int] = set()
    for fig in figures:
        # 图块自带图注的，不要再拿正文里碰巧像图注的段落去覆盖它
        if fig.caption:
            continue
        _bind_caption(fig, captions, used)

    if render_images and source_pdf is not None and figures:
        _attach_images(source_pdf, figures, render_dpi)

    total_chars = sum(len(b.text) for b in blocks)
    return ParsedDocument(
        page_count=len(pages),
        engine="mineru",
        engine_version="unknown",
        blocks=blocks,
        figures=figures,
        tables=tables,
        text_layer=total_chars > 100,
    )


def _table_html(block: dict) -> str | None:
    for key in ("html", "table_html", "content_html"):
        value = block.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _attach_images(pdf_path: Path, figures: list[ParsedFigure], dpi: int) -> None:
    """缩略图仍由我们用 pdfium 裁（MinerU 的图片产物不必依赖）。"""
    try:
        from .render import attach_figure_images
    except ImportError:  # pragma: no cover
        return
    try:
        attach_figure_images(pdf_path, figures, dpi=dpi)
    except Exception:  # noqa: BLE001 — 裁图失败不该让整篇解析失败
        return
