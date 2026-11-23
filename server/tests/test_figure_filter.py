# SPDX-License-Identifier: AGPL-3.0-or-later
"""二维码过滤的单测（见 litbench/figure_filter.py）。

判据是几何的（QR 四边形覆盖率 + 图块尺寸），因为课本二维码太小、解码成功率低。
这里用 cv2 自己生成一张二维码当正样本；负样本用白图/大图。
"""

from __future__ import annotations

import pytest

from litbench.adapters.base import ParsedFigure
from litbench.figure_filter import (
    QR_MIN_COVERAGE,
    drop_qr_figures,
    is_qr_figure,
    qr_coverage,
)

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")


def qr_png(text: str = "https://example.com/litbench") -> bytes:
    """生成一张带 quiet zone 的二维码 PNG（与课本里 30–60pt 的小图同构）。"""
    matrix = cv2.QRCodeEncoder_create().encode(text)
    matrix = (matrix > 0).astype("uint8") * 255
    matrix = np.pad(matrix, 4, constant_values=255)
    img = cv2.resize(matrix, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def white_png(size: int = 240) -> bytes:
    ok, buf = cv2.imencode(".png", np.full((size, size), 255, dtype="uint8"))
    assert ok
    return buf.tobytes()


def fig(bbox: list[float], image: bytes | None) -> ParsedFigure:
    return ParsedFigure(page=1, bbox=bbox, image_bytes=image)


def test_qr_only_figure_is_detected() -> None:
    png = qr_png()
    coverage = qr_coverage(png)
    assert coverage is not None and coverage >= QR_MIN_COVERAGE
    assert is_qr_figure(fig([10, 10, 50, 50], png))


def test_blank_and_missing_images_are_kept() -> None:
    assert qr_coverage(None) is None
    assert not is_qr_figure(fig([10, 10, 50, 50], white_png()))
    assert not is_qr_figure(fig([10, 10, 50, 50], None))


def test_large_figure_containing_qr_is_kept() -> None:
    """大图里含一个小二维码（arXiv 图常见）：图块本身不能删。"""
    png = qr_png()
    assert not is_qr_figure(fig([0, 0, 400, 500], png))


def test_wide_or_tall_block_is_kept() -> None:
    png = qr_png()
    assert not is_qr_figure(fig([0, 0, 20, 120], png))  # 长宽比 0.17，不像二维码
    assert not is_qr_figure(fig([0, 0, 120, 20], png))


def test_drop_only_qr_figures() -> None:
    qr = fig([10, 10, 50, 50], qr_png())
    plot = fig([10, 100, 300, 260], white_png(300))
    figures = [qr, plot]
    dropped = drop_qr_figures(figures)
    assert dropped == [qr]
    assert figures == [plot]
