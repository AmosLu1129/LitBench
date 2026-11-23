# SPDX-License-Identifier: AGPL-3.0-or-later
"""图号校核规则层的单测（纯函数，不碰 DB / 模型）。

覆盖三类真实课本里踩到的问题：
1. 编号文本被污染（空格、/、凸、^、e→5）后的容错与置信度；
2. 一个系列图被拆成多个图块时的归组与父图号继承；
3. caption 里有图号但没被提取出来时的补提。
"""

from __future__ import annotations

from litbench.adapters.base import ParsedBlock, ParsedDocument, ParsedFigure
from litbench.labeling import (
    CONF_INHERIT,
    CONF_REPAIRED,
    CONF_REVIEW,
    apply_label_rules,
    assess_text_layer,
    canonicalize_label,
)

KNOWN = {"fig:10-1", "fig:10-5", "fig:10-6", "fig:7-1", "fig:7-15", "fig:4-2-1", "fig:4-3-1"}


def fig(page: int, bbox: list[float], *, caption: str = "", label: str | None = None) -> ParsedFigure:
    return ParsedFigure(page=page, bbox=bbox, caption=caption, label_raw=label)


def doc_with(*figures: ParsedFigure, blocks: list[ParsedBlock] | None = None) -> ParsedDocument:
    return ParsedDocument(
        page_count=max([f.page for f in figures] or [1]),
        engine="test",
        engine_version="1",
        figures=list(figures),
        blocks=blocks or [],
    )


# --- 编号容错 ---------------------------------------------------------------
def test_clean_labels_stay_high_confidence() -> None:
    for raw, want in [
        ("图 10-5 开口带传动的几何关系", "fig:10-5"),
        ("Fig. 3", "fig:3"),
        ("图 4.2.1 G(jω) 的极坐标图", "fig:4-2-1"),
    ]:
        got = canonicalize_label(raw, known_labels=KNOWN)
        assert got.label_norm == want
        assert got.confidence == CONF_REVIEW, raw
        assert got.source == "text"


def test_repaired_labels() -> None:
    cases = {
        "图 7 - 25 降膜蒸发小点图": "fig:7-25",       # 编号中间被空格撑开
        "图 10/0 圆周运动产生的离心拉力分析": "fig:10-0",  # 分隔符被识别成 /
        "图 10凸7 瞬时链速变化规律": "fig:10-7",        # 分隔符被识别成「凸」
        "图 10^2 链节伸长": "fig:10-2",                # 分隔符被识别成 ^
    }
    for raw, want in cases.items():
        got = canonicalize_label(raw, known_labels=KNOWN)
        assert got.label_norm == want, raw
        assert got.confidence == CONF_REPAIRED, raw
        assert got.source == "rule_repair"


def test_ocr_letter_repair_uses_context() -> None:
    """图 4.e.1：e 被当成 5，且 4.5.x 在本章出现过 → 修复并给中等置信度。"""
    got = canonicalize_label("图 4.e.1 习题4.11 图", known_labels=KNOWN | {"fig:4-5-1"})
    assert got.label_norm == "fig:4-5-1"
    assert got.source == "rule_repair"
    assert got.confidence <= CONF_REPAIRED


def test_unparsable_text_returns_none() -> None:
    for raw in ["题4.2图", "带传动的工作原理", ""]:
        got = canonicalize_label(raw, known_labels=KNOWN)
        assert got.label_norm is None


# --- 子图归组 / 继承 --------------------------------------------------------
def test_subfigures_share_group_and_parent_label() -> None:
    parent_caption = "图 4.2.1 G(jω) 的极坐标图"
    figures = [
        fig(3, [100, 100, 200, 180], caption=parent_caption, label="图 4.2.1"),
        fig(3, [210, 100, 310, 180], caption=parent_caption),
        fig(3, [320, 100, 420, 180], caption=parent_caption),
    ]
    doc = doc_with(*figures)
    stats = apply_label_rules(doc)
    assert stats["groups"] == 1
    assert all(f.group_key == "p3-g1" for f in figures)
    assert [f.sublabel for f in figures] == ["a", "b", "c"]
    assert figures[1].label_raw == "图 4.2.1"
    assert figures[1].label_source == "rule_inherit"
    assert figures[1].label_confidence == CONF_INHERIT
    assert len({f.group_key for f in figures}) == 1


def test_single_figure_has_no_group() -> None:
    doc = doc_with(fig(4, [100, 100, 300, 200], caption="图 4.2.2 半对数坐标图"))
    apply_label_rules(doc)
    assert doc.figures[0].group_key is None


def test_subfigure_letters_follow_reading_order_not_top_edge() -> None:
    """并排子图右边的上边缘高 2pt，不能被排到左边（真实课本 03/04/05 都踩到）。

    旧实现按 (top, left) 排序：右边图 top 小 2pt → 先排到 → 拿到 a，整行标反。
    """
    figures = [
        fig(2, [100, 380, 250, 560], caption="(a) 等效结构"),
        fig(2, [300, 378, 450, 555], caption="(b) 等效电路"),
    ]
    figures[0].label_raw = "图 5-2"
    doc = doc_with(*figures)
    stats = apply_label_rules(doc)
    assert stats["groups"] == 1
    assert figures[0].sublabel == "a"  # 左边
    assert figures[1].sublabel == "b"  # 右边


def test_subfigure_letters_2x2_are_row_major() -> None:
    """2×2 系列图：a 上左、b 上右、c 下左、d 下右（阅读顺序，不是列优先）。"""
    caption = "图 4-50 一元系统燃烧过程的计算结果"
    figures = [
        fig(1, [100, 330, 250, 440], caption=caption, label="图 4-50"),  # 上左
        fig(1, [290, 329, 438, 440], caption=caption),  # 上右（top 小 1pt）
        fig(1, [104, 463, 264, 575], caption=caption),  # 下左
        fig(1, [289, 460, 438, 574], caption=caption),  # 下右
    ]
    doc = doc_with(*figures)
    apply_label_rules(doc)
    assert [f.sublabel for f in figures] == ["a", "b", "c", "d"]


def test_junk_letter_in_caption_is_ignored() -> None:
    """图注里出现「(j)」这种脏字符时，不能把它当成子图字母；字母只按阅读顺序排。"""
    figures = [
        fig(6, [100, 100, 200, 200], caption="(j)"),
        fig(6, [210, 100, 310, 200], caption="图 4.3.2 因子 jω 的频率特性图"),
    ]
    figures[1].label_raw = "图 4.3.2"
    doc = doc_with(*figures)
    apply_label_rules(doc)
    # 旧实现会把左边这张标成子图 j
    assert [f.sublabel for f in figures] == ["a", "b"]


def test_misread_subfigure_number_rejoins_group() -> None:
    """OCR 把「图 4-50a」读成「图 4-500」时，按「图注以 a) 开头 + 尾段是父号前缀扩展」并回系列。"""
    figures = [
        fig(1, [100, 330, 250, 440], caption="a) 方案1", label="图 4-500"),  # 上左
        fig(1, [290, 329, 438, 440], caption="b) 方案2", label="图 4-50"),  # 上右（父号）
        fig(1, [104, 463, 264, 575], caption="c)"),  # 下左
        fig(1, [289, 460, 438, 574], caption="d)"),  # 下右
    ]
    doc = doc_with(*figures)
    stats = apply_label_rules(doc)
    assert stats["groups"] == 1
    assert stats["reassigned"] == 1
    assert [f.sublabel for f in figures] == ["a", "b", "c", "d"]
    assert figures[0].label_raw == "图 4-50"  # 坏号被父号覆盖
    assert figures[0].label_source == "rule_inherit"


def test_caption_letters_do_not_override_reading_order() -> None:
    """子图图注的 (a)/(b) 会被版面模型错配到相邻块；字母以位置为准，不采信图注字母。"""
    figures = [
        fig(7, [100, 100, 200, 200], caption="(b)"),
        fig(7, [210, 100, 310, 200], caption="(a)"),
    ]
    figures[0].label_raw = "图 4.3.3"
    doc = doc_with(*figures)
    apply_label_rules(doc)
    assert [f.sublabel for f in figures] == ["a", "b"]


def test_groups_are_recomputed_not_stale() -> None:
    """第二次 apply 必须清掉上一轮的 group_key——OCR 融合会改标签，僵尸组会留错号。"""
    caption = "图 4.2.1 G(jω) 的极坐标图"
    figures = [
        fig(3, [100, 100, 200, 180], caption=caption, label="图 4.2.1"),
        fig(3, [210, 100, 310, 180], caption=caption),
    ]
    doc = doc_with(*figures)
    apply_label_rules(doc)
    assert figures[0].group_key is not None

    # 模拟 OCR 融合把第二张改成另一个图号 → 两张不再是同一系列
    figures[1].label_raw = "图 4.2.2"
    figures[1].label_source = "ocr"
    apply_label_rules(doc)
    assert figures[0].group_key is None
    assert figures[1].group_key is None
    assert figures[0].sublabel is None  # 不再是系列成员，字母也要清掉


# --- 抓取漏提的编号 ---------------------------------------------------------
def test_repairs_label_from_caption_text() -> None:
    """caption 里其实有「图 7-15」，只是解析器没提出来。"""
    doc = doc_with(fig(13, [100, 100, 300, 200], caption="及时排液的措施 图 7-15"))
    stats = apply_label_rules(doc)
    assert stats["repaired"] == 1
    assert doc.figures[0].label_raw == "图 7-15"
    assert doc.figures[0].label_source == "rule_repair"


def test_body_reference_is_not_treated_as_label() -> None:
    """「题4.2图」「图4.5.11 和图4.5.12 画出了…」这种没有独立图注的，不能硬提。"""
    doc = doc_with(fig(1, [100, 100, 200, 200], caption="题4.2图"))
    apply_label_rules(doc)
    assert doc.figures[0].label_raw is None


# --- 文字层质量 -------------------------------------------------------------
def test_text_quality_good_vs_broken() -> None:
    good = ParsedDocument(
        page_count=1,
        engine="x",
        engine_version="1",
        blocks=[
            ParsedBlock(
                page=1,
                bbox=[0, 0, 1, 1],
                type="text",
                text="图 10-5 开口带传动的几何关系。带传动的主要形式有开口传动、交叉传动。",
            )
        ],
    )
    broken = ParsedDocument(
        page_count=1,
        engine="x",
        engine_version="1",
        blocks=[
            ParsedBlock(page=1, bbox=[0, 0, 1, 1], type="text", text="图 10/0 圆周运动 图 10凸7 瞬时链速"),
        ],
    )
    assert assess_text_layer(good).score >= 0.9
    assert assess_text_layer(broken).score < assess_text_layer(good).score
