# SPDX-License-Identifier: AGPL-3.0-or-later
"""元数据补全（M8，2026-11-16）：Crossref / arXiv 查询、diff、显式 apply。

守三条：

1. 查询只读外部 API，不落库；apply 默认只填空白，覆盖已有值要 overwrite=true；
2. 查不到 404、网络失败 502、参数错 422、覆盖冲突 409，不吞错；
3. 测试用固定 JSON/XML 样本，不依赖外网；真机验收另外跑真实 DOI/arXiv。
"""

from __future__ import annotations

from datetime import UTC, datetime
from xml.etree import ElementTree

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench import metadata
from litbench.db import get_session
from litbench.main import app
from litbench.metadata import (
    MetadataConflict,
    MetadataError,
    MetadataNotFound,
    MetadataUnavailable,
    apply_metadata,
    build_field_diff,
    lookup_arxiv,
    lookup_crossref,
    normalize_arxiv_id,
    normalize_doi,
)
from litbench.models import Base, Paper, PaperVersion

CROSSREF_SAMPLE = {
    "message": {
        "DOI": "10.1038/nature12373",
        "type": "journal-article",
        "title": ["Nanometre-scale thermometry in a living cell"],
        "container-title": ["Nature"],
        "volume": "500",
        "issue": "7460",
        "page": "54-58",
        "issued": {"date-parts": [[2013, 7, 31]]},
        "author": [
            {"given": "G.", "family": "Kucsko"},
            {"name": "The Collaboration"},
        ],
    }
}

ARXIV_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <title>Attention Is All You Need</title>
    <author><name>Ashish Vaswani</name></author>
    <author><name>Noam Shazeer</name></author>
    <published>2017-06-12T17:57:34Z</published>
    <arxiv:journal_ref>NeurIPS 2017</arxiv:journal_ref>
    <arxiv:doi>10.1000/xyz</arxiv:doi>
  </entry>
</feed>
"""


@pytest.fixture
async def client(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'meta.db').as_posix()}", poolclass=NullPool
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


def _paper(**overrides) -> Paper:
    base = dict(
        id="a" * 32,
        title="Untitled upload",
        title_norm="untitled upload",
        authors_json=[],
        year=None,
        venue=None,
        volume=None,
        issue=None,
        pages=None,
        doi=None,
        arxiv_id=None,
        entry_type="journal",
    )
    base.update(overrides)
    return Paper(**base)


async def _seed(client, paper: Paper | None = None) -> Paper:
    paper = paper or _paper()
    Session = client.session_factory
    async with Session() as s:
        s.add(paper)
        await s.flush()
        s.add(
            PaperVersion(
                paper_id=paper.id,
                kind="uploaded",
                file_sha256="d" * 64,
                file_path="pdfs/d.pdf",
                page_count=1,
                parser="pdfium",
                parsed_at=datetime.now(UTC),
            )
        )
        await s.commit()
    return paper


# ---------------------------------------------------------------------------
# 清洗 / 映射
# ---------------------------------------------------------------------------
def test_normalize_identifiers() -> None:
    assert normalize_doi("https://doi.org/10.1038/NATURE12373.") == "10.1038/NATURE12373"
    assert normalize_doi("doi: 10.1038/nature12373") == "10.1038/nature12373"
    assert normalize_arxiv_id("arXiv:1706.03762v7") == "1706.03762"
    assert normalize_arxiv_id("https://arxiv.org/pdf/1706.03762.pdf") == "1706.03762"
    assert normalize_arxiv_id("hep-th/9901001v2") == "hep-th/9901001"
    assert normalize_arxiv_id("nope") == ""


async def test_lookup_crossref_maps_fields(monkeypatch) -> None:
    async def fake_json(url, params=None):
        assert "10.1038/nature12373" in url
        return CROSSREF_SAMPLE

    monkeypatch.setattr(metadata, "_get_json", fake_json)
    fields = await lookup_crossref("https://doi.org/10.1038/nature12373")
    assert fields["title"] == "Nanometre-scale thermometry in a living cell"
    assert fields["year"] == 2013
    assert fields["venue"] == "Nature"
    assert (fields["volume"], fields["issue"], fields["pages"]) == ("500", "7460", "54–58")
    assert fields["doi"] == "10.1038/nature12373"
    assert fields["entry_type"] == "journal"
    assert fields["authors_json"] == [
        {"lastName": "Kucsko", "firstName": "G."},
        {"name": "The Collaboration"},
    ]


async def test_lookup_crossref_not_found_and_bad_identifier(monkeypatch) -> None:
    async def not_found(url, params=None):
        raise MetadataNotFound("Crossref 没有收录这个 DOI")

    monkeypatch.setattr(metadata, "_get_json", not_found)
    with pytest.raises(MetadataNotFound):
        await lookup_crossref("10.1038/nope")
    with pytest.raises(MetadataError):
        await lookup_crossref("not-a-doi")


async def test_lookup_arxiv_maps_fields(monkeypatch) -> None:
    async def fake_xml(url, params=None):
        assert params == {"id_list": "1706.03762"}
        return ElementTree.fromstring(ARXIV_SAMPLE)

    monkeypatch.setattr(metadata, "_get_xml", fake_xml)
    fields = await lookup_arxiv("arXiv:1706.03762v7")
    assert fields["title"] == "Attention Is All You Need"
    assert fields["year"] == 2017
    assert fields["venue"] == "NeurIPS 2017"
    assert fields["arxiv_id"] == "1706.03762"
    assert fields["entry_type"] == "preprint"
    assert fields["doi"] == "10.1000/xyz"
    assert [a.get("lastName") for a in fields["authors_json"]] == ["Vaswani", "Shazeer"]


async def test_lookup_arxiv_empty_entry(monkeypatch) -> None:
    empty = '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'

    async def fake_xml(url, params=None):
        return ElementTree.fromstring(empty)

    monkeypatch.setattr(metadata, "_get_xml", fake_xml)
    with pytest.raises(MetadataNotFound):
        await lookup_arxiv("1706.03762")


# ---------------------------------------------------------------------------
# diff / apply
# ---------------------------------------------------------------------------
def test_build_field_diff_only_lists_changes() -> None:
    paper = _paper(year=2013, doi="10.1038/nature12373")
    rows = build_field_diff(
        paper,
        {
            "title": "Nanometre-scale thermometry in a living cell",
            "year": 2013,
            "venue": "Nature",
            "doi": "https://doi.org/10.1038/nature12373",
        },
    )
    by_field = {row["field"]: row for row in rows}
    assert set(by_field) == {"title", "venue"}  # 相同的 year/doi 不重复列
    assert by_field["title"]["empty"] is False and by_field["title"]["conflict"] is True
    assert by_field["venue"]["empty"] is True and by_field["venue"]["conflict"] is False


def test_apply_fills_empty_fields_and_title_norm() -> None:
    paper = _paper(title="")
    changed = apply_metadata(
        paper,
        {
            "title": "A Real Title",
            "year": 2020,
            "venue": " Journal X ",
            "authors_json": [{"lastName": "Li", "firstName": "Ming"}],
        },
    )
    assert changed == ["authors_json", "title", "venue", "year"]
    assert paper.title == "A Real Title"
    assert paper.title_norm == "a real title"  # normalize_title 小写
    assert paper.venue == "Journal X"
    assert paper.authors_json == [{"lastName": "Li", "firstName": "Ming"}]


def test_apply_conflict_requires_overwrite() -> None:
    paper = _paper(title="Old Title")
    with pytest.raises(MetadataConflict):
        apply_metadata(paper, {"title": "New Title"})
    apply_metadata(paper, {"title": "New Title"}, overwrite=True)
    assert paper.title == "New Title"


def test_apply_rejects_bad_values_and_unknown_fields() -> None:
    paper = _paper()
    for fields in (
        {"title": ""},
        {"year": 1500},
        {"doi": "not-a-doi"},
        {"arxiv_id": "nope"},
        {"entry_type": "nonsense"},
        {"authors_json": []},
        {"authors_json": [{"firstName": ""}]},
        {"abstract": "not allowed"},
    ):
        with pytest.raises(MetadataError):
            apply_metadata(paper, fields)
    assert paper.title == "Untitled upload"


# ---------------------------------------------------------------------------
# HTTP 契约
# ---------------------------------------------------------------------------
async def test_lookup_endpoint_maps_diff(client, monkeypatch) -> None:
    paper = await _seed(client)

    async def fake_lookup(p, **kwargs):
        return {
            "paper_id": p.id,
            "mode": kwargs.get("mode", "auto"),
            "source": "crossref",
            "identifier": "10.1038/nature12373",
            "fields": build_field_diff(p, {"title": "Real Title", "year": 2020}),
            "candidates": [],
            "warnings": [],
        }

    monkeypatch.setattr("litbench.routers.metadata.lookup_for_paper", fake_lookup)
    response = await client.post(
        f"/api/papers/{paper.id}/metadata/lookup", json={"mode": "auto"}
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["source"] == "crossref"
    assert [row["field"] for row in data["fields"]] == ["title", "year"]
    assert data["fields"][0]["conflict"] is True


async def test_lookup_endpoint_error_mapping(client, monkeypatch) -> None:
    paper = await _seed(client)

    async def not_found(p, **kwargs):
        raise MetadataNotFound("Crossref 没有收录这个 DOI")

    monkeypatch.setattr("litbench.routers.metadata.lookup_for_paper", not_found)
    response = await client.post(f"/api/papers/{paper.id}/metadata/lookup", json={})
    assert response.status_code == 404

    async def unavailable(p, **kwargs):
        raise MetadataUnavailable("Crossref 请求超时")

    monkeypatch.setattr("litbench.routers.metadata.lookup_for_paper", unavailable)
    response = await client.post(f"/api/papers/{paper.id}/metadata/lookup", json={})
    assert response.status_code == 502

    async def bad_request(p, **kwargs):
        raise MetadataError("没有可用的 DOI / arXiv ID")

    monkeypatch.setattr("litbench.routers.metadata.lookup_for_paper", bad_request)
    response = await client.post(f"/api/papers/{paper.id}/metadata/lookup", json={})
    assert response.status_code == 422

    missing = await client.post("/api/papers/" + "0" * 32 + "/metadata/lookup", json={})
    assert missing.status_code == 404


async def test_lookup_disabled_503(client, monkeypatch) -> None:
    paper = await _seed(client)
    monkeypatch.setattr(metadata.settings, "metadata_lookup_enabled", False)
    response = await client.post(f"/api/papers/{paper.id}/metadata/lookup", json={})
    assert response.status_code == 503
    assert "关闭" in response.json()["detail"]


async def test_apply_endpoint_writes_and_returns_paper(client) -> None:
    paper = await _seed(client)
    response = await client.post(
        f"/api/papers/{paper.id}/metadata/apply",
        json={
            "fields": {
                "year": 2020,
                "venue": "Journal of Testing",
                "pages": "1–9",
                "authors_json": [{"lastName": "Li", "firstName": "Ming"}],
            }
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["year"] == 2020
    assert data["venue"] == "Journal of Testing"
    assert data["pages"] == "1–9"
    assert data["authors_json"] == [{"lastName": "Li", "firstName": "Ming"}]


async def test_apply_endpoint_conflict_then_overwrite(client) -> None:
    paper = await _seed(client)
    conflict = await client.post(
        f"/api/papers/{paper.id}/metadata/apply",
        json={"fields": {"title": "New Title"}},
    )
    assert conflict.status_code == 409
    assert "覆盖" in conflict.json()["detail"]

    applied = await client.post(
        f"/api/papers/{paper.id}/metadata/apply",
        json={"fields": {"title": "New Title"}, "overwrite": True},
    )
    assert applied.status_code == 200
    assert applied.json()["title"] == "New Title"


async def test_apply_endpoint_rejects_unknown_field(client) -> None:
    paper = await _seed(client)
    response = await client.post(
        f"/api/papers/{paper.id}/metadata/apply",
        json={"fields": {"abstract": "not allowed"}},
    )
    assert response.status_code == 422
    assert "abstract" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 外部 HTTP 错误映射（MockTransport；不联网）
# ---------------------------------------------------------------------------
async def test_crossref_http_error_mapping(monkeypatch) -> None:
    import httpx

    real_client = httpx.AsyncClient

    def install(handler) -> None:
        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(metadata.httpx, "AsyncClient", factory)

    install(lambda request: httpx.Response(404, text="not found"))
    with pytest.raises(MetadataNotFound):
        await lookup_crossref("10.1038/nature12373")

    install(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(MetadataUnavailable):
        await lookup_crossref("10.1038/nature12373")

    install(lambda request: httpx.Response(200, text="<html>not json</html>"))
    with pytest.raises(MetadataUnavailable):
        await lookup_crossref("10.1038/nature12373")

    def timeout(request):
        raise httpx.ReadTimeout("timed out", request=request)

    install(timeout)
    with pytest.raises(MetadataUnavailable):
        await lookup_crossref("10.1038/nature12373")


async def test_arxiv_http_and_xml_error_mapping(monkeypatch) -> None:
    import httpx

    real_client = httpx.AsyncClient

    def install(handler) -> None:
        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(metadata.httpx, "AsyncClient", factory)

    install(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(MetadataUnavailable):
        await lookup_arxiv("1706.03762")

    install(lambda request: httpx.Response(200, text="<feed>broken"))
    with pytest.raises(MetadataUnavailable):
        await lookup_arxiv("1706.03762")


async def test_oversized_external_response_rejected(monkeypatch) -> None:
    import httpx

    real_client = httpx.AsyncClient
    huge = "x" * (metadata.MAX_METADATA_BYTES + 1)

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(200, text=huge)
        )
        return real_client(*args, **kwargs)

    monkeypatch.setattr(metadata.httpx, "AsyncClient", factory)
    with pytest.raises(MetadataUnavailable):
        await lookup_crossref("10.1038/nature12373")


# ---------------------------------------------------------------------------
# auto/search 编排
# ---------------------------------------------------------------------------
def _fake_paper(**overrides):
    from types import SimpleNamespace

    base = dict(id="p" * 32, title="Some title", doi=None, arxiv_id=None)
    base.update(overrides)
    return SimpleNamespace(**base)


async def test_lookup_for_paper_auto_prefers_doi(monkeypatch) -> None:
    async def fake_crossref(doi):
        return {"title": "X", "year": 2020}

    monkeypatch.setattr(metadata, "lookup_crossref", fake_crossref)
    payload = await metadata.lookup_for_paper(_fake_paper(doi="10.1038/nature12373"))
    assert payload["source"] == "crossref"
    assert payload["identifier"] == "10.1038/nature12373"
    assert payload["fields"][0]["field"] == "title"


async def test_lookup_for_paper_auto_uses_arxiv_and_errors(monkeypatch) -> None:
    async def fake_arxiv(arxiv_id):
        return {"title": "Y", "arxiv_id": arxiv_id}

    monkeypatch.setattr(metadata, "lookup_arxiv", fake_arxiv)
    payload = await metadata.lookup_for_paper(_fake_paper(arxiv_id="arXiv:1706.03762v7"))
    assert payload["source"] == "arxiv"
    assert payload["identifier"] == "1706.03762"

    with pytest.raises(MetadataError):
        await metadata.lookup_for_paper(_fake_paper())
    with pytest.raises(MetadataError):
        await metadata.lookup_for_paper(_fake_paper(), mode="doi")
