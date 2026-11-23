# SPDX-License-Identifier: AGPL-3.0-or-later
"""学位论文 LaTeX 包（M8，2026-11-15）：[D] 条目 / 骨架导出 / 引用交叉检查。

守六条（docs/17 §11.3）：

1. 学位论文元数据（学校/学位/出版地）能落库、能 PATCH、能从 Zotero 映射进来；
2. GB/T 给 ``[D]``、BibTeX 给 ``@phdthesis`` / ``@mastersthesis``、RIS 给 ``THES``，
   其它风格不能继续输出 ``[J]``；缺字段只 warning 不编造；
3. 导出的 .tex 是能编译的 ctexbook 骨架：封面占位 / 摘要 / 目录 / 章节 claim /
   ``\\cite{}`` / refs.bib / references_gbt.tex；
4. claim 的 section 会按别名归章（引言→绪论、实验结果→实验与结果），
   自定义 ``chapter_order`` 优先，对不上的 section 作为额外章节保留；
5. 引用检查是确定性的：``\\cite`` 找不到给建议，``\\ref{fig/tab}`` 比对图号/表号，
   unused = 范围内没被正文引到的文献；
6. 认证打开时只导出当前用户可见的文献 + 自己的 claim（管理员仍按可见性）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.config import settings
from litbench.db import get_session
from litbench.main import app
from litbench.models import (
    Base,
    Claim,
    Figure,
    Paper,
    PaperVersion,
    TableBlock,
)
from litbench.thesis import export_thesis
from litbench.zotero import _item_from_mapping


@pytest.fixture
async def client(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'thesis.db').as_posix()}", poolclass=NullPool
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


async def _seed(client) -> dict:
    Session = client.session_factory
    async with Session() as s:
        thesis = Paper(
            id="t" * 32,
            title="基于深度学习的微通道传热预测方法研究",
            title_norm="基于深度学习的微通道传热预测方法研究",
            cite_key="thesis:zhang2023",
            year=2023,
            authors_json=[{"name": "张三"}],
            entry_type="thesis",
            thesis_school="清华大学",
            thesis_degree="博士",
            thesis_place="北京",
            status="parsed",
        )
        journal = Paper(
            id="j" * 32,
            title="纳米流体强化传热的实验研究",
            title_norm="纳米流体强化传热的实验研究",
            doi="10.1234/abc",
            cite_key="doi:10.1234/abc",
            year=2024,
            venue="工程热物理学报",
            volume="45",
            issue="2",
            pages="321–328",
            authors_json=[{"name": "王磊"}, {"name": "李四"}],
            status="parsed",
        )
        s.add_all([thesis, journal])
        await s.flush()
        version = PaperVersion(
            paper_id=thesis.id,
            kind="uploaded",
            file_sha256="a" * 64,
            file_path="pdfs/thesis.pdf",
            page_count=3,
            parser="pdfium",
            parsed_at=datetime.now(UTC),
        )
        s.add(version)
        await s.flush()
        figure = Figure(
            paper_version_id=version.id,
            page=2,
            label_raw="图 7-1",
            label_norm="图7-1",
            caption="微通道传热实验装置",
            bbox_json=[0, 0, 10, 10],
            cite_key="P1v1-F1",
        )
        table = TableBlock(
            paper_version_id=version.id,
            page=3,
            label_raw="表 7-1",
            label_norm="表7-1",
            caption="不同工质的换热系数对比",
            html="<table><tr><td>1</td></tr></table>",
            quality="structured",
        )
        s.add_all([figure, table])
        await s.flush()
        s.add_all(
            [
                Claim(
                    paper_id=thesis.id,
                    paper_version_id=version.id,
                    text="深度学习方法可以预测微通道的传热系数",
                    section="摘要",
                    order_idx=0,
                    status="approved",
                    evidence_json=[],
                ),
                Claim(
                    paper_id=thesis.id,
                    paper_version_id=version.id,
                    text="已有研究多采用经验关联式",
                    section="引言",
                    order_idx=0,
                    status="approved",
                    evidence_json=[],
                ),
                Claim(
                    paper_id=journal.id,
                    text="纳米流体可显著提升换热系数",
                    section="实验结果",
                    order_idx=0,
                    status="draft",
                    evidence_json=[],
                ),
            ]
        )
        await s.commit()
        return {"thesis": thesis.id, "journal": journal.id, "figure": figure.id, "table": table.id}


# ---------------------------------------------------------------------------
# 元数据：Zotero 映射 / PATCH / [D] 条目
# ---------------------------------------------------------------------------
def test_zotero_item_type_maps_to_entry_type() -> None:
    thesis = _item_from_mapping(
        {"key": "T1"},
        {
            "itemType": "thesis",
            "title": "基于深度学习的传热预测",
            "university": "清华大学",
            "thesisType": "博士",
            "place": "北京",
            "date": "2023",
        },
    )
    assert thesis is not None
    assert thesis.entry_type == "thesis"
    assert (thesis.thesis_school, thesis.thesis_degree, thesis.thesis_place) == (
        "清华大学",
        "博士",
        "北京",
    )

    journal = _item_from_mapping(
        {"key": "J1"}, {"itemType": "journalArticle", "title": "A journal paper"}
    )
    assert journal is not None and journal.entry_type == "journal"

    conference = _item_from_mapping(
        {"key": "C1"}, {"itemType": "conferencePaper", "title": "A conference paper"}
    )
    assert conference is not None and conference.entry_type == "conference"

    unknown = _item_from_mapping(
        {"key": "U1"}, {"itemType": "webpage", "title": "A webpage"}
    )
    assert unknown is not None and unknown.entry_type == "other"


async def test_patch_entry_type_and_thesis_fields(client) -> None:
    seed = await _seed(client)
    patched = await client.patch(
        f"/api/papers/{seed['journal']}",
        json={
            "entry_type": "thesis",
            "thesis_school": "北京大学",
            "thesis_degree": "硕士",
            "thesis_place": "北京",
        },
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["entry_type"] == "thesis"
    assert (body["thesis_school"], body["thesis_degree"], body["thesis_place"]) == (
        "北京大学",
        "硕士",
        "北京",
    )

    invalid = await client.patch(
        f"/api/papers/{seed['journal']}", json={"entry_type": "novel"}
    )
    assert invalid.status_code == 422


async def test_gbt_thesis_uses_d_and_missing_fields_warn(client) -> None:
    seed = await _seed(client)
    response = await client.post(
        "/api/references/format",
        json={"paper_ids": [seed["thesis"]], "style": "gbt7714"},
    )
    assert response.status_code == 200, response.text
    entry = response.json()["items"][0]
    assert "[D]" in entry["entry"]
    assert "北京: 清华大学, 2023" in entry["entry"]
    assert "[J]" not in entry["entry"]
    assert entry["warnings"] == []

    # 缺字段：只提示，不编造
    Session = client.session_factory
    async with Session() as s:
        bare = Paper(
            id="b" * 32,
            title="只有题名的学位论文",
            title_norm="只有题名的学位论文",
            entry_type="thesis",
        )
        s.add(bare)
        await s.commit()
    bare_entry = (
        await client.post(
            "/api/references/format",
            json={"paper_ids": [bare.id], "style": "gbt7714"},
        )
    ).json()["items"][0]
    assert "[D]" in bare_entry["entry"]
    for warning in ("缺出版年", "缺学校", "缺学位类型", "缺出版地", "缺作者"):
        assert warning in bare_entry["warnings"], bare_entry["warnings"]
    assert "None" not in bare_entry["entry"]


async def test_bibtex_ris_apa_ieee_thesis(client) -> None:
    seed = await _seed(client)
    bib = (
        await client.post(
            "/api/references/format",
            json={"paper_ids": [seed["thesis"]], "style": "bibtex"},
        )
    ).json()["items"][0]["entry"]
    assert "@phdthesis{thesis:zhang2023" in bib
    assert "school = {清华大学}" in bib
    assert "address = {北京}" in bib
    assert "type = {博士}" in bib
    assert "@article" not in bib

    apa = (
        await client.post(
            "/api/references/format",
            json={"paper_ids": [seed["thesis"]], "style": "apa7"},
        )
    ).json()["items"][0]["entry"]
    assert "[Doctoral dissertation, 清华大学]" in apa
    assert "[J]" not in apa

    ieee = (
        await client.post(
            "/api/references/format",
            json={"paper_ids": [seed["thesis"]], "style": "ieee"},
        )
    ).json()["items"][0]["entry"]
    assert "Ph.D. dissertation" in ieee and "清华大学" in ieee
    assert "[J]" not in ieee

    ris = (
        await client.post(
            "/api/references/format",
            json={"paper_ids": [seed["thesis"]], "style": "ris"},
        )
    ).json()["items"][0]["entry"]
    assert "TY  - THES" in ris
    assert "PB  - 清华大学" in ris and "CY  - 北京" in ris and "M3  - 博士" in ris

    # 硕士 → @mastersthesis（degree 决定，不靠猜）
    Session = client.session_factory
    async with Session() as s:
        master = Paper(
            id="m" * 32,
            title="某硕士论文",
            title_norm="某硕士论文",
            cite_key="thesis:master",
            year=2022,
            entry_type="thesis",
            thesis_school="浙江大学",
            thesis_degree="硕士",
            authors_json=[{"name": "李四"}],
        )
        s.add(master)
        await s.commit()
    master_bib = (
        await client.post(
            "/api/references/format",
            json={"paper_ids": [master.id], "style": "bibtex"},
        )
    ).json()["items"][0]["entry"]
    assert "@mastersthesis{thesis:master" in master_bib


# ---------------------------------------------------------------------------
# 骨架导出
# ---------------------------------------------------------------------------
async def test_export_thesis_skeleton_and_extra_files(client) -> None:
    seed = await _seed(client)
    response = await client.post(
        "/api/thesis/export",
        json={
            "title": "微通道传热预测方法研究",
            "author": "张三",
            "school": "清华大学",
            "degree": "博士",
            "advisor": "李老师",
            "year": 2026,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["filename"].endswith(".tex")
    content = data["content"]
    for fragment in (
        r"\documentclass[UTF8,12pt,openany]{ctexbook}",
        r"\begin{titlepage}",
        "清华大学",
        r"\chapter*{摘要}",
        r"\chapter{绪论}",
        r"\chapter{实验与结果}",
        "已有研究多采用经验关联式",
        r"\cite{thesis:zhang2023}",
        r"\bibliography{refs}",
    ):
        assert fragment in content, f"缺片段 {fragment!r}"
    assert "纳米流体可显著提升换热系数" in content  # include_drafts 默认 True
    assert data["claim_count"] == 3
    assert data["paper_count"] == 2
    assert data["chapter_count"] == 5
    assert data["warnings"] == []

    extra = data["extra_files"]
    assert "@phdthesis{thesis:zhang2023" in extra["refs.bib"]
    assert "@article{doi:10.1234/abc" in extra["refs.bib"]
    gbt = extra["references_gbt.tex"]
    assert r"\begin{thebibliography}{99}" in gbt
    assert r"\bibitem{thesis:zhang2023}" in gbt
    assert "[D]" in gbt


async def test_export_thesis_scope_drafts_and_chapter_order(client) -> None:
    seed = await _seed(client)
    # 只导出 approved + 指定文献范围：draft 卡片（journal）不出现
    response = await client.post(
        "/api/thesis/export",
        json={
            "title": "只用已认可卡片",
            "include_drafts": False,
            "paper_ids": [seed["thesis"]],
            "chapter_order": ["引言", "方法"],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    content = data["content"]
    assert data["claim_count"] == 2
    assert data["paper_count"] == 1
    assert "纳米流体可显著提升换热系数" not in content
    assert r"\chapter{引言}" in content
    assert r"\chapter{方法}" in content
    assert r"\chapter{绪论}" not in content

    # 没有 claim 但有文献：仍可导出空骨架（章节留 TODO 注释）
    only_refs = await client.post(
        "/api/thesis/export",
        json={
            "paper_ids": [seed["journal"]],
            "title": "只有参考文献",
            "include_drafts": False,
        },
    )
    assert only_refs.status_code == 200, only_refs.text
    assert only_refs.json()["claim_count"] == 0
    assert only_refs.json()["paper_count"] == 1


async def test_export_thesis_requires_content(client) -> None:
    empty = await client.post("/api/thesis/export", json={})
    assert empty.status_code == 422


async def test_export_thesis_extra_chapter_keeps_unmatched_section(client) -> None:
    seed = await _seed(client)
    Session = client.session_factory
    async with Session() as s:
        s.add(
            Claim(
                paper_id=seed["thesis"],
                text="这一节不在默认章节里",
                section="讨论与展望",
                status="approved",
                evidence_json=[],
            )
        )
        await s.commit()
    data = (
        await client.post("/api/thesis/export", json={"title": "额外章节"})
    ).json()
    assert r"\chapter{讨论与展望}" in data["content"]
    assert "这一节不在默认章节里" in data["content"]


async def test_export_thesis_missing_metadata_warns_not_fakes(client) -> None:
    await _seed(client)
    data = (await client.post("/api/thesis/export", json={})).json()
    assert "缺论文题目，封面会留空" in data["warnings"]
    assert "缺学校，封面会留空" in data["warnings"]
    assert "缺年份，封面会留空" in data["warnings"]
    assert r"\underline{\hspace{6cm}}" in data["content"]


# ---------------------------------------------------------------------------
# 引用 / 图表标签交叉检查
# ---------------------------------------------------------------------------
async def test_thesis_check_missing_unused_fig_tab(client) -> None:
    seed = await _seed(client)
    text = (
        r"深度学习预测~\cite{thesis:zhang2023}"
        "\n"
        r"错误引用~\cite{nope}"
        "\n"
        r"见图~\ref{fig:图7-1} 与图~\ref{fig:图9-9}"
        "\n"
        rf"见表~\ref{{tab:表7-1}}、\ref{{tab:{seed['table']}}} 与表~\ref{{tab:表9-9}}"
    )
    response = await client.post("/api/thesis/check", json={"text": text})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 2
    assert data["resolved"] == 1
    assert data["unknown"] == 1
    assert [item["status"] for item in data["citations"]] == ["resolved", "unknown"]
    assert data["citations"][1]["suggestion"] is None or isinstance(
        data["citations"][1]["suggestion"], str
    )
    assert data["figures"][0]["status"] == "resolved"
    assert data["figures"][1]["status"] == "unknown"
    # 表号（表7-1）与降级写法（table_id）都要能解析；不存在的表号报 unknown
    assert [item["status"] for item in data["tables"]] == ["resolved", "resolved", "unknown"]
    assert set(data["missing"]) == {"cite:nope", "fig:图9-9", "tab:表9-9"}
    assert data["unused"] == ["doi:10.1234/abc"]
    assert any("图 图9-9" in w for w in data["warnings"])
    assert "3 项找不到" in data["message"]


async def test_thesis_check_paper_scope_and_auth_visibility(
    client, monkeypatch
) -> None:
    seed = await _seed(client)
    data = (
        await client.post(
            "/api/thesis/check",
            json={"text": r"\cite{thesis:zhang2023}", "paper_ids": [seed["thesis"]]},
        )
    ).json()
    assert data["unused"] == []  # 范围只有 thesis，且已引用
    assert data["total"] == 1

    # 认证打开：A 只能导出自己的 claim，B 的私有文献完全不可见
    monkeypatch.setattr(settings, "auth_enabled", True)
    Session = client.session_factory
    async with Session() as s:
        a_paper = Paper(
            id="a" * 32,
            title="A 的文献",
            title_norm="a 的文献",
            cite_key="paper:a",
            owner_user_id="user-a",
        )
        b_paper = Paper(
            id="b2" * 16,
            title="B 的文献",
            title_norm="b 的文献",
            cite_key="paper:b",
            owner_user_id="user-b",
        )
        s.add_all([a_paper, b_paper])
        await s.flush()
        s.add_all(
            [
                Claim(
                    paper_id=a_paper.id,
                    text="A 的论断",
                    section="引言",
                    status="approved",
                    owner_user_id="user-a",
                    evidence_json=[],
                ),
                Claim(
                    paper_id=b_paper.id,
                    text="B 的论断",
                    section="引言",
                    status="approved",
                    owner_user_id="user-b",
                    evidence_json=[],
                ),
            ]
        )
        await s.commit()
        user_a = SimpleNamespace(
            id="user-a", username="a", display_name="A", role="member", is_active=True
        )
        payload = await export_thesis(
            s, title="A 的学位论文", paper_ids=[a_paper.id], user=user_a
        )
    assert payload["claim_count"] == 1
    assert "A 的论断" in payload["content"]
    assert "B 的论断" not in payload["content"]
    assert "paper:b" not in payload["extra_files"]["refs.bib"]
