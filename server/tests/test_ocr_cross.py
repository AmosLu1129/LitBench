# SPDX-License-Identifier: AGPL-3.0-or-later
"""双 OCR 交叉校核：图号抽取、两套结果一致性、文字层↔OCR 融合。

真跑 RapidOCR 太慢（而且 CI 里不该有模型），所以这里 mock 掉「第二套 OCR 读图」这一步，
只测试判定逻辑本身。
"""

from __future__ import annotations

from pathlib import Path

from litbench.adapters.base import ParsedBlock, ParsedDocument, ParsedFigure
from litbench import ocr_cross


def fig(page: int, bbox: list[float], *, caption: str = "", label: str | None = None,
        caption_bbox: list[float] | None = None) -> ParsedFigure:
    return ParsedFigure(
        page=page, bbox=bbox, caption=caption, label_raw=label, caption_bbox=caption_bbox
    )


def doc_of(*figures: ParsedFigure, text_layer: bool = True) -> ParsedDocument:
    return ParsedDocument(
        page_count=max([f.page for f in figures] or [1]),
        engine="test",
        engine_version="1",
        figures=list(figures),
        text_layer=text_layer,
        blocks=[ParsedBlock(page=1, bbox=[0, 0, 1, 1], type="text", text="正文")],
    )


def test_labels_in_text_handles_broken_ocr_text() -> None:
    """第二套 OCR 也可能把编号读坏，抽取逻辑要能吃下常见形态。"""
    assert ocr_cross.labels_in_text("图 4.2.1 的极坐标图") == {"fig:4-2-1"}
    assert ocr_cross.labels_in_text("图 7 - 25 降膜蒸发") == {"fig:7-25"}
    assert ocr_cross.labels_in_text("图 4.e.1 习题") == {"fig:4-5-1"}
    assert ocr_cross.labels_in_text("没有编号的说明文字") == set()


def test_cross_check_consensus_conflict_and_unresolved(monkeypatch) -> None:
    figures = [
        fig(3, [100, 100, 200, 180], caption="图 4.2.1 G(jω) 的极坐标图", caption_bbox=[100, 200, 300, 230]),
        # 主文本是「图 4.e.1」（字母段，需要校核）；第二套读成另一个也带字母段的号 → 冲突
        fig(4, [100, 300, 200, 380], caption="图 4.e.1 习题4.11 图", caption_bbox=[100, 400, 300, 430]),
        # 两套都读成「图 4.e.2」：字面量一致但无法安全归一 → 待确认，不硬改
        fig(5, [100, 500, 200, 580], caption="图 4.e.2 习题4.18 图", caption_bbox=[100, 600, 300, 630]),
    ]
    doc = doc_of(*figures)
    monkeypatch.setattr(ocr_cross, "_render_page", lambda *a, **k: object())
    readings = iter(["图 4.2.1 G(jω) 的极坐标图", "图 4.e.9 无法解析", "图 4.e.2 习题4.18 图"])
    monkeypatch.setattr(ocr_cross, "_read_on_image", lambda image, bbox, dpi: next(readings))

    stats = ocr_cross.cross_check_figures(doc, Path("fake.pdf"))
    assert stats["checked"] == 3
    assert stats["consensus"] == 1
    assert stats["conflict"] == 1
    assert stats["unresolved"] == 1
    assert figures[0].label_source == "ocr_consensus"
    assert figures[0].label_confidence >= 0.9
    assert figures[1].label_source == "ocr_cross_conflict"
    assert figures[1].label_confidence < 0.6
    assert figures[2].label_raw == "图4.e.2"
    assert figures[2].label_source == "ocr_cross_unresolved"
    assert figures[2].label_confidence < 0.6

    # 主文本没有编号、第二套读到了 → 补上
    third = fig(6, [100, 700, 200, 780], caption="(a) 极坐标图", caption_bbox=[100, 800, 300, 830])
    doc2 = doc_of(third)
    monkeypatch.setattr(ocr_cross, "_render_page", lambda *a, **k: object())
    monkeypatch.setattr(ocr_cross, "_read_on_image", lambda image, bbox, dpi: "图 4.3.9 因子 K")
    stats2 = ocr_cross.cross_check_figures(doc2, Path("fake.pdf"))
    assert stats2["rescued"] == 1
    assert third.label_raw == "图 4.3.9"


def test_second_ocr_rescues_missing_label(monkeypatch) -> None:
    """主 OCR 没提到图号、第二套读到了 → 补上。"""
    one = fig(5, [100, 100, 200, 180], caption="(a) 极坐标图", caption_bbox=[100, 200, 300, 230])
    doc = doc_of(one)
    monkeypatch.setattr(ocr_cross, "_render_page", lambda *a, **k: object())
    monkeypatch.setattr(ocr_cross, "_read_on_image", lambda image, bbox, dpi: "图 4.3.1 因子 K 的频率特性图")
    stats = ocr_cross.cross_check_figures(doc, Path("fake.pdf"))
    assert stats["rescued"] == 1
    assert one.label_raw == "图 4.3.1"
    assert one.label_source == "ocr"


def test_merge_text_layer_prefers_ocr_when_text_layer_wrong() -> None:
    """文字层的「图 162」错号被 OCR 的「图 1-2」替换（书 01 的真实形态）。"""
    base_fig = fig(2, [100, 100, 400, 200], caption="图 162 交又传动与半交又传动", label="图 162")
    base = doc_of(base_fig)
    ocr = doc_of(
        fig(2, [102, 98, 398, 202], caption="图 1-2 交叉传动与半交叉传动", label="图 1-2"),
        text_layer=False,
    )
    stats = ocr_cross.merge_text_layer(base, ocr)
    assert stats["matched"] == 1
    assert stats["relabeled"] == 1
    assert base_fig.label_raw == "图 1-2"
    assert base_fig.label_source == "ocr"


def test_merge_text_layer_keeps_agreement() -> None:
    base_fig = fig(3, [100, 100, 400, 200], caption="图 4.2.1 G(jω) 的极坐标图", label="图 4.2.1")
    base = doc_of(base_fig)
    ocr = doc_of(fig(3, [101, 101, 399, 199], caption="图 4.2.1 G(jω) 的极坐标图"), text_layer=False)
    stats = ocr_cross.merge_text_layer(base, ocr)
    assert stats["consensus"] == 1
    assert base_fig.label_raw == "图 4.2.1"
    assert base_fig.label_source == "ocr_consensus"


def test_merge_does_not_match_distant_figure() -> None:
    base_fig = fig(3, [100, 100, 200, 200], caption="图 4.2.1 图", label="图 4.2.1")
    base = doc_of(base_fig)
    ocr = doc_of(fig(3, [400, 500, 500, 600], caption="图 9-9 完全无关"), text_layer=False)
    stats = ocr_cross.merge_text_layer(base, ocr)
    assert stats["unmatched"] == 1
    assert base_fig.label_raw == "图 4.2.1"
