# SPDX-License-Identifier: AGPL-3.0-or-later
"""章节树（sections.py）单测：用合成 block 覆盖真机上见过的几种形态。

真机数据（docs/15 §12）里：docling/mineru 教材有 section 标题块但混着 running header、
习题区、表题；pdfium 的 arXiv 没有标题类型且两栏交错。这里把这些形态固化成小样本，
免得以后改启发式时「看着像对的」但实际切歪。
"""

from __future__ import annotations

from litbench.sections import build_sections


def _b(block_id: str, page: int, order: int, btype: str, text: str) -> dict:
    return {
        "id": block_id,
        "page": page,
        "order_idx": order,
        "type": btype,
        "text": text,
    }


def test_chapter_title_split_over_two_blocks_is_merged() -> None:
    blocks = [
        _b("b0", 1, 0, "section", "第 7 章"),
        _b("b1", 1, 1, "section", "相变对流传热的计算"),
        _b("b2", 1, 2, "text", "本章介绍相变传热的基本规律与计算方法。"),
        _b("b3", 2, 3, "section", "7.1 凝结传热的模式"),
        _b("b4", 2, 4, "text", "凝结分为珠状与膜状两种基本形式。"),
    ]
    sections = build_sections(blocks, page_count=2, parser="docling")
    assert [(s.title, s.level) for s in sections] == [
        ("第 7 章 相变对流传热的计算", 1),
        ("7.1 凝结传热的模式", 2),
    ]
    assert [s.key for s in sections]  # 稳定 key
    assert sections[0].block_ids == ["b2"]
    assert sections[0].page_start == 1 and sections[0].page_end == 1


def test_running_header_is_ignored_but_first_chapter_kept() -> None:
    blocks = []
    order = 0
    for page in range(1, 6):
        blocks.append(_b(f"h{page}", page, order, "section", "第 4 章 电力电子技术基础"))
        order += 1
        blocks.append(_b(f"t{page}", page, order, "text", f"第 {page} 页的正文内容，讲电力电子器件。"))
        order += 1
    blocks.append(_b("sec", 6, order, "section", "4.1 电力电子器件"))
    blocks.append(_b("body", 6, order + 1, "text", "晶闸管是最常见的电力电子器件。"))
    sections = build_sections(blocks, page_count=6, parser="mineru")
    titles = [s.title for s in sections]
    assert titles.count("第 4 章 电力电子技术基础") == 1  # 只留第一次当入口
    assert "4.1 电力电子器件" in titles


def test_exercise_area_becomes_one_tail_section() -> None:
    blocks = [
        _b("c0", 1, 0, "section", "第 7 章 相变传热"),
        _b("t0", 1, 1, "text", "正文第一段，介绍本章内容。"),
        _b("s1", 2, 2, "section", "7.1 凝结传热"),
        _b("t1", 2, 3, "text", "凝结传热的正文内容。"),
        _b("ex", 9, 4, "section", "I 习 题 I"),
        _b("e1", 9, 5, "text", "7-1 什么是膜状凝结？"),
        _b("e2", 9, 6, "text", "7-2 写出努塞尔公式。"),
        _b("e3", 9, 7, "text", "7-3 计算换热系数。"),
    ]
    sections = build_sections(blocks, page_count=10, parser="docling")
    assert [s.title for s in sections] == ["第 7 章 相变传热", "7.1 凝结传热", "习题"]
    assert len(sections[-1].block_ids) == 3
    assert sections[-1].page_end == 9


def test_early_exercise_heading_does_not_truncate_later_chapters() -> None:
    """教材选段可能从「上一章习题」开始（实测 04 燃烧学），不能在这里停。"""
    blocks = [
        _b("h0", 1, 0, "section", "第四章 着火理论"),
        _b("t0", 1, 1, "text", "上一章末尾的正文内容。"),
        _b("ex", 2, 2, "section", "思考题和习题"),
        _b("t1", 2, 3, "text", "4-1 什么是热着火？"),
        _b("ref", 3, 4, "section", "参考文献"),
        _b("t2", 3, 5, "text", "[1] 某教材. 燃烧学[M]. 1990."),
        _b("ch5", 4, 6, "section", "第五章"),
        _b("ch5b", 4, 7, "section", "气体燃料燃烧"),
        _b("s1", 4, 8, "section", "第一节 扩散火焰与预混火焰"),
        _b("t3", 4, 9, "text", "气体燃料燃烧的正文内容。"),
    ]
    sections = build_sections(blocks, page_count=29, parser="mineru")
    titles = [s.title for s in sections]
    assert "思考题和习题" in titles
    assert "第五章 气体燃料燃烧" in titles
    assert any(t.startswith("第一节") for t in titles)


def test_relative_numbering_stays_sibling_and_returns_to_parent() -> None:
    blocks = [
        _b("h", 1, 0, "section", "第 5 章 气体燃料燃烧"),
        _b("t0", 1, 1, "text", "章首的正文内容，这里多写几个字。"),
        _b("s1", 2, 2, "section", "第一节 火焰结构"),
        _b("t1", 2, 3, "text", "第一节的正文内容，这里多写几个字。"),
        _b("a1", 3, 4, "section", "一、预混火焰"),
        _b("t2", 3, 5, "text", "预混火焰的正文内容，这里多写几个字。"),
        _b("b1", 4, 6, "section", "1. 层流火焰"),
        _b("t3", 4, 7, "text", "层流火焰的正文内容，这里多写几个字。"),
        _b("b2", 5, 8, "section", "2. 湍流火焰"),
        _b("t4", 5, 9, "text", "湍流火焰的正文内容，这里多写几个字。"),
        _b("a2", 6, 10, "section", "二、扩散火焰"),
        _b("t5", 6, 11, "text", "扩散火焰的正文内容，这里多写几个字。"),
    ]
    sections = build_sections(blocks, page_count=6, parser="mineru")
    levels = {s.title: s.level for s in sections}
    assert levels["第一节 火焰结构"] == 2
    assert levels["一、预混火焰"] == 3
    assert levels["1. 层流火焰"] == 4
    assert levels["2. 湍流火焰"] == 4  # 与 1. 同级
    assert levels["二、扩散火焰"] == 3  # 回到「一、」的层级


def test_pdfium_documents_fall_back_to_page_windows() -> None:
    blocks = []
    for page in range(1, 23):
        for line in range(3):
            blocks.append(
                _b(
                    f"p{page}l{line}",
                    page,
                    (page - 1) * 3 + line,
                    "text",
                    f"第 {page} 页第 {line} 行的正文，两栏交错的行级文本。",
                )
            )
    sections = build_sections(blocks, page_count=22, parser="pdfium")
    assert sections
    assert all(s.kind == "page" for s in sections)
    assert len(sections) == 6
    assert sections[0].title == "第 1–4 页"
    assert all(s.block_ids for s in sections)


def test_heading_poor_document_falls_back_to_page_windows() -> None:
    blocks = [
        _b("b0", 1, 0, "text", "没有任何标题的扫描件正文第一段。"),
        _b("b1", 2, 1, "text", "第二页的正文内容。"),
        _b("b2", 3, 2, "text", "第三页的正文内容。"),
    ]
    sections = build_sections(blocks, page_count=3, parser="mineru")
    assert all(s.kind == "page" for s in sections)
    assert [s.title for s in sections] == ["第 1 页", "第 2 页", "第 3 页"]
    assert sections[0].block_ids == ["b0"]


def test_empty_sections_are_dropped() -> None:
    blocks = [
        _b("h0", 1, 0, "section", "第 1 章 开头"),
        _b("h1", 1, 1, "section", "1.1 空标题"),
        _b("h2", 2, 2, "section", "1.2 有内容"),
        _b("t", 2, 3, "text", "这里有正文，够长了可以当证据块。"),
    ]
    sections = build_sections(blocks, page_count=2, parser="docling")
    assert [s.title for s in sections] == ["第 1 章 开头", "1.2 有内容"]
