# SPDX-License-Identifier: AGPL-3.0-or-later
"""解析后过滤：把课本/扫描件里混进来的「独立二维码」当图扔掉。

为什么需要它：中文教材常把「扫码看视频/课件」的二维码印在版面上（一页一个，
红色或黑白，30–60pt 见方）。版面模型把它判成图片块，于是图库里出现一张
「图 10-1（30×30）」——它不是文献内容，还会干扰子图归组和图号继承。

为什么不靠解码：课本二维码又小又糊，``cv2.QRCodeDetector.detectAndDecode`` 在
实测 4 张真二维码上全部解码失败（缩略图只有 62×62），所以判据用**几何**：

* ``detect`` 到的 QR 四边形占整块图片面积的比例（``coverage``）；
* 图块尺寸（pt）与长宽比。

实测（5 本教材 352 张图）：4 张独立二维码 coverage 0.50–0.85、尺寸 ≤60pt；
而「大图里含一个小二维码」的 arXiv 图 coverage 0.01–0.21、尺寸 ≥170pt；
散点图/频谱图被误检时 coverage ≤0.40。两条阈值一起用，误杀风险很低。

许可证：opencv（Apache-2.0）是可选依赖，缺了就静默跳过过滤（不影响解析）。
"""

from __future__ import annotations

import logging

from .adapters.base import ParsedFigure

log = logging.getLogger("litbench.filter")

# 二维码四边形覆盖整块图的比例下限。真二维码块实测 ≥0.50，误检的图 ≤0.40。
QR_MIN_COVERAGE = 0.42
# 独立资源二维码在教材里最大约 60pt；超过 120pt 的图块即使检到 QR 也保留（大图含小码）。
QR_MAX_SIDE_PT = 120.0
QR_MIN_ASPECT = 0.35
QR_MAX_ASPECT = 1.8


def qr_coverage(image_bytes: bytes | None) -> float | None:
    """二维码四边形面积 / 图片面积；未检出或没有 cv2 时返回 ``None``。"""
    if not image_bytes:
        return None
    try:
        import cv2
        import numpy as np
    except Exception:  # noqa: BLE001 — 可选依赖，缺了就不做过滤
        return None
    img = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None or img.size == 0:
        return None
    ok, points = cv2.QRCodeDetector().detect(img)
    if not ok or points is None:
        return None
    pts = points.reshape(-1, 2).astype("float32")
    area = abs(float(cv2.contourArea(pts)))
    if area <= 0:
        return None
    return area / float(img.shape[0] * img.shape[1])


def is_qr_figure(fig: ParsedFigure) -> bool:
    """这张图块本身是不是一个二维码（而不是「图里含二维码」）。"""
    bbox = list(fig.bbox or [])
    if len(bbox) != 4:
        return False
    w = float(bbox[2]) - float(bbox[0])
    h = float(bbox[3]) - float(bbox[1])
    if w <= 0 or h <= 0:
        return False
    if max(w, h) > QR_MAX_SIDE_PT:
        return False
    aspect = w / h
    if not (QR_MIN_ASPECT <= aspect <= QR_MAX_ASPECT):
        return False
    coverage = qr_coverage(fig.image_bytes)
    return coverage is not None and coverage >= QR_MIN_COVERAGE


def drop_qr_figures(figures: list[ParsedFigure]) -> list[ParsedFigure]:
    """就地删掉二维码图块，返回被删掉的列表（给日志/报告用）。"""
    dropped = [fig for fig in figures if is_qr_figure(fig)]
    if dropped:
        dropped_ids = {id(fig) for fig in dropped}
        figures[:] = [fig for fig in figures if id(fig) not in dropped_ids]
    return dropped


__all__ = [
    "QR_MAX_SIDE_PT",
    "QR_MIN_COVERAGE",
    "drop_qr_figures",
    "is_qr_figure",
    "qr_coverage",
]
