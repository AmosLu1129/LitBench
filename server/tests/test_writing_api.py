# SPDX-License-Identifier: AGPL-3.0-or-later
"""M5 写作侧 API 测试：claim 卡片 / 对比矩阵 / LaTeX·BibTeX 导出 / 引用校验。

重点守四条：

1. claim 的证据是硬约束（非空 + 真实存在 + 同版本），不能落一条「空口论断」；
2. 矩阵完全派生，不落库；缺文献/超篇数要报可读错误；
3. 导出能编译：LaTeX 正文 + refs.bib 成对返回，特殊字符要转义；
4. 引用校验对 cite_key / DOI / arXiv 三种写法都能解析，找不到给建议。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.db import get_session
from litbench.main import app
from litbench.models import (
    Base,
    Block,
    Figure,
    Paper,
    PaperVersion,
    Summary,
    TableBlock,
)


@pytest.fixture
async def client(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'writing.db').as_posix()}", poolclass=NullPool
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
        paper = Paper(
            title="相变传热研究：凝结与沸腾",
            title_norm="相变传热研究凝结与沸腾",
            doi="10.1234/abc",
            cite_key="doi:10.1234/abc",
            year=2024,
            venue="Int. J. Heat Mass Transf.",
            authors_json=[{"firstName": "Wei", "lastName": "Zhang"}, "Li Ming"],
            abstract="本文研究膜状凝结与核态沸腾的换热规律，并给出工程计算方法。",
            status="parsed",
        )
        paper2 = Paper(
            title="纳米流体强化传热综述",
            title_norm="纳米流体强化传热综述",
            arxiv_id="2601.00001",
            cite_key="arXiv:2601.00001",
            year=2026,
            authors_json=[{"name": "Wang Lei"}],
            status="parsed",
        )
        s.add_all([paper, paper2])
        await s.flush()
        version = PaperVersion(
            paper_id=paper.id,
            kind="uploaded",
            file_sha256="a" * 64,
            file_path="pdfs/a.pdf",
            page_count=3,
            parser="pdfium",
            parsed_at=datetime.now(timezone.utc),
        )
        version2 = PaperVersion(
            paper_id=paper2.id,
            kind="uploaded",
            file_sha256="b" * 64,
            file_path="pdfs/b.pdf",
            page_count=2,
            parser="pdfium",
            parsed_at=datetime.now(timezone.utc),
        )
        s.add_all([version, version2])
        await s.flush()
        block = Block(
            paper_version_id=version.id,
            page=2,
            order_idx=0,
            type="text",
            text="膜状凝结是工程设计的依据，珠状凝结换热系数更高但不稳定。",
            bbox_json=[0, 0, 10, 10],
        )
        block_other = Block(
            paper_version_id=version2.id,
            page=1,
            order_idx=0,
            type="text",
            text="纳米颗粒形状会显著影响膜态沸腾的临界热流密度。",
            bbox_json=[0, 0, 10, 10],
        )
        s.add_all([block, block_other])
        await s.flush()
        figure = Figure(
            paper_version_id=version.id,
            page=2,
            label_raw="图 7-1",
            label_norm="图7-1",
            caption="不同润湿能力壁面上液膜形成的接触角",
            bbox_json=[0, 0, 100, 100],
            cite_key="P1v1-F1",
        )
        table = TableBlock(
            paper_version_id=version.id,
            page=3,
            label_raw="表 7-1",
            label_norm="表7-1",
            caption="膜状凝结与珠状凝结的换热系数对比",
            html="<table><tr><td>1</td></tr></table>",
            quality="structured",
        )
        summary = Summary(
            paper_version_id=version.id,
            section_key="k1",
            section_title="7.1 凝结传热的模式",
            content="凝结分为珠状与膜状两种基本形式。",
            key_points_json=["珠状凝结换热更强", "膜状凝结是工程设计依据"],
            evidence_block_ids_json=[block.id],
            source="agent",
            status="published",
        )
        s.add_all([figure, table, summary])
        await s.commit()
        return {
            "paper_id": paper.id,
            "paper2_id": paper2.id,
            "version_id": version.id,
            "version2_id": version2.id,
            "block_id": block.id,
            "block_other_id": block_other.id,
            "figure_id": figure.id,
            "table_id": table.id,
            "summary_id": summary.id,
        }


async def _create_claim(client, seed: dict, **overrides) -> dict:
    body = {
        "paper_id": seed["paper_id"],
        "text": "膜状凝结是工程设计的依据",
        "section": "引言",
        "evidence": [{"type": "block", "id": seed["block_id"]}],
        **overrides,
    }
    response = await client.post("/api/claims", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_claim_resolves_evidence(client) -> None:
    seed = await _seed(client)
    claim = await _create_claim(client, seed)
    assert claim["paper_title"].startswith("相变传热研究")
    assert claim["cite_key"] == "doi:10.1234/abc"
    assert len(claim["evidence"]) == 1
    evidence = claim["evidence"][0]
    assert evidence["type"] == "block"
    assert evidence["label"].startswith("p2 text")
    assert "膜状凝结" in evidence["snippet"]
    assert claim["section"] == "引言"
    assert claim["status"] == "draft"


async def test_claim_requires_existing_evidence(client) -> None:
    seed = await _seed(client)
    no_evidence = await client.post(
        "/api/claims",
        json={"paper_id": seed["paper_id"], "text": "空口无凭的论断", "evidence": []},
    )
    assert no_evidence.status_code == 422

    missing = await client.post(
        "/api/claims",
        json={
            "paper_id": seed["paper_id"],
            "text": "证据不存在",
            "evidence": [{"type": "block", "id": "0" * 32}],
        },
    )
    assert missing.status_code == 422

    foreign = await client.post(
        "/api/claims",
        json={
            "paper_id": seed["paper_id"],
            "text": "跨版本证据",
            "evidence": [{"type": "block", "id": seed["block_other_id"]}],
        },
    )
    assert foreign.status_code == 422
    assert "版本" in foreign.text


async def test_claim_list_filter_and_update(client) -> None:
    seed = await _seed(client)
    first = await _create_claim(client, seed, section="引言", status="approved")
    await _create_claim(client, seed, text="第二条论断", section="讨论")

    listed = await client.get(f"/api/claims?paper_id={seed['paper_id']}")
    assert listed.status_code == 200
    assert len(listed.json()) == 2

    approved = await client.get("/api/claims?status=approved")
    assert [row["claim_id"] for row in approved.json()] == [first["claim_id"]]

    asked = await client.get("/api/claims?q=第二条")
    assert len(asked.json()) == 1

    patched = await client.patch(
        f"/api/claims/{first['claim_id']}",
        json={"text": "改写后的论断", "status": "draft", "tags": ["核心"]},
    )
    assert patched.status_code == 200
    assert patched.json()["text"] == "改写后的论断"
    assert patched.json()["tags"] == ["核心"]

    deleted = await client.delete(f"/api/claims/{first['claim_id']}")
    assert deleted.status_code == 204
    left = await client.get(f"/api/claims?paper_id={seed['paper_id']}")
    assert len(left.json()) == 1


async def test_matrix_derives_columns(client) -> None:
    seed = await _seed(client)
    await _create_claim(client, seed)
    response = await client.get(
        f"/api/matrix?paper_ids={seed['paper_id']},{seed['paper2_id']}"
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert [paper["paper_id"] for paper in data["papers"]] == [
        seed["paper_id"],
        seed["paper2_id"],
    ]
    rows = {row["key"]: row for row in data["rows"]}
    assert rows["title"]["values"][0]["text"].startswith("相变传热")
    assert "2024" in rows["meta"]["values"][0]["text"]
    assert "膜状" in rows["abstract"]["values"][0]["text"]
    assert "珠状凝结" in rows["findings"]["values"][0]["text"]
    assert rows["assets"]["values"][0]["text"] == "1 图 · 1 表"
    assert "膜状凝结" in rows["claims"]["values"][0]["text"]
    assert rows["claims"]["values"][1]["text"] == "（还没有写作卡片）"

    missing = await client.get("/api/matrix?paper_ids=" + "0" * 32)
    assert missing.status_code == 422


async def test_export_latex_with_bib(client) -> None:
    seed = await _seed(client)
    claim = await _create_claim(client, seed, text="膜状凝结是设计依据 100% 可靠_a")
    response = await client.post(
        "/api/writing/export",
        json={"format": "latex", "paper_ids": [seed["paper_id"]], "title": "我的综述"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["filename"] == "我的综述.tex"
    content = data["content"]
    assert r"\documentclass" in content
    assert r"\section{引言}" in content
    assert r"\cite{doi:10.1234/abc}" in content
    assert r"100\%" in content, "LaTeX 特殊字符必须转义"
    assert r"\_" in content
    assert "refs.bib" in data["extra_files"]
    bib = data["extra_files"]["refs.bib"]
    assert "@article{doi:10.1234/abc" in bib
    assert "Int. J. Heat Mass Transf." in bib
    assert claim["claim_id"]

    with_comments = await client.post(
        "/api/writing/export",
        json={
            "format": "latex",
            "paper_ids": [seed["paper_id"]],
            "with_evidence_comments": True,
        },
    )
    assert f"% [claim {claim['claim_id']}" in with_comments.json()["content"]


async def test_export_bibtex_and_markdown(client) -> None:
    seed = await _seed(client)
    await _create_claim(client, seed)

    bib = await client.post(
        "/api/writing/export",
        json={"format": "bibtex", "paper_ids": [seed["paper_id"], seed["paper2_id"]]},
    )
    assert bib.status_code == 200
    body = bib.json()
    assert body["filename"] == "refs.bib"
    assert "arXiv:2601.00001" in body["content"]
    assert "eprint = {2601.00001}" in body["content"]
    assert body["paper_count"] == 2

    md = await client.post(
        "/api/writing/export",
        json={"format": "markdown", "paper_ids": [seed["paper_id"]]},
    )
    assert md.status_code == 200
    assert "## 引言" in md.json()["content"]
    assert "[@doi:10.1234/abc]" in md.json()["content"]

    bad = await client.post("/api/writing/export", json={"format": "pdf"})
    assert bad.status_code == 422


async def test_export_requires_claims(client) -> None:
    seed = await _seed(client)
    empty = await client.post(
        "/api/writing/export", json={"format": "latex", "paper_ids": [seed["paper_id"]]}
    )
    assert empty.status_code == 422
    assert "没有可导出" in empty.text


async def test_export_only_approved(client) -> None:
    seed = await _seed(client)
    await _create_claim(client, seed, status="draft")
    response = await client.post(
        "/api/writing/export",
        json={"format": "latex", "include_drafts": False},
    )
    assert response.status_code == 422
    assert "没有可导出" in response.text


async def test_check_citations(client) -> None:
    await _seed(client)
    text = (
        r"凝结是设计依据~\cite{doi:10.1234/abc}；"
        r"纳米流体见~\cite{2601.00001}；"
        r"不存在的~\cite{arXiv:9999.99999}。"
    )
    response = await client.post("/api/writing/check-citations", json={"text": text})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 3
    assert data["resolved"] == 2
    assert data["unknown"] == 1
    statuses = {item["key"]: item["status"] for item in data["citations"]}
    assert statuses["doi:10.1234/abc"] == "resolved"
    assert statuses["2601.00001"] == "resolved"
    unknown = next(item for item in data["citations"] if item["status"] == "unknown")
    assert unknown["suggestion"] in ("arXiv:2601.00001", "doi:10.1234/abc")

    none = await client.post("/api/writing/check-citations", json={"text": "没有引用"})
    assert none.json()["total"] == 0
    assert "没有检测到" in none.json()["message"]


# ---------------------------------------------------------------------------
# 写作台小修（2026-11-15）：claim 编辑历史 / 矩阵自定义维度
# ---------------------------------------------------------------------------
async def test_claim_revision_history_and_restore(client) -> None:
    seed = await _seed(client)
    claim = await _create_claim(client, seed, text="第一版论断")
    claim_id = claim["claim_id"]

    # 没改过 → 没有历史
    empty = await client.get(f"/api/claims/{claim_id}/revisions")
    assert empty.status_code == 200
    assert empty.json() == []

    patched = await client.patch(
        f"/api/claims/{claim_id}",
        json={
            "text": "第二版论断",
            "section": "方法",
            "status": "approved",
            "tags": ["核心"],
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["text"] == "第二版论断"

    history = (await client.get(f"/api/claims/{claim_id}/revisions")).json()
    assert len(history) == 1
    old = history[0]
    assert old["text"] == "第一版论断"
    assert old["section"] == "引言"
    assert old["status"] == "draft"
    assert old["reason"] == "edited"
    assert old["edited_by"] == "single"
    assert old["evidence"] and old["evidence"][0]["type"] == "block"

    # 同内容再保存一次：不算一次编辑，不涨历史
    same = await client.patch(
        f"/api/claims/{claim_id}",
        json={"text": "第二版论断", "section": "方法", "status": "approved", "tags": ["核心"]},
    )
    assert same.status_code == 200
    assert len((await client.get(f"/api/claims/{claim_id}/revisions")).json()) == 1

    restored = await client.post(
        f"/api/claims/{claim_id}/revisions/{old['revision_id']}/restore"
    )
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["text"] == "第一版论断"
    assert body["section"] == "引言"
    assert body["status"] == "draft"
    assert body["tags"] == []

    # 恢复动作本身也留一份（当前 = 第二版），历史链不断
    history2 = (await client.get(f"/api/claims/{claim_id}/revisions")).json()
    assert [row["reason"] for row in history2] == ["restored", "edited"]
    assert history2[0]["text"] == "第二版论断"
    assert history2[1]["text"] == "第一版论断"

    missing = await client.post(
        f"/api/claims/{claim_id}/revisions/{'0' * 32}/restore"
    )
    assert missing.status_code == 404

    no_claim = await client.get(f"/api/claims/{'0' * 32}/revisions")
    assert no_claim.status_code == 404


async def test_matrix_dimensions_catalog_and_subset(client) -> None:
    seed = await _seed(client)
    catalog = await client.get("/api/matrix/dimensions")
    assert catalog.status_code == 200, catalog.text
    dims = catalog.json()["dimensions"]
    keys = [row["key"] for row in dims]
    assert keys == [
        "title",
        "authors",
        "meta",
        "abstract",
        "findings",
        "assets",
        "claims",
        "cite",
        "type",
        "tags",
    ]
    defaults = [row["key"] for row in dims if row["default"]]
    assert defaults == ["title", "authors", "meta", "abstract", "findings", "assets", "claims"]
    for row in dims:
        assert row["label"] and row["description"]

    # 给文献补标签，验证可选维度确实取库内数据
    Session = client.session_factory
    async with Session() as s:
        paper = await s.get(Paper, seed["paper_id"])
        paper.tags_json = ["传热", "综述"]
        await s.commit()

    response = await client.get(
        f"/api/matrix?paper_ids={seed['paper_id']}&dimensions=title,tags,cite,type"
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert [row["key"] for row in data["rows"]] == ["title", "tags", "cite", "type"]
    rows = {row["key"]: row for row in data["rows"]}
    assert rows["tags"]["values"][0]["text"] == "传热、综述"
    assert rows["cite"]["values"][0]["text"] == "doi:10.1234/abc"
    assert rows["type"]["values"][0]["text"] == "期刊"

    # 顺序按请求，重复 key 去重
    ordered = await client.get(
        f"/api/matrix?paper_ids={seed['paper_id']}&dimensions=claims,title,claims"
    )
    assert [row["key"] for row in ordered.json()["rows"]] == ["claims", "title"]

    unknown = await client.get(
        f"/api/matrix?paper_ids={seed['paper_id']}&dimensions=title,nope"
    )
    assert unknown.status_code == 422
    assert "未知矩阵维度" in unknown.text

    # 不传 dimensions = 原来的默认 7 行（向后兼容）
    legacy = await client.get(f"/api/matrix?paper_ids={seed['paper_id']}")
    assert [row["key"] for row in legacy.json()["rows"]] == defaults
