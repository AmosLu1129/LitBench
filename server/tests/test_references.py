# SPDX-License-Identifier: AGPL-3.0-or-later
"""参考文献条目生成（2026-11-13）：13 种格式预设 + 缺字段提示 + 卷期页补全。

守三条：

1. 字段缺就省略并 warning，**任何格式都不能把 None / 空括号漏进条目**；
2. 作者姓名按格式规则处理（GB/T 姓氏大写、APA 串列逗号、MLA 全名、Vancouver 无点）；
3. 批量顺序 = 传入顺序，重复 id 去重；未知格式 422、文献不存在 404。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.db import get_session
from litbench.main import app
from litbench.models import Base, Paper, PaperVersion


@pytest.fixture
async def client(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'refs.db').as_posix()}", poolclass=NullPool
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


def _journal(id_suffix: str = "1") -> Paper:
    return Paper(
        id="a" * 31 + id_suffix,
        title="Slip-patterned microchannels for enhanced heat transport",
        title_norm="slip patterned microchannels for enhanced heat transport",
        doi="10.1016/j.ijheatmasstransfer.2026.112128",
        cite_key="doi:10.1016/j.ijheatmasstransfer.2026.112128",
        year=2026,
        venue="International Journal of Heat and Mass Transfer",
        volume="231",
        issue="3",
        pages="112–128",
        authors_json=[
            {"firstName": "Wei", "lastName": "Zhang"},
            {"firstName": "Ming", "lastName": "Li"},
        ],
    )


def _preprint() -> Paper:
    return Paper(
        id="b" * 32,
        title="Swirl flow in microchannels",
        title_norm="swirl flow in microchannels",
        arxiv_id="2603.09607",
        cite_key="arXiv:2603.09607",
        year=2026,
        authors_json=[{"name": "Wang Lei"}],
    )


async def _seed(client) -> dict:
    Session = client.session_factory
    async with Session() as s:
        journal = _journal()
        preprint = _preprint()
        s.add_all([journal, preprint])
        await s.flush()
        s.add(
            PaperVersion(
                paper_id=journal.id,
                kind="uploaded",
                file_sha256="c" * 64,
                file_path="pdfs/c.pdf",
                page_count=1,
                parser="pdfium",
                parsed_at=datetime.now(UTC),
            )
        )
        await s.commit()
        return {"journal": journal.id, "preprint": preprint.id}


async def test_styles_endpoint(client) -> None:
    response = await client.get("/api/references/styles")
    assert response.status_code == 200, response.text
    data = response.json()
    keys = [row["key"] for row in data["styles"]]
    assert keys == [
        "gbt7714",
        "apa7",
        "ieee",
        "mla9",
        "chicago",
        "nature",
        "science",
        "acs",
        "elsevier",
        "vancouver",
        "bibtex",
        "ris",
        "custom",
    ]
    for row in data["styles"]:
        assert row["label"]
        assert row["description"]
        assert row["sample"], f"{row['key']} 没有样例"
    assert "GB/T 7714" in {row["key"]: row["label"] for row in data["styles"]}["gbt7714"]
    assert "note" in data


async def test_format_all_styles(client) -> None:
    seed = await _seed(client)
    expectations = {
        "gbt7714": ["[1] ", "ZHANG W", "[J]", "231(3): 112–128"],
        "apa7": ["Zhang, W., & Li, M.", "(2026).", "231(3), 112–128", "https://doi.org/"],
        "ieee": ["[1] W. Zhang and M. Li,", "vol. 231", "no. 3", "pp. 112–128"],
        "mla9": ["Zhang, Wei, and Ming Li.", "vol. 231", "pp. 112–128"],
        "chicago": ["Zhang, Wei, and Ming Li. 2026.", "231 (3): 112–128"],
        "nature": ["Zhang, W. & Li, M.", "(2026)"],
        "science": ["W. Zhang & M. Li.", "(2026)"],
        "acs": ["Zhang, W.; Li, M.", "2026, 231 (3), 112–128"],
        "elsevier": ["Zhang, W., Li, M., 2026.", "231 (3), 112–128"],
        "vancouver": ["[1] Zhang W, Li M.", "2026;231(3):112-128"],
        "bibtex": ["@article{doi:10.1016/j.ijheatmasstransfer.2026.112128", "volume = {231}", "number = {3}"],
        "ris": ["TY  - JOUR", "AU  - Zhang, Wei", "VL  - 231", "SP  - 112", "ER  -"],
    }
    for style, fragments in expectations.items():
        response = await client.post(
            "/api/references/format",
            json={"paper_ids": [seed["journal"]], "style": style},
        )
        assert response.status_code == 200, f"{style}: {response.text}"
        entry = response.json()["items"][0]["entry"]
        for fragment in fragments:
            assert fragment in entry, f"{style} 缺片段 {fragment!r}：{entry}"
        assert "None" not in entry, f"{style} 漏出 None：{entry}"
        assert "( )" not in entry and "[]" not in entry, f"{style} 有空括号：{entry}"


async def test_format_custom_template(client) -> None:
    seed = await _seed(client)
    response = await client.post(
        "/api/references/format",
        json={
            "paper_ids": [seed["journal"]],
            "style": "custom",
            "template": "{authors} ({year}). {title}. {journal}, {volume}({issue}), {pages}.",
        },
    )
    assert response.status_code == 200, response.text
    entry = response.json()["items"][0]["entry"]
    assert entry.startswith("ZHANG W, LI M (2026).")
    assert "231(3), 112–128" in entry or "231(3), 112–128" in entry


async def test_missing_fields_are_warned_not_faked(client) -> None:
    Session = client.session_factory
    async with Session() as s:
        bare = Paper(
            id="c" * 32,
            title="A paper with only a title",
            title_norm="a paper with only a title",
            year=2024,
        )
        s.add(bare)
        await s.commit()
    response = await client.post(
        "/api/references/format",
        json={"paper_ids": [bare.id], "style": "apa7"},
    )
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert "(2024)." in item["entry"]
    for warning in ("缺期刊/会议名", "缺页码", "缺 DOI/arXiv", "缺作者"):
        assert warning in item["warnings"], f"缺提示 {warning}：{item['warnings']}"
    assert "None" not in item["entry"]


async def test_chinese_authors_keep_order_and_characters(client) -> None:
    Session = client.session_factory
    async with Session() as s:
        paper = Paper(
            id="d" * 32,
            title="膜状凝结是工程设计的依据",
            title_norm="膜状凝结是工程设计的依据",
            year=2025,
            venue="工程热物理学报",
            volume="46",
            issue="2",
            pages="321–328",
            authors_json=[{"name": "王磊"}, {"name": "张三"}],
        )
        s.add(paper)
        await s.commit()
    response = await client.post(
        "/api/references/format",
        json={"paper_ids": [paper.id], "style": "gbt7714"},
    )
    entry = response.json()["items"][0]["entry"]
    assert entry.startswith("[1] 王磊, 张三.")
    assert "工程热物理学报, 2025, 46(2): 321–328" in entry


async def test_order_dedup_and_errors(client) -> None:
    seed = await _seed(client)
    response = await client.post(
        "/api/references/format",
        json={
            "paper_ids": [seed["preprint"], seed["journal"], seed["preprint"]],
            "style": "gbt7714",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert [item["paper_id"] for item in data["items"]] == [seed["preprint"], seed["journal"]]
    assert data["paper_count"] == 2
    assert data["content"].count("\n") == 1

    unknown_style = await client.post(
        "/api/references/format",
        json={"paper_ids": [seed["journal"]], "style": "nope"},
    )
    assert unknown_style.status_code == 422

    missing = await client.post(
        "/api/references/format",
        json={"paper_ids": ["0" * 32], "style": "apa7"},
    )
    assert missing.status_code == 404

    empty = await client.post(
        "/api/references/format", json={"paper_ids": [], "style": "apa7"}
    )
    assert empty.status_code == 422


async def test_preprint_style_uses_arxiv(client) -> None:
    seed = await _seed(client)
    response = await client.post(
        "/api/references/format",
        json={"paper_ids": [seed["preprint"]], "style": "apa7"},
    )
    entry = response.json()["items"][0]["entry"]
    assert "arXiv" in entry
    assert "https://arxiv.org/abs/2603.09607" in entry


async def test_patch_biblio_fields(client) -> None:
    seed = await _seed(client)
    patched = await client.patch(
        f"/api/papers/{seed['journal']}",
        json={"volume": "232", "issue": "4", "pages": "1-9"},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert (body["volume"], body["issue"], body["pages"]) == ("232", "4", "1-9")

    response = await client.post(
        "/api/references/format",
        json={"paper_ids": [seed["journal"]], "style": "ieee"},
    )
    entry = response.json()["items"][0]["entry"]
    assert "vol. 232" in entry and "no. 4" in entry and "pp. 1-9" in entry

    cleared = await client.patch(f"/api/papers/{seed['journal']}", json={"volume": ""})
    assert cleared.status_code == 200
    assert cleared.json()["volume"] is None
