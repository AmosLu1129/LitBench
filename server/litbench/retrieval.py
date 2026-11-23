# SPDX-License-Identifier: AGPL-3.0-or-later
"""轻量 BM25 检索（M9.1，2026-11-15）。

为什么不用 SQLite FTS5 / Postgres tsvector 直接上：

* 两套 SQL 方言的全文索引 DDL、中文分词和迁移都要各写一份，而当前个人库规模
  （10 篇 / 140 图 / 数千 block）在 Python 里排序足够快；
* 检索口径（中文按字二元组 + 英文按词）需要和评分一起演进，放在纯函数里
  可单测、可离线评估，不绑数据库版本；
* 真正的语义召回（向量）是下一步，先把 ``semantic`` 路径从 ILIKE 升级成
  **可解释的 BM25 排序**，接口契约不变。

文档由若干字段加权拼接（title 权重高于 caption），字段内部的 token 按权重
重复计入；CJK 同时保留单字与二元组（「传热」→ 传 / 热 / 传热），这样中文
不依赖 jieba 也能召回；英文按 ``[a-z0-9]+`` 切词。

边界：这是**词面**排序，不是 embedding 语义；同义词/跨语言仍不命中，
这些留给 M9.1b 的 BYO embedding。命中为 0 时不会退化成「随便给几条」。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

# 英文/数字 token；DOI、arXiv id 里的点和斜杠会被切开，但 cite_key 一般
# 由完整 id 组成，切出来的片段仍能命中。
_WORD_RE = re.compile(r"[a-z0-9]+")
_CJK_RE = re.compile(r"[\u3400-\u9fff]+")

# BM25 常用参数（Robertson/Sparck Jones 默认值），不做 per-corpus 调参
K1 = 1.2
B = 0.75
# 查询串在文档里作为连续子串出现时的固定加分：保持旧 LIKE 的「片段命中」
# 行为（例如输入 "pyroly" 这种非完整词），同时让精确片段排在词面命中前面。
EXACT_BONUS = 6.0


@dataclass(frozen=True)
class Document:
    """一篇待检索文档：``id`` + 加权后的 token 序列 + 用于子串加分的原文。"""

    id: str
    tokens: tuple[str, ...]
    text: str = ""


def tokenize(text: str | None) -> list[str]:
    """中英文混合 tokenize：英文按词，中文按单字 + 二元组。"""
    value = (text or "").lower()
    tokens: list[str] = [match.group(0) for match in _WORD_RE.finditer(value)]
    for match in _CJK_RE.finditer(value):
        run = match.group(0)
        if len(run) == 1:
            tokens.append(run)
            continue
        # 单字保召回（「膜」也能命中「膜状凝结」），二元组保精度
        tokens.extend(run)
        tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def authors_text(authors: list | None) -> str:
    """把 ``paper.authors_json``（字符串或 Zotero 风格 dict）拍平成检索文本。"""
    bits: list[str] = []
    for author in authors or []:
        if isinstance(author, str):
            bits.append(author)
        elif isinstance(author, dict):
            name = author.get("name") or " ".join(
                str(author.get(key) or "").strip()
                for key in ("firstName", "lastName")
                if author.get(key)
            )
            if name:
                bits.append(str(name))
    return " ".join(bits)


def weighted_tokens(fields: dict[str, str], weights: dict[str, float]) -> tuple[str, ...]:
    """把字段按权重拼成 token 序列：权重 2 的字段 token 计两次。"""
    out: list[str] = []
    for name, value in fields.items():
        tokens = tokenize(value)
        if not tokens:
            continue
        repeat = max(1, int(round(weights.get(name, 1.0))))
        for _ in range(repeat):
            out.extend(tokens)
    return tuple(out)


def _query_tokens(query: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for token in tokenize(query):
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


def rank_documents(
    documents: list[Document],
    query: str,
    *,
    limit: int | None = None,
) -> list[tuple[str, float]]:
    """BM25 排序 + 子串加分，返回 ``[(id, score)]``（score>0，按分数降序）。

    没有任何 token/子串命中的文档不返回——调用方据此把 ``total`` 算成真实命中数。
    """
    if not documents:
        return []
    query_text = (query or "").strip().lower()
    terms = _query_tokens(query_text)
    if not terms and not query_text:
        return []

    doc_tokens = [doc.tokens for doc in documents]
    doc_lengths = [len(tokens) for tokens in doc_tokens]
    total_docs = len(documents)
    avgdl = (sum(doc_lengths) / total_docs) if total_docs else 0.0
    if avgdl <= 0:
        # 全是空文档：只能靠子串加分
        scored = [
            (doc.id, EXACT_BONUS)
            for doc in documents
            if query_text and query_text in doc.text
        ]
        return sorted(scored, key=lambda item: item[1], reverse=True)[:limit]

    # 查询里出现过的 token → 文档频率
    document_frequency: Counter[str] = Counter()
    for tokens in doc_tokens:
        unique = set(tokens)
        for term in terms:
            if term in unique:
                document_frequency[term] += 1
    idf = {
        term: max(
            0.0,
            math.log(
                1.0 + (total_docs - document_frequency[term] + 0.5)
                / (document_frequency[term] + 0.5)
            ),
        )
        for term in terms
    }

    scored: list[tuple[str, float]] = []
    for doc, tokens, length in zip(documents, doc_tokens, doc_lengths, strict=True):
        score = 0.0
        frequencies = Counter(tokens)
        for term in terms:
            tf = frequencies.get(term, 0)
            if not tf:
                continue
            denominator = tf + K1 * (1.0 - B + B * (length / avgdl))
            score += idf[term] * (tf * (K1 + 1.0)) / denominator
        if query_text and query_text in doc.text:
            score += EXACT_BONUS
        if score > 0:
            scored.append((doc.id, score))

    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored[:limit] if limit is not None else scored


__all__ = [
    "EXACT_BONUS",
    "Document",
    "authors_text",
    "rank_documents",
    "tokenize",
    "weighted_tokens",
]
