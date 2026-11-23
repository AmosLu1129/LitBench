# SPDX-License-Identifier: AGPL-3.0-or-later
"""写作大纲 + 引用/图表交叉检查（M8，2026-11-16）。

守三条：

1. 大纲按 claim.section 归到默认章节（别名归章），章节内按 order_idx 排序；
2. 正文「图 3」与卡片证据「图 4」对不上要报 issue；引用键解析不了也要报；
3. 只读派生：不写 claim、不落库；没有卡片 422。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.db import get_session
from litbench.main import app
from litbench.models import Base, Claim, Paper, PaperVersion
from litbench.outline import _extract_mentions, build_outline


@pytest.fixture
async def client(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'outline.db').as_posix()}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with Session() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        ac.session_factory = Session  # type: ignore[attr-defined]
        yield ac
    app.dependency_overrides.clear()
    await engine.dispose()


def _claim(
    claim_id: str,
    text: str,
    section: str,
    order_idx: int,
    evidence: list[dict],
    *,
    status: str = "approved",
) -> Claim:
    return Claim(
        id=claim_id,
        paper_id="a" * 32,
        text=text,
        section=section,
        status=status,
        order_idx=order_idx,
        evidence_json=evidence,
    )


async def _seed(client, *, include_draft: bool = True) -> None:
    Session = client.session_factory
    async with Session() as s:
        paper = Paper(
            id="a" * 32,
            title="多孔介质传热实验",
            title_norm="多孔介质传热实验",
            cite_key="key1",
            year=2026,
            authors_json=[{"name": "王五"}],
            entry_type="journal",
        )
        s.add(paper)
        await s.flush()
        s.add(
            PaperVersion(
                paper_id=paper.id,
                kind="uploaded",
                file_sha256="a" * 64,
                file_path="pdfs/a.pdf",
                page_count=1,
                parser="pdfium",
                parsed_at=datetime.now(UTC),
            )
        )
        claims = [
            _claim(
                "c" * 32,
                "如图 3 所示，传热系数提升。",
                "实验结果",
                2,
                [{"type": "figure", "id": "f4", "label": "图 4", "page": 5, "snippet": ""}],
            ),
            _claim(
                "d" * 32,
                "如图 4 所示，传热系数提升。",
                "实验结果",
                1,
                [{"type": "figure", "id": "f4", "label": "图 4", "page": 5, "snippet": ""}],
            ),
            _claim(
                "e" * 32,
                "见表 2，参数如下。",
                "引言",
                0,
                [{"type": "table", "id": "t2", "label": "表 2", "page": 3, "snippet": ""}],
            ),
            _claim(
                "f" * 32,
                "前人工作\\cite{missing-key} 也观察到了。",
                "讨论",
                0,
                [{"type": "summary", "id": "s1", "label": "讨论", "page": None, "snippet": ""}],
            ),
        ]
        if include_draft:
            claims.append(
                _claim(
                    "g" * 32,
                    "草稿卡片。",
                    "实验结果",
                    0,
                    [{"type": "figure", "id": "f4", "label": "图 4", "page": 5, "snippet": ""}],
                    status="draft",
                )
            )
        s.add_all(claims)
        await s.commit()


def test_extract_mentions_variants() -> None:
    figures, tables = _extract_mentions(
        "图 3、图3、Fig. 4、Figure 5(a)、\\ref{fig:6}；表 2、Table 3、\\ref{tab:4}"
    )
    assert {"3", "4", "5(a)", "6"} <= figures
    assert {"2", "3", "4"} <= tables
    assert _extract_mentions("图（未编号）")[0] == set()


def test_extract_mentions_ignores_english_words() -> None:
    # 「configure 3 / stable 3」里的 fig/tab 不是图表引用，不能误报
    figures, tables = _extract_mentions("configure 3 and stable 3")
    assert figures == set() and tables == set()
    figures, tables = _extract_mentions("See Figure 3 and Table 2.")
    assert figures == {"3"} and tables == {"2"}


def test_extract_mentions_ignores_chinese_compounds() -> None:
    # 图表/发表/代表 里的图/表字不是引用；如图/见表/第 3 图仍是引用
    figures, tables = _extract_mentions("图表 2 展示趋势，发表 2 篇论文，代表 2 个样本。")
    assert figures == set() and tables == set()
    figures, tables = _extract_mentions("如图 3 和见表 2，另见图3-1。")
    assert figures == {"3", "3-1"} and tables == {"2"}


def test_unnumbered_evidence_is_ignored() -> None:
    paper = type("P", (), {"cite_key": "key1", "title": "t"})()
    claim = type(
        "C",
        (),
        {
            "id": "x" * 32,
            "text": "正文没有图号。",
            "status": "approved",
            "paper_id": "a" * 32,
            "order_idx": 0,
            "evidence_json": [
                {"type": "figure", "id": "f1", "label": "图（未编号）", "page": 1}
            ],
        },
    )()
    # 直接走纯函数：无编号证据不参与「有证据但正文没提」的 notes
    from litbench.outline import _claim_checks

    issues, notes = _claim_checks(
        claim.text, claim.evidence_json, all_papers=[paper], claim_cite_key="key1"
    )
    assert issues == [] and notes == []


async def test_build_outline_groups_sorts_and_flags(client) -> None:
    await _seed(client)
    Session = client.session_factory
    async with Session() as s:
        payload = await build_outline(s)
    sections = payload["sections"]
    assert [row["section"] for row in sections] == ["绪论", "实验与结果", "讨论"]
    assert payload["total_claims"] == 5
    exp = sections[1]
    assert [claim["order_idx"] for claim in exp["claims"]] == [0, 1, 2]
    # 实验与结果里第一张 order=0 是草稿；order=1 匹配图 4，order=2 图 3 对不上
    mismatch = exp["claims"][2]
    assert mismatch["claim_id"] == "c" * 32
    assert mismatch["issues"][0]["kind"] == "figure"
    assert "图 3" in mismatch["issues"][0]["message"]
    matched = exp["claims"][1]
    assert matched["issues"] == []
    citation = sections[2]["claims"][0]
    assert citation["issues"][0]["kind"] == "citation"
    assert "missing-key" in citation["issues"][0]["message"]
    assert payload["issue_count"] == 2
    assert "## 实验与结果" in payload["markdown"]
    assert "⚠" in payload["markdown"]


async def test_build_outline_respects_section_order_and_drafts(client) -> None:
    await _seed(client)
    Session = client.session_factory
    async with Session() as s:
        payload = await build_outline(
            s, include_drafts=False, section_order=["实验与结果", "绪论"]
        )
    sections = payload["sections"]
    assert [row["section"] for row in sections] == ["实验与结果", "绪论", "讨论"]
    assert payload["total_claims"] == 4
    assert all(claim["status"] == "approved" for section in sections for claim in section["claims"])


async def test_outline_endpoint(client) -> None:
    await _seed(client)
    response = await client.post("/api/writing/outline", json={})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total_claims"] == 5
    assert data["issue_count"] == 2
    assert [row["section"] for row in data["sections"]] == ["绪论", "实验与结果", "讨论"]
    assert data["markdown"].startswith("# 写作大纲")

    filtered = await client.post(
        "/api/writing/outline", json={"include_drafts": False}
    )
    assert filtered.status_code == 200
    assert filtered.json()["total_claims"] == 4


async def test_outline_endpoint_empty_422(client) -> None:
    response = await client.post("/api/writing/outline", json={})
    assert response.status_code == 422
    assert "没有可整理的 claim" in response.json()["detail"]


async def test_outline_citation_index_not_truncated_at_300(client) -> None:
    """可见文献 >300 时，后面文献的 \\cite 也要能解析（不能再用前 300 篇截断）。"""
    Session = client.session_factory
    async with Session() as s:
        papers = [
            Paper(
                id=f"{index:032x}",
                title=f"Paper {index}",
                title_norm=f"paper {index}",
                cite_key=f"key-{index}",
                year=2020,
                authors_json=[{"name": "A"}],
            )
            for index in range(1, 302)
        ]
        s.add_all(papers)
        await s.flush()
        s.add(
            Claim(
                id="z" * 32,
                paper_id=f"{301:032x}",
                text="前人工作\\cite{key-301}。",
                section="绪论",
                status="approved",
                evidence_json=[{"type": "summary", "id": "s1", "label": "摘要"}],
            )
        )
        await s.commit()
    response = await client.post("/api/writing/outline", json={})
    assert response.status_code == 200, response.text
    claim = response.json()["sections"][0]["claims"][0]
    assert claim["issues"] == []
    assert claim["paper_id"] == f"{301:032x}"
