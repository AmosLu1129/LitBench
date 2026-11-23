# SPDX-License-Identifier: AGPL-3.0-or-later
"""M9.3b 表格结构指标：HTML 网格展开与两后端一致性比较。"""

from __future__ import annotations

import pytest

from litbench.table_struct import (
    bbox_iou,
    compare_grids,
    expand_grid,
    match_tables,
    parse_table_grid,
    shingle_f1,
    table_stats,
)


def test_parse_simple_table() -> None:
    html = (
        "<table><caption>Table 1: Results</caption>"
        "<tr><th>A</th><th>B</th></tr>"
        "<tr><td>1</td><td>2</td></tr></table>"
    )
    grid = parse_table_grid(html)
    assert grid is not None
    assert len(grid.rows) == 2
    assert grid.caption == "Table 1: Results"
    assert [cell.text for cell in grid.rows[0]] == ["A", "B"]
    stats = table_stats(grid)
    assert stats["rows"] == 2
    assert stats["cols"] == 2
    assert stats["cells"] == 4


def test_parse_returns_none_for_markdown_and_empty() -> None:
    assert parse_table_grid(None) is None
    assert parse_table_grid("") is None
    assert parse_table_grid("| a | b |\n| - | - |") is None
    assert parse_table_grid("<table></table>") is None


def test_expand_rowspan_colspan() -> None:
    html = (
        "<table>"
        "<tr><td rowspan='2' colspan='2'>A</td><td>B</td></tr>"
        "<tr><td>C</td></tr>"
        "<tr><td>D</td><td>E</td><td>F</td></tr>"
        "</table>"
    )
    grid = parse_table_grid(html)
    assert grid is not None
    expanded = expand_grid(grid)
    assert (expanded.height, expanded.width) == (3, 3)
    # A 覆盖 2x2，锚点只在左上角
    assert expanded.texts[0][0] == "A"
    assert expanded.texts[1][1] == "A"
    assert expanded.origins[0][0] is True
    assert expanded.origins[1][1] is False
    assert expanded.occupied[1][0] is True
    # C 被 rowspan 挤到第 3 列
    assert expanded.texts[1][2] == "C"
    assert expanded.texts[2] == ["D", "E", "F"]
    assert expanded.colspans == 1
    assert expanded.rowspans == 1


def test_shingle_f1() -> None:
    assert shingle_f1("", "") == 1.0
    assert shingle_f1("abc", "") == 0.0
    assert shingle_f1("Table 1 results", "table 1 results") == 1.0
    low = shingle_f1("alpha beta gamma", "完全不同的内容")
    assert low == 0.0
    partial = shingle_f1("accuracy 0.91 on test set", "accuracy 0.91 on train set")
    assert 0.0 < partial < 1.0


def test_compare_identical_tables() -> None:
    html = (
        "<table><caption>Table 2</caption>"
        "<tr><td>Method</td><td>Score</td></tr>"
        "<tr><td>Ours</td><td>0.91</td></tr></table>"
    )
    grid_a = parse_table_grid(html)
    grid_b = parse_table_grid(html)
    assert grid_a is not None and grid_b is not None
    metrics = compare_grids(grid_a, grid_b)
    assert metrics["same_dims"] is True
    assert metrics["same_spans"] is True
    assert metrics["grid_iou"] == 1.0
    assert metrics["cell_exact"] == 1.0
    assert metrics["text_f1"] == 1.0
    assert metrics["caption_f1"] == 1.0


def test_compare_tables_with_different_structure() -> None:
    merged = (
        "<table><tr><td colspan='2'>Header</td></tr>"
        "<tr><td>A</td><td>B</td></tr></table>"
    )
    plain = (
        "<table><tr><td>Header</td><td>Header2</td></tr>"
        "<tr><td>A</td><td>B</td></tr></table>"
    )
    grid_a = parse_table_grid(merged)
    grid_b = parse_table_grid(plain)
    assert grid_a is not None and grid_b is not None
    metrics = compare_grids(grid_a, grid_b)
    assert metrics["same_spans"] is False
    assert metrics["same_dims"] is True
    # 两种写法都铺满 2x2：占位相同，但合并结构和第二格文本不同
    assert metrics["grid_iou"] == 1.0
    assert metrics["cell_exact"] < 1.0


def test_compare_row_count_difference() -> None:
    short = "<table><tr><td>A</td><td>B</td></tr></table>"
    tall = "<table><tr><td>A</td><td>B</td></tr><tr><td>C</td><td>D</td></tr></table>"
    grid_a = parse_table_grid(short)
    grid_b = parse_table_grid(tall)
    assert grid_a is not None and grid_b is not None
    metrics = compare_grids(grid_a, grid_b)
    assert metrics["same_dims"] is False
    assert metrics["grid_iou"] < 1.0
    assert metrics["cell_exact"] == 1.0  # 共有的第一行完全一致


def test_malformed_html_is_tolerated() -> None:
    html = "<table><tr><td>A<td>B<tr><td>C</table>"
    grid = parse_table_grid(html)
    assert grid is not None
    expanded = expand_grid(grid)
    assert (expanded.height, expanded.width) == (2, 2)


def test_parse_rejects_too_many_cells() -> None:
    html = "<table>" + "<tr><td>x</td></tr>" * 5001 + "</table>"
    with pytest.raises(Exception):  # noqa: B017 — 具体是 TableStructError
        parse_table_grid(html)


def test_bbox_and_matching() -> None:
    assert bbox_iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert bbox_iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0
    left = [
        (1, (0.0, 0.0, 100.0, 100.0)),
        (1, (0.0, 200.0, 100.0, 300.0)),
        (2, (0.0, 0.0, 100.0, 100.0)),
    ]
    right = [
        (1, (0.0, 5.0, 100.0, 105.0)),
        (3, (0.0, 0.0, 100.0, 100.0)),
    ]
    pairs, unmatched_left, unmatched_right = match_tables(left, right)
    assert [(i, j) for i, j, _ in pairs] == [(0, 0)]
    assert unmatched_left == [1, 2]
    assert unmatched_right == [1]


def test_matching_skips_missing_bbox() -> None:
    pairs, unmatched_left, unmatched_right = match_tables(
        [(1, None)], [(1, (0.0, 0.0, 10.0, 10.0))]
    )
    assert pairs == []
    assert unmatched_left == [0]
    assert unmatched_right == [0]
