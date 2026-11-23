# SPDX-License-Identifier: AGPL-3.0-or-later
"""图表检索。

两条路分开实现，不是一个函数加个排序参数：

* **出处检索**（``pq.is_provenance``）——确定性匹配。命中不到就是 0 条，
  绝不能退化成「找几个相似的给你」。这是和语义检索最本质的区别。
* **语义 / 浏览**——M9.1 起语义路径用 BM25 排序（``retrieval.py``），不再用
  caption/标题的 ILIKE 子串；``engine`` 字段说明实际引擎（``bm25`` / ``like``）。
  BM25 仍是**词面**排序，真正的向量召回在 M9.1b（BYO embedding）。
"""

from __future__ import annotations

import re

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .labeling import LOW_CONFIDENCE
from .labels import ParsedQuery, normalize_label, normalize_title
from .models import Figure, Paper, PaperCollection, PaperVersion, UserPaperState
from .retrieval import Document, authors_text, rank_documents, weighted_tokens

_INTERNAL_ID = re.compile(r"^[0-9a-f]{32}$")

# 语义路径默认引擎；``like`` 只保留给评估脚本做基线对比（不对外暴露参数）。
SEMANTIC_ENGINES = ("bm25", "like")
DEFAULT_SEMANTIC_ENGINE = "bm25"
# BM25 排序需要看到候选全集（先过滤、后排序）；个人库规模下 5000 足够，
# 超出时截断并在 hint 里说明（避免一次查询把大库全部拉进内存）。
SEMANTIC_CANDIDATE_CAP = 5000

# 字段权重：题名/图注/图号主导，作者与刊名只做辅助召回
_FIGURE_FIELD_WEIGHTS = {
    "caption": 3.0,
    "label": 2.0,
    "title": 3.0,
    "authors": 1.0,
    "venue": 1.0,
}

# 图号校核阶段 3：低置信/冲突/无号的筛选
LABEL_STATES = ("review", "unlabeled", "low_confidence", "conflict", "unresolved")

# 图表类型（adapters/base.py 里 ParsedFigure.figure_type 的取值集合）。
# API 参数校验用它，别让非法值静默变成「0 条结果」。
FIGURE_TYPES = ("chart", "micrograph", "photo", "map", "diagram", "structure", "other")

# LIKE 的转义符：用户输入里的 \ % _ 都按普通字符匹配
LIKE_ESCAPE = "\\"


def _like_pattern(text: str) -> str:
    """把用户输入包成 LIKE 片段，并转义通配符。

    不转义时 ``q=%`` 就是「匹配所有行」、``q=a_b`` 里的 ``_`` 会变成
    「任意一个字符」——用户以为自己搜的是字面片段，结果语义完全不同。
    """
    escaped = text.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
    escaped = escaped.replace("%", f"{LIKE_ESCAPE}%").replace("_", f"{LIKE_ESCAPE}_")
    return f"%{escaped}%"


def _label_state_condition(state: str):
    if state == "unlabeled":
        return Figure.label_raw.is_(None)
    if state == "low_confidence":
        return Figure.label_confidence.isnot(None) & (Figure.label_confidence < LOW_CONFIDENCE)
    if state == "conflict":
        return Figure.label_source == "ocr_cross_conflict"
    if state == "unresolved":
        return Figure.label_source == "ocr_cross_unresolved"
    # review = 所有需要人工/agent 看一眼的
    return or_(
        Figure.label_raw.is_(None),
        Figure.label_confidence < LOW_CONFIDENCE,
        Figure.label_source.in_(("ocr_cross_conflict", "ocr_cross_unresolved")),
    )


def _paper_conditions(pq: ParsedQuery, *, text_as_filter: bool) -> list:
    conds: list = []

    if pq.doi:
        conds.append(Paper.doi == pq.doi)
    if pq.arxiv:
        conds.append(Paper.arxiv_id == pq.arxiv)
    if pq.citekey:
        conds.append(Paper.cite_key == pq.citekey)
    if pq.year:
        conds.append(Paper.year == pq.year)
    if pq.author:
        # authors_json 是数组。转文本后 ILIKE 对 Postgres 与 SQLite 都成立，
        # 代价是不能用索引——个人库规模（1e4 量级）可以接受。
        conds.append(
            cast(Paper.authors_json, String).ilike(
                _like_pattern(pq.author), escape=LIKE_ESCAPE
            )
        )
    if text_as_filter and pq.text:
        if _INTERNAL_ID.match(pq.text):
            conds.append(Paper.id == pq.text)
        else:
            norm = normalize_title(pq.text)
            title_conds = [
                Paper.title.like(_like_pattern(pq.text), escape=LIKE_ESCAPE),
                Paper.cite_key.ilike(_like_pattern(pq.text), escape=LIKE_ESCAPE),
            ]
            if norm:
                title_conds.append(
                    Paper.title_norm.like(_like_pattern(norm), escape=LIKE_ESCAPE)
                )
            conds.append(or_(*title_conds))
    return conds


def classify(pq: ParsedQuery) -> str:
    if pq.is_provenance:
        return "provenance"
    if pq.text:
        return "semantic"
    return "browse"


def _figure_document(figure: Figure, paper: Paper) -> Document:
    fields = {
        "caption": figure.caption or "",
        "label": " ".join(
            value for value in (figure.label_raw, figure.label_norm) if value
        ),
        "title": paper.title or "",
        "authors": authors_text(paper.authors_json),
        "venue": paper.venue or "",
    }
    # 子串加分只沿用旧 LIKE 覆盖的 caption/标题/图号，避免把作者、刊名也当精确命中
    text = " ".join(
        value
        for value in (
            figure.caption or "",
            figure.label_raw or figure.label_norm or "",
            paper.title or "",
        )
        if value
    ).lower()
    return Document(
        id=figure.id,
        tokens=weighted_tokens(fields, _FIGURE_FIELD_WEIGHTS),
        text=text,
    )


async def search_figures(
    session: AsyncSession,
    pq: ParsedQuery,
    *,
    figure_type: str | None = None,
    review_status: str | None = None,
    label_state: str | None = None,
    favorite: bool | None = None,
    collection_ids: list[str] | None = None,
    paper_ids: list[str] | None = None,
    scope_condition=None,
    favorite_user_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    semantic_engine: str = DEFAULT_SEMANTIC_ENGINE,
) -> tuple[str, bool, str, int, list[tuple[Figure, PaperVersion, Paper]]]:
    """检索图表，返回 ``(mode, degraded, engine, total, rows)``。

    ``total`` 是**匹配总数**（加 limit/offset 之前算），不是当前页条数——
    前端翻页/显示「共 N 条」都靠它。

    ``scope_condition`` / ``favorite_user_id`` 是团队共享（M7）加的：认证打开后
    图表列表只能看到自己库里的文献（或指定工作组的共享文献），收藏也是每人一份。
    认证关闭时两者都是 None，走旧行为。

    ``semantic_engine="like"`` 只用于评估脚本复现旧基线，API 不暴露。
    """
    if semantic_engine not in SEMANTIC_ENGINES:
        raise ValueError(f"semantic_engine 只支持 {SEMANTIC_ENGINES}")
    mode = classify(pq)
    if paper_ids and mode == "browse":
        # 指定了具体文献（paper id / citekey / DOI 命中）且没有自由查询：
        # 这是确定性的「按文献列出处」，空结果要能给出「没有匹配」的提示，
        # 而不是落到「按时间浏览」。
        mode = "provenance"
    use_bm25 = mode == "semantic" and semantic_engine == "bm25"
    degraded = mode == "semantic" and not use_bm25
    engine = "bm25" if use_bm25 else ("like" if mode == "semantic" else mode)

    stmt = (
        select(Figure, PaperVersion, Paper)
        .join(PaperVersion, Figure.paper_version_id == PaperVersion.id)
        .join(Paper, PaperVersion.paper_id == Paper.id)
        # 软删除的图（用户删掉/手动清理）不出现在任何检索结果里
        .where(Figure.deleted_at.is_(None))
    )

    for cond in _paper_conditions(pq, text_as_filter=(mode == "provenance")):
        stmt = stmt.where(cond)

    # 文献视角的精确过滤（E1：paper 参数解析成 id 后由这里收口）
    if paper_ids:
        stmt = stmt.where(Paper.id.in_(paper_ids))

    if pq.label:
        stmt = stmt.where(Figure.label_norm == pq.label)

    if figure_type:
        stmt = stmt.where(Figure.figure_type == figure_type)

    if review_status:
        stmt = stmt.where(Figure.review_status == review_status)

    if label_state:
        stmt = stmt.where(_label_state_condition(label_state))

    # 工作台（2026-11-12）：文献视角的过滤——收藏夹 / 某棵知识树。
    # 收藏夹与「全部文献」共用同一套图表检索，只是先按 paper 过滤。
    if favorite is not None:
        if favorite_user_id:
            stmt = stmt.where(
                Paper.id.in_(
                    select(UserPaperState.paper_id).where(
                        UserPaperState.user_id == favorite_user_id,
                        UserPaperState.favorite.is_(favorite),
                    )
                )
            )
        else:
            stmt = stmt.where(Paper.favorite.is_(favorite))
    if scope_condition is not None:
        stmt = stmt.where(scope_condition)
    if collection_ids:
        # 去重放在「集合 → paper_id」子查询里，而不是 join 后对整行 DISTINCT：
        # figure/paper_version/paper 都有多个 JSON 列，Postgres 下 SELECT DISTINCT
        # 整行会因为没有 JSON 等值运算符而报错；只在 paper_id 上去重即可，
        # 也不会让重复的 join 行把 total/分页算错。
        stmt = stmt.where(
            Paper.id.in_(
                select(PaperCollection.paper_id)
                .where(PaperCollection.collection_id.in_(collection_ids))
                .distinct()
            )
        )

    if use_bm25:
        # 先按过滤条件取候选（个人库规模下几千条可接受），再在 Python 里打分排序；
        # 排序要看到全集，不能用 SQL LIMIT 先截断。
        candidates = (
            await session.execute(
                stmt.order_by(Figure.page, Figure.id).limit(SEMANTIC_CANDIDATE_CAP)
            )
        ).all()
        documents: list[Document] = []
        by_id: dict[str, tuple[Figure, PaperVersion, Paper]] = {}
        for figure, version, paper in candidates:
            documents.append(_figure_document(figure, paper))
            by_id[figure.id] = (figure, version, paper)
        ranked = rank_documents(documents, pq.text)
        page = ranked[offset : offset + limit]
        rows = [by_id[figure_id] for figure_id, _score in page]
        return "semantic", False, "bm25", len(ranked), rows

    if mode == "semantic":
        # 仅评估脚本的旧基线：caption/标题 ILIKE 子串
        pattern = _like_pattern(pq.text)
        stmt = stmt.where(
            or_(
                Figure.caption.ilike(pattern, escape=LIKE_ESCAPE),
                Paper.title.ilike(pattern, escape=LIKE_ESCAPE),
            )
        )

    # 真实 total：必须在 limit/offset 之前、用同一套 where 条件数。
    # 集合过滤已用 paper_id 子查询去重，这里计数不会因 join 重复行翻倍。
    total = int(
        (
            await session.execute(
                select(func.count()).select_from(stmt.subquery())
            )
        ).scalar()
        or 0
    )

    if mode == "browse":
        stmt = (
            stmt.order_by(Paper.created_at.desc(), Figure.page, Figure.id)
            .limit(limit)
            .offset(offset)
        )
    elif mode == "provenance":
        # 确定性结果不排序，按自然顺序返回；id 兜底保证同一页/同图号的并列项
        # 在多次请求里顺序稳定（offset 翻页不重复、不漏项）。
        stmt = (
            stmt.order_by(Paper.year.desc(), Figure.page, Figure.label_num, Figure.id)
            .limit(limit)
            .offset(offset)
        )
    else:
        stmt = stmt.order_by(Figure.page, Figure.id).limit(limit).offset(offset)

    rows = (await session.execute(stmt)).all()
    return mode, degraded, engine, total, [(r[0], r[1], r[2]) for r in rows]


def normalize_label_filter(raw: str | None) -> str | None:
    """把 API 传进来的 label 归一化；无法识别时保留原样做精确匹配。"""
    if not raw:
        return None
    return normalize_label(raw) or raw.strip().lower()


__all__ = [
    "FIGURE_TYPES",
    "LABEL_STATES",
    "LIKE_ESCAPE",
    "classify",
    "normalize_label_filter",
    "search_figures",
]
