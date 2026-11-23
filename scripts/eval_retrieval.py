# SPDX-License-Identifier: AGPL-3.0-or-later
"""检索评估（M9.1）：20 条真实查询，对比旧 LIKE 基线与 BM25。

用法：

    cd server && python ../scripts/eval_retrieval.py
    python scripts/eval_retrieval.py --db sqlite+aiosqlite:///.../dev.db

指标：

* **figure hit@5 / MRR**：语义检索返回的前 5 张图里，是否有目标文献的图；
* **paper hit@1 / hit@5 / MRR**：`GET /api/papers?q=` 的同一套查询，目标文献排第几。

查询来自 dev 库 10 篇真实 arXiv 论文（英文热/流体方向），覆盖自然语言长句、
关键词短语、方法/现象描述；出处检索（图号/DOI/作者+年份）不在此评估，因为它
走确定性路径、本来就不该由排序决定。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from litbench.labels import parse_query  # noqa: E402
from litbench.models import Figure, Paper, PaperVersion  # noqa: E402
from litbench.retrieval import (  # noqa: E402
    Document,
    authors_text,
    rank_documents,
    weighted_tokens,
)
from litbench.search import search_figures  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / ".data" / "tmp" / "eval_retrieval.json"

# (query, 目标文献标题片段, 类型)
QUERIES: list[tuple[str, str, str]] = [
    ("pool boiling heater thermal properties", "Impact of Heater Thermal Properties", "natural"),
    ("nucleate pool boiling multiscale automata simulation", "Impact of Heater Thermal Properties", "natural"),
    ("boiling transition localized dryout vapor structures", "From localized dryout", "natural"),
    ("Reynolds number effects on boiling transition", "From localized dryout", "natural"),
    ("swirl flow microchannel patterned slip walls", "Swirl flow in microchannels", "natural"),
    ("slip walls enhance heat transport microchannels", "Swirl flow in microchannels", "natural"),
    ("hypersonic axisymmetric open cavities heat transfer", "Experimental Investigation of Heat Transfer in Axisymmetric", "natural"),
    ("open cavity hypersonic speeds experiment", "Experimental Investigation of Heat Transfer in Axisymmetric", "natural"),
    ("microchannel heat sink modeling considerations", "Modeling considerations about a microchannel", "natural"),
    ("field synergy fractal geometry cooling plate", "Convective Heat Transfer Optimization", "natural"),
    ("liquid cooling plate topology optimization", "Convective Heat Transfer Optimization", "natural"),
    ("nanofluid film boiling vertical cylinder", "Influence of nanoparticle shapes", "natural"),
    ("nanoparticle shapes boiling heat transfer", "Influence of nanoparticle shapes", "natural"),
    ("chip thermal design simulation measurement review", "A review of simulation", "natural"),
    ("review of simulation measurement techniques chip", "A review of simulation", "natural"),
    ("ultimate regime Rayleigh Darcy convection", "Ultimate regime in Rayleigh", "natural"),
    ("turbulent forced convection porous lattices friction", "Modelling friction and heat transfer", "natural"),
    ("fractal microchannel heat transfer", "Convective Heat Transfer Optimization", "keyword"),
    ("nanoparticle boiling", "Influence of nanoparticle shapes", "keyword"),
    ("hypersonic cavity", "Experimental Investigation of Heat Transfer in Axisymmetric", "keyword"),
]

_PAPER_FIELD_WEIGHTS = {
    "title": 3.0,
    "cite_key": 3.0,
    "abstract": 1.5,
    "authors": 1.5,
    "venue": 1.0,
    "tags": 2.0,
}


def _paper_document(paper: Paper) -> Document:
    tags = " ".join(str(tag) for tag in (paper.tags_json or []) if str(tag).strip())
    fields = {
        "title": paper.title or "",
        "cite_key": paper.cite_key or "",
        "abstract": paper.abstract or "",
        "authors": authors_text(paper.authors_json),
        "venue": paper.venue or "",
        "tags": tags,
    }
    text = f"{paper.title or ''} {paper.cite_key or ''}".lower()
    return Document(
        id=paper.id,
        tokens=weighted_tokens(fields, _PAPER_FIELD_WEIGHTS),
        text=text,
    )


def _like_paper_ids(papers: list[Paper], query: str) -> list[str]:
    needle = query.strip().lower()
    return [
        paper.id
        for paper in papers
        if needle and (needle in (paper.title or "").lower() or needle in (paper.cite_key or "").lower())
    ]


def _figure_hit(rows, gold: str, k: int = 5) -> int | None:
    for index, (_figure, _version, paper) in enumerate(rows[:k], start=1):
        if gold.lower() in (paper.title or "").lower():
            return index
    return None


def _paper_hit(rows: list[Paper], gold: str, k: int) -> int | None:
    for index, paper in enumerate(rows[:k], start=1):
        if gold.lower() in (paper.title or "").lower():
            return index
    return None


def _summary(hits: list[int | None], prefix: str) -> dict:
    hit1 = sum(1 for hit in hits if hit == 1) / len(hits)
    hit5 = sum(1 for hit in hits if hit is not None) / len(hits)
    mrr = sum((1.0 / hit) for hit in hits if hit is not None) / len(hits)
    return {f"{prefix}_hit@1": round(hit1, 4), f"{prefix}_hit@5": round(hit5, 4), f"{prefix}_mrr": round(mrr, 4)}


async def evaluate(db_url: str) -> dict:
    engine = create_async_engine(db_url, poolclass=None)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    results: list[dict] = []
    async with Session() as session:
        papers = list((await session.execute(select(Paper))).scalars())
        print(f"语料：{len(papers)} 篇文献 / "
              f"{len((await session.execute(select(Figure))).scalars().all())} 张图")
        for query, gold, kind in QUERIES:
            pq = parse_query(query)
            baseline_rows = await search_figures(
                session, pq, limit=5, semantic_engine="like"
            )
            bm25_rows = await search_figures(
                session, pq, limit=5, semantic_engine="bm25"
            )
            # (mode, degraded, engine, total, rows)
            baseline_fig = _figure_hit(baseline_rows[4], gold)
            bm25_fig = _figure_hit(bm25_rows[4], gold)

            baseline_papers = _like_paper_ids(papers, query)
            ranked = rank_documents([_paper_document(paper) for paper in papers], query)
            by_id = {paper.id: paper for paper in papers}
            bm25_papers = [by_id[paper_id] for paper_id, _score in ranked]
            baseline_paper = _paper_hit(
                [by_id[pid] for pid in baseline_papers], gold, 5
            )
            bm25_paper = _paper_hit(bm25_papers, gold, 5)

            row = {
                "query": query,
                "kind": kind,
                "mode": baseline_rows[0],
                "baseline_figure_rank": baseline_fig,
                "bm25_figure_rank": bm25_fig,
                "baseline_figure_total": baseline_rows[3],
                "bm25_figure_total": bm25_rows[3],
                "baseline_paper_rank": baseline_paper,
                "bm25_paper_rank": bm25_paper,
            }
            results.append(row)
            print(
                f"- {query[:46]:<46} fig: like={baseline_fig} bm25={bm25_fig}"
                f" | paper: like={baseline_paper} bm25={bm25_paper}"
            )
    await engine.dispose()

    figure_hits_like = [row["baseline_figure_rank"] for row in results]
    figure_hits_bm25 = [row["bm25_figure_rank"] for row in results]
    paper_hits_like = [row["baseline_paper_rank"] for row in results]
    paper_hits_bm25 = [row["bm25_paper_rank"] for row in results]
    summary = {
        "queries": len(results),
        "figure_like": _summary(figure_hits_like, "figure_like"),
        "figure_bm25": _summary(figure_hits_bm25, "figure_bm25"),
        "paper_like": _summary(paper_hits_like, "paper_like"),
        "paper_bm25": _summary(paper_hits_bm25, "paper_bm25"),
        "regressions": [
            row["query"]
            for row in results
            if row["baseline_figure_rank"] is not None
            and row["bm25_figure_rank"] is None
        ],
    }
    payload = {"summary": summary, "results": results}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("结果写入", OUT)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        default=os.environ.get(
            "DATABASE_URL",
            f"sqlite+aiosqlite:///{(Path(__file__).resolve().parents[1] / '.data' / 'dev.db').as_posix()}",
        ),
    )
    args = parser.parse_args()
    asyncio.run(evaluate(args.db))


if __name__ == "__main__":
    main()
