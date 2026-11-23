# SPDX-License-Identifier: AGPL-3.0-or-later
"""按需升级的判据。

轻量档（pdfium）是秒级的，但它在两类版面上一定会漏：

1. **看到了图号、没定位到图**：caption 抽了出来、图区域没锚住（通栏图 + 双栏正文、
   页面大量 path 装饰时最常见）。这类漏检能量化——「图注里的图号集合」减「已锚定的图号集合」。
2. **caption 被截断**：只抓到 "Figure 3." 后面几个字，没有整段图注。

两个判据都不需要人工看，也不需要知道论文讲什么。**这里只负责「这份结果是不是明显不够」**，
真正决定花不花那 60 秒的是 worker（见 `workers/run.py::_maybe_escalate`）。

做成纯函数的原因：阈值要调，调完得能立刻验证；纯函数最好写单测，也不牵扯数据库。
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass

from .adapters import ParsedDocument
from .labels import LABEL_RE, normalize_label


@dataclass(frozen=True)
class Weakness:
    """一次「轻量档不够用」的判断结果。"""

    code: str  # unanchored_captions | short_captions
    reason: str  # 给人看的一句话
    missing_labels: int = 0
    avg_caption_chars: float = 0.0


def caption_labels(doc: ParsedDocument) -> set[str]:
    """图注块里出现过的图号（``fig:3`` 这种规范形式）。"""
    found: set[str] = set()
    for block in doc.blocks:
        if block.type != "caption":
            continue
        match = LABEL_RE.match((block.text or "").strip())
        if not match:
            continue
        norm = normalize_label(match.group(0))
        if norm and norm.startswith("fig"):
            found.add(norm)
    return found


def anchored_labels(doc: ParsedDocument) -> set[str]:
    """已经定位到 bbox 的图号。"""
    found: set[str] = set()
    for fig in doc.figures:
        if not fig.label_raw:
            continue
        norm = normalize_label(fig.label_raw)
        if norm and norm.startswith("fig"):
            found.add(norm)
    return found


def find_weakness(
    doc: ParsedDocument,
    *,
    min_missing: int = 1,
    min_caption_chars: int = 40,
) -> Weakness | None:
    """判断这份解析结果是否明显不够，够了返回 ``None``。

    注意：**没有文字层的扫描件不走这里**——那是 OCR（MinerU）的活，
    拿 Docling 去跑一篇扫描件只会得到一堆空 block。
    """
    if not doc.text_layer:
        return None

    caps = caption_labels(doc)
    anchored = anchored_labels(doc)
    missing = len(caps - anchored)
    if missing >= max(1, min_missing):
        return Weakness(
            code="unanchored_captions",
            reason=f"识别到 {len(caps)} 个图号，只定位到 {len(anchored)} 张图（缺 {missing}）",
            missing_labels=missing,
        )

    lengths = [len(fig.caption or "") for fig in doc.figures]
    if len(lengths) >= 2:
        avg = sum(lengths) / len(lengths)
        if avg < min_caption_chars:
            return Weakness(
                code="short_captions",
                reason=f"图注平均 {avg:.0f} 字（阈值 {min_caption_chars}），版面没有被真正理解",
                avg_caption_chars=avg,
            )
    return None


def can_escalate(
    target: str, current: str, *, mineru_url: str | None = None
) -> tuple[bool, str]:
    """增强档现在能不能用。返回 ``(可用, 不可用原因)``。

    没装 Docling 就老老实实不升级——排一个注定失败的 job，只是把 60 秒的等待
    换成一条红色错误。
    """
    if not target:
        return False, "未配置增强档（ESCALATE_BACKEND 为空）"
    if target == current:
        return False, "当前就是增强档"
    if target == "docling":
        if importlib.util.find_spec("docling") is None:
            return False, "docling 未安装"
        return True, ""
    if target == "mineru":
        if not mineru_url:
            return False, "MINERU_URL 未配置"
        return True, ""
    return False, f"未知后端：{target}"
