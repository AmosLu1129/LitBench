# SPDX-License-Identifier: AGPL-3.0-or-later
"""图号与查询解析的单元测试。

不依赖任何第三方库，可以直接跑：

    python server/tests/test_labels.py

也可以被 pytest 收集：

    python -m pytest server/tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from litbench.labels import normalize_label, normalize_title, parse_query, split_label


def test_label_variants() -> None:
    cases = {
        "Fig. 3": "fig:3",
        "Figure 3": "fig:3",
        "FIG.3": "fig:3",
        "图3": "fig:3",
        "图 3": "fig:3",
        "Fig. 3a": "fig:3a",
        "Fig. S3": "fig:s3",
        "Table 2": "tab:2",
        "表2": "tab:2",
    }
    for raw, want in cases.items():
        got = normalize_label(raw)
        assert got == want, f"{raw!r} -> {got!r}, 期望 {want!r}"


def test_label_noise_is_none() -> None:
    for raw in ["", "hello", "第 3 章", "2019", "figure"]:
        assert normalize_label(raw) is None, raw


def test_title_norm() -> None:
    assert normalize_title("Deep Learning for NLP.") == "deep learning for nlp"
    assert normalize_title("  Coal   Pyrolysis  ") == "coal pyrolysis"
    assert normalize_title("粒子群优化算法") == "粒子群优化算法"


def test_doi() -> None:
    q = parse_query("10.1016/j.fuel.2019.115000")
    assert q.doi == "10.1016/j.fuel.2019.115000"
    assert q.is_provenance


def test_arxiv() -> None:
    q = parse_query("arXiv:2101.12345")
    assert q.arxiv == "2101.12345"
    assert q.is_provenance


def test_citekey() -> None:
    q = parse_query("wang2019deep")
    assert q.citekey == "wang2019deep"
    assert q.is_provenance


def test_author_year_plus_label() -> None:
    """一次查询可以同时带文献线索和图号，两者都生效。"""
    q = parse_query("Wang 2019 Fig. 3")
    assert q.author == "Wang"
    assert q.year == 2019
    assert q.label == "fig:3"
    assert q.text == ""


def test_doi_plus_label() -> None:
    q = parse_query("Fig. 3 of 10.1016/j.fuel.2019.115000")
    assert q.label == "fig:3"
    assert q.doi == "10.1016/j.fuel.2019.115000"


def test_free_text_is_not_provenance() -> None:
    q = parse_query("coal pyrolysis kinetics")
    assert q.text == "coal pyrolysis kinetics"
    assert q.label is None
    assert not q.is_provenance


def test_empty_query() -> None:
    q = parse_query("   ")
    assert q.text == ""
    assert not q.is_provenance


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok    {name}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL  {name}: {exc}")
    print(f"\n{'all passed' if not failed else f'{failed} failed'}")
    sys.exit(1 if failed else 0)
def test_chinese_textbook_label_keeps_chapter_number() -> None:
    """中文教材的「图 10-5」不能退化成「图 10」——否则整章 14 张图同名。"""
    assert normalize_label("图 10-5 开口带传动的几何关系") == "fig:10-5"
    assert normalize_label("图7-12 整体式低肋管截面图") == "fig:7-12"
    assert split_label("fig:10-5") == (10, "5")
    assert split_label("fig:7-12") == (7, "12")

    q = parse_query("图 10-5")
    assert q.label == "fig:10-5"
    assert q.is_provenance


def test_english_labels_are_unchanged() -> None:
    """加了章节号之后，arXiv 那套写法必须原样。"""
    assert normalize_label("Fig. 3") == "fig:3"
    assert normalize_label("Figure 3a") == "fig:3a"
    assert normalize_label("FIG. S2") == "fig:s2"
    assert normalize_label("Fig. 3 of the paper") == "fig:3"
    assert split_label("fig:3a") == (3, "a")
    assert split_label("fig:s2") == (2, None)


def test_dotted_chinese_labels_keep_all_sections() -> None:
    """《自动控制原理》全用点分编号「图 4.2.1」，不能退化成「图 4」。"""
    assert normalize_label("图 4.2.1 G(jω) 的极坐标图") == "fig:4-2-1"
    assert normalize_label("图4.4.2 [例4.4.1]开环") == "fig:4-4-2"
    assert normalize_label("表 4.4.1 二阶系统阻尼比对照表") == "tab:4-4-1"
    assert split_label("fig:4-2-1") == (4, "2-1")

    q = parse_query("图 4.2.1")
    assert q.label == "fig:4-2-1"
    assert q.is_provenance

    # 点分扩展不能把英文 "Fig. 3 of 10" 带偏（它没有紧贴的分隔符）
    assert normalize_label("Fig. 3 of 10") == "fig:3"


