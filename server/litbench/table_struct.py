# SPDX-License-Identifier: AGPL-3.0-or-later
"""表格结构抽取与一致性指标（M9.3b，2026-11-15）。

M9.3a 解决「表格 → LaTeX」，本模块解决另一个问题：**两个解析后端对同一张表
的结构还原到底差多少**。只做测量，不接管导出；LaTeX 仍由 ``table_latex.py``。

口径（写清楚，免得把数字当质量）：

* 只认 HTML 里第一张 ``<table>``，``colspan/rowspan`` 允许 1..50；
* 结构指标：原始行数/展开后行高宽、原始单元格数、合并单元格数；
* 内容指标：3-gram shingle F1（顺序不敏感，与 ``docs/11`` 的 OCR 对比同一口径）；
* 位置指标：两表按同页 bbox 匹配后，逐 (行,列) 比较是否被占用、文本是否一致；
* 归一化：NFKC + 折叠空白 + 小写；不做同义词/公式语义归一。

诚实边界：这些数字只说明「两个解析器是否互相一致」，**不等于「谁更接近真值」**。
表格真值需要人工标注，M9.2 的 50 张 bbox 集是图框，不是表格。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser

_SPACE = re.compile(r"\s+")
_MAX_SPAN = 50
_MAX_CELLS = 5000


class TableStructError(ValueError):
    """HTML 不是一张可解析的表格（没有 <table>、或单元格超限）。"""


@dataclass
class Cell:
    text: str = ""
    colspan: int = 1
    rowspan: int = 1
    header: bool = False


@dataclass
class Grid:
    """HTML 里的第一张表，保留原始行（含 span），不做展开。"""

    rows: list[list[Cell]] = field(default_factory=list)
    caption: str = ""
    truncated: bool = False


@dataclass
class ExpandedGrid:
    """把 span 摊平后的网格；同一个锚点覆盖的所有格子文本相同。"""

    height: int
    width: int
    texts: list[list[str]]
    occupied: list[list[bool]]
    origins: list[list[bool]]
    colspans: int
    rowspans: int


class _TableParser(HTMLParser):
    """只抽第一张 table；畸形 HTML 尽量容错，不抛异常（超限除外）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.grid = Grid()
        self._table_seen = False
        self._done = False
        self._row: list[Cell] | None = None
        self._cell: Cell | None = None
        self._in_caption = False
        self._cell_count = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001 — HTMLParser 签名
        tag = tag.lower()
        if self._done:
            return
        if tag == "table":
            if self._table_seen:
                self.grid.truncated = True
                self._done = True
                return
            self._table_seen = True
            return
        if not self._table_seen:
            return
        if tag == "caption":
            self._in_caption = True
            return
        if tag == "tr":
            self._finish_row()
            self._row = []
            return
        if tag in ("td", "th"):
            self._finish_cell()
            if self._row is None:
                self._row = []
            self._cell_count += 1
            if self._cell_count > _MAX_CELLS:
                raise TableStructError("表格单元格过多（>5000），拒绝比较")
            attr = dict(attrs)
            self._cell = Cell(
                colspan=self._span(attr.get("colspan")),
                rowspan=self._span(attr.get("rowspan")),
                header=(tag == "th"),
            )
            return
        if tag == "br" and self._cell is not None:
            self._cell.text += " "

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._done:
            return
        if tag == "caption":
            self._in_caption = False
            return
        if tag in ("td", "th"):
            self._finish_cell()
            return
        if tag == "tr":
            self._finish_row()
            return
        if tag == "table":
            self._finish_row()
            self._done = True

    def handle_data(self, data: str) -> None:
        if not self._table_seen or self._done:
            return
        if self._in_caption:
            self.grid.caption += data
        elif self._cell is not None:
            self._cell.text += data

    def close(self) -> None:
        super().close()
        self._finish_row()
        self._finish_cell()

    def _finish_cell(self) -> None:
        if self._cell is None:
            return
        if self._row is None:
            self._row = []
        self._row.append(
            Cell(
                text=_SPACE.sub(" ", self._cell.text).strip(),
                colspan=self._cell.colspan,
                rowspan=self._cell.rowspan,
                header=self._cell.header,
            )
        )
        self._cell = None

    def _finish_row(self) -> None:
        self._finish_cell()
        if self._row:
            self.grid.rows.append(self._row)
        self._row = None

    @staticmethod
    def _span(raw: str | None) -> int:
        try:
            value = int(str(raw or "1").strip())
        except ValueError:
            return 1
        return max(1, min(value, _MAX_SPAN))


def parse_table_grid(html: str | None) -> Grid | None:
    """HTML → Grid；没有 <table> 或 HTML 为空时返回 None。

    注意：Docling 的 markdown 兜底（``| a | b |``）不算结构化表格，这里也返回 None，
    调用方应把 ``has_html`` 和 ``has_grid`` 分开记。
    """
    if not html or "<table" not in html.lower():
        return None
    parser = _TableParser()
    try:
        parser.feed(html)
        parser.close()
    except TableStructError:
        raise
    except Exception:  # noqa: BLE001 — 第三方 HTML 千奇百怪，解析失败按「没有结构」处理
        return None
    if not parser.grid.rows:
        return None
    parser.grid.caption = _SPACE.sub(" ", parser.grid.caption).strip()
    return parser.grid


def expand_grid(grid: Grid) -> ExpandedGrid:
    """把 rowspan/colspan 摊平成矩形网格。

    行优先放置：遇到已被上方 rowspan 占用的列就向右跳；文本按锚点复制到覆盖的
    所有格子（比较文本时只看锚点，``origins`` 标出锚点位置）。
    """
    occupied: dict[tuple[int, int], tuple[str, bool]] = {}
    colspans = 0
    rowspans = 0
    for row_index, row in enumerate(grid.rows):
        column = 0
        for cell in row:
            while (row_index, column) in occupied:
                column += 1
            for dr in range(cell.rowspan):
                for dc in range(cell.colspan):
                    occupied[(row_index + dr, column + dc)] = (
                        cell.text,
                        dr == 0 and dc == 0,
                    )
            if cell.colspan > 1:
                colspans += 1
            if cell.rowspan > 1:
                rowspans += 1
            column += cell.colspan

    if not occupied:
        return ExpandedGrid(0, 0, [], [], [], 0, 0)
    height = max(r for r, _ in occupied) + 1
    width = max(c for _, c in occupied) + 1
    texts = [["" for _ in range(width)] for _ in range(height)]
    occupancy = [[False for _ in range(width)] for _ in range(height)]
    origins = [[False for _ in range(width)] for _ in range(height)]
    for (row, column), (text, is_origin) in occupied.items():
        texts[row][column] = text
        occupancy[row][column] = True
        origins[row][column] = is_origin
    return ExpandedGrid(
        height=height,
        width=width,
        texts=texts,
        occupied=occupancy,
        origins=origins,
        colspans=colspans,
        rowspans=rowspans,
    )


def _norm(text: str) -> str:
    """NFKC + 折叠空白 + 小写；单元格内空白直接去掉（``表 1`` 与 ``表1`` 算同一个）。"""
    normalized = unicodedata.normalize("NFKC", text or "")
    return _SPACE.sub("", normalized).lower()


def _shingles(text: str, n: int = 3) -> Counter[str]:
    cleaned = _norm(text)
    if not cleaned:
        return Counter()
    if len(cleaned) < n:
        return Counter({cleaned: 1})
    return Counter(cleaned[i : i + n] for i in range(len(cleaned) - n + 1))


def shingle_f1(left: str, right: str, n: int = 3) -> float:
    """字符 n-gram shingle F1（0~1）；两边都为空时记 1.0（空表对空表算一致）。"""
    a = _shingles(left, n)
    b = _shingles(right, n)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    overlap = sum((a & b).values())
    precision = overlap / sum(a.values())
    recall = overlap / sum(b.values())
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def table_stats(grid: Grid) -> dict:
    """单张表的结构统计；用于报告，不参与相似度计算。"""
    expanded = expand_grid(grid)
    raw_cells = sum(len(row) for row in grid.rows)
    empty = sum(1 for row in grid.rows for cell in row if not cell.text.strip())
    chars = sum(len(cell.text) for row in grid.rows for cell in row)
    return {
        "rows": len(grid.rows),
        "height": expanded.height,
        "cols": expanded.width,
        "cells": raw_cells,
        "empty_cells": empty,
        "colspans": expanded.colspans,
        "rowspans": expanded.rowspans,
        "chars": chars,
        "caption_chars": len(grid.caption),
    }


def _flatten(expanded: ExpandedGrid) -> str:
    """锚点文本按行优先拼接；span 覆盖的重复文本不重复计入。"""
    parts: list[str] = []
    for row in range(expanded.height):
        for column in range(expanded.width):
            if expanded.origins[row][column]:
                text = expanded.texts[row][column]
                if text:
                    parts.append(text)
    return " ".join(parts)


def _occupied_cells(expanded: ExpandedGrid) -> set[tuple[int, int]]:
    return {
        (row, column)
        for row in range(expanded.height)
        for column in range(expanded.width)
        if expanded.occupied[row][column]
    }


def compare_grids(left: Grid, right: Grid) -> dict:
    """比较两张表的网格结构与内容，返回可直接进 JSON 的指标。"""
    a = expand_grid(left)
    b = expand_grid(right)
    cells_a = _occupied_cells(a)
    cells_b = _occupied_cells(b)
    union = cells_a | cells_b
    common = cells_a & cells_b

    same_columns = a.width == b.width
    same_rows = a.height == b.height
    exact = 0
    if common:
        exact = sum(
            1
            for row, column in common
            if _norm(a.texts[row][column]) == _norm(b.texts[row][column])
        )
    span_equal = (a.colspans, a.rowspans) == (b.colspans, b.rowspans)
    return {
        "rows_left": a.height,
        "rows_right": b.height,
        "cols_left": a.width,
        "cols_right": b.width,
        "same_dims": same_rows and same_columns,
        "same_spans": span_equal,
        "grid_iou": round(len(common) / len(union), 4) if union else 0.0,
        "cell_exact": round(exact / len(common), 4) if common else 0.0,
        "text_f1": round(shingle_f1(_flatten(a), _flatten(b)), 4),
        "caption_f1": (
            round(shingle_f1(left.caption, right.caption), 4)
            if left.caption and right.caption
            else None
        ),
    }


# ----------------------------------------------------------------------
# bbox 匹配：两个后端给的表区域不一定完全重合（一个含表题、一个不含），
# 所以 IoU 之外再加一个「交集 / 较小面积」，避免漏配。
def _area(bbox: tuple[float, float, float, float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _intersection(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    return width * height


def bbox_iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    inter = _intersection(a, b)
    union = _area(a) + _area(b) - inter
    return inter / union if union > 0 else 0.0


def bbox_overlap_min(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> float:
    inter = _intersection(a, b)
    smaller = min(_area(a), _area(b))
    return inter / smaller if smaller > 0 else 0.0


def match_tables(
    left: list[tuple[int, tuple[float, float, float, float] | None]],
    right: list[tuple[int, tuple[float, float, float, float] | None]],
    *,
    threshold: float = 0.3,
) -> tuple[list[tuple[int, int, float]], list[int], list[int]]:
    """按 (页, bbox) 贪心匹配两张表列表。

    参数是两个 ``[(page, bbox), ...]``；返回 ``(配对 [(i, j, score)], 左未配, 右未配)``。
    score 取 ``max(IoU, 交集/较小面积)``；bbox 缺失（None 或零面积）的表不参与配对。
    """
    candidates: list[tuple[float, int, int]] = []
    for left_index, (left_page, left_bbox) in enumerate(left):
        if not left_bbox or _area(left_bbox) <= 0:
            continue
        for right_index, (right_page, right_bbox) in enumerate(right):
            if left_page != right_page or not right_bbox or _area(right_bbox) <= 0:
                continue
            score = max(bbox_iou(left_bbox, right_bbox), bbox_overlap_min(left_bbox, right_bbox))
            if score >= threshold:
                candidates.append((score, left_index, right_index))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))

    used_left: set[int] = set()
    used_right: set[int] = set()
    pairs: list[tuple[int, int, float]] = []
    for score, left_index, right_index in candidates:
        if left_index in used_left or right_index in used_right:
            continue
        used_left.add(left_index)
        used_right.add(right_index)
        pairs.append((left_index, right_index, round(score, 3)))
    pairs.sort(key=lambda item: item[0])
    unmatched_left = [index for index in range(len(left)) if index not in used_left]
    unmatched_right = [index for index in range(len(right)) if index not in used_right]
    return pairs, unmatched_left, unmatched_right
