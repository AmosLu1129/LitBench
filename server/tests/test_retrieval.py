# SPDX-License-Identifier: AGPL-3.0-or-later
"""BM25 检索纯函数测试（M9.1，2026-11-15）。

守三件事：

1. 中英文 tokenize 不依赖第三方分词：中文单字 + 二元组、英文按词；
2. 排序对词面命中有区分度，字段权重生效，0 命中返回空（不退化）；
3. 旧 LIKE 的「片段命中」保留：查询是文档连续子串时仍能命中并加分。
"""

from __future__ import annotations

from litbench.retrieval import (
    Document,
    authors_text,
    rank_documents,
    tokenize,
    weighted_tokens,
)


def test_tokenize_mixed_language() -> None:
    tokens = tokenize("Heat Transfer 2024，膜状凝结")
    assert "heat" in tokens and "transfer" in tokens and "2024" in tokens
    # 中文：单字保召回 + 二元组保精度
    assert "膜" in tokens and "膜状" in tokens and "状凝" in tokens and "凝结" in tokens
    assert tokenize(None) == []
    assert tokenize("  ") == []


def test_weighted_tokens_repeat_fields() -> None:
    tokens = weighted_tokens(
        {"title": "heat", "caption": "curve"},
        {"title": 3.0, "caption": 1.0},
    )
    assert tokens.count("heat") == 3
    assert tokens.count("curve") == 1


def test_authors_text_accepts_strings_and_zotero_dicts() -> None:
    assert authors_text(["Wang Lei", {"firstName": "Wei", "lastName": "Zhang"}]) == (
        "Wang Lei Wei Zhang"
    )
    assert authors_text([{"name": "张三"}]) == "张三"
    assert authors_text(None) == ""


def test_bm25_ranks_relevant_document_first() -> None:
    docs = [
        Document(id="a", tokens=weighted_tokens({"title": "coal pyrolysis kinetics"}, {"title": 1})),
        Document(id="b", tokens=weighted_tokens({"title": "deep learning for NLP"}, {"title": 1})),
    ]
    ranked = rank_documents(docs, "pyrolysis kinetics")
    assert [doc_id for doc_id, _score in ranked] == ["a"]

    # 两个词都命中的文档分数高于只命中一个词的文档
    docs.append(
        Document(
            id="c",
            tokens=weighted_tokens({"title": "coal combustion boiler"}, {"title": 1}),
        )
    )
    ranked2 = rank_documents(docs, "coal pyrolysis")
    assert ranked2[0][0] == "a"
    assert {doc_id for doc_id, _score in ranked2} == {"a", "c"}


def test_exact_substring_bonus_keeps_partial_queries() -> None:
    docs = [
        Document(id="a", tokens=tokenize("coal pyrolysis kinetics"), text="coal pyrolysis kinetics"),
        Document(id="b", tokens=tokenize("coal combustion boiler"), text="coal combustion boiler"),
    ]
    # "pyroly" 不是完整 token，只能靠子串加分命中（旧 LIKE 语义）
    ranked = rank_documents(docs, "pyroly")
    assert [doc_id for doc_id, _score in ranked] == ["a"]


def test_no_match_returns_empty() -> None:
    docs = [Document(id="a", tokens=tokenize("coal pyrolysis"), text="coal pyrolysis")]
    assert rank_documents(docs, "nanoparticle") == []
    assert rank_documents(docs, "") == []
    assert rank_documents([], "coal") == []


def test_cjk_query_matches_chinese_caption() -> None:
    docs = [
        Document(id="zh", tokens=tokenize("膜状凝结是工程设计的依据")),
        Document(id="en", tokens=tokenize("boiling heat transfer")),
    ]
    ranked = rank_documents(docs, "膜状凝结")
    assert [doc_id for doc_id, _score in ranked] == ["zh"]


def test_limit_and_stable_order() -> None:
    docs = [
        Document(id=f"d{i}", tokens=tokenize("shared term")) for i in range(5)
    ]
    ranked = rank_documents(docs, "shared term", limit=3)
    assert len(ranked) == 3
    # 同分时按 id 兜底，保证翻页稳定
    assert [doc_id for doc_id, _score in ranked] == ["d0", "d1", "d2"]
