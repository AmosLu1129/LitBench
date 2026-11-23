# SPDX-License-Identifier: AGPL-3.0-or-later
"""M9.1b-lite：复用 chat 模型做查询改写（不是 embedding 向量召回）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench import query_expand
from litbench.db import get_session
from litbench.llm import LLMError, LLMResult
from litbench.main import app
from litbench.models import Base, Figure, Paper, PaperVersion
from litbench.query_expand import ExpandedQuery, expand_query, should_expand


class FakeClient:
    provider = "fake"
    model = "fake-query-expand"

    def __init__(self, payload: dict | str | None = None, *, fail: str | None = None) -> None:
        self.payload = payload
        self.fail = fail
        self.calls = 0

    async def complete(self, messages, *, json_schema=None, meta=None, max_tokens=None, timeout_s=None):
        self.calls += 1
        if self.fail:
            raise LLMError(self.fail)
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        parsed = None if isinstance(self.payload, str) else self.payload
        return LLMResult(
            text=text or "{}",
            parsed=parsed,
            provider=self.provider,
            model=self.model,
            tokens_in=10,
            tokens_out=5,
            latency_ms=1,
        )


def _fake_llm(monkeypatch, client: FakeClient) -> None:
    monkeypatch.setattr(query_expand, "effective_config", lambda: ("ui", object()))
    monkeypatch.setattr(query_expand, "get_client", lambda *a, **k: client)
    query_expand.reset_cache()


def test_should_expand_only_for_chinese() -> None:
    query_expand.reset_cache()
    assert should_expand("多孔介质强化传热") is True
    assert should_expand("porous media heat transfer") is False
    assert should_expand("Fig. 3") is False
    assert should_expand("") is False


async def test_expand_query_builds_keywords_and_caches(monkeypatch) -> None:
    client = FakeClient(
        {
            "english_query": "porous media enhanced heat transfer",
            "keywords": ["porous media", "heat transfer enhancement", "porous media"],
        }
    )
    _fake_llm(monkeypatch, client)
    expanded = await expand_query("多孔介质强化传热")
    assert expanded is not None
    assert expanded.provider == "fake"
    assert expanded.model == "fake-query-expand"
    assert expanded.keywords == ["porous media", "heat transfer enhancement"]
    assert "porous media enhanced heat transfer" in expanded.text
    assert "多孔介质强化传热" in expanded.text
    # 同一查询命中缓存，不再调模型
    second = await expand_query("多孔介质强化传热")
    assert second is not None and second.text == expanded.text
    assert client.calls == 1


async def test_expand_query_falls_back_on_mock_or_error(monkeypatch) -> None:
    query_expand.reset_cache()
    monkeypatch.setattr(query_expand, "effective_config", lambda: ("mock", None))
    assert await expand_query("中文查询") is None

    client = FakeClient(fail="upstream timeout")
    _fake_llm(monkeypatch, client)
    assert await expand_query("另一个中文查询") is None


async def test_expand_query_rejects_empty_model_output(monkeypatch) -> None:
    _fake_llm(monkeypatch, FakeClient({"english_query": "", "keywords": []}))
    assert await expand_query("空输出查询") is None


# ---------------------------------------------------------------------------
# 图检索集成：中文查询在开改写后命中英文图注
# ---------------------------------------------------------------------------
@pytest.fixture
async def client(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'qe.db').as_posix()}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        paper = Paper(
            title="Porous media heat transfer review",
            title_norm="porous media heat transfer review",
            status="parsed",
        )
        s.add(paper)
        await s.flush()
        version = PaperVersion(
            paper_id=paper.id,
            kind="uploaded",
            file_sha256="c" * 64,
            file_path="pdfs/c.pdf",
            page_count=3,
            parser="docling",
            parsed_at=datetime.now(UTC),
        )
        s.add(version)
        await s.flush()
        s.add(
            Figure(
                paper_version_id=version.id,
                page=2,
                bbox_json=[10.0, 20.0, 100.0, 80.0],
                label_raw="Fig. 1",
                label_norm="fig:1",
                label_num=1,
                caption="Enhanced heat transfer in a porous medium with cubic lattice.",
                cite_key="qe-v1-fig1",
            )
        )
        await s.commit()

        async def _session():
            async with Session() as session:
                yield session

        app.dependency_overrides[get_session] = _session
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac
        app.dependency_overrides.clear()
        await engine.dispose()


async def test_figure_search_chinese_query_without_expansion_is_empty(client) -> None:
    response = await client.get("/api/figures", params={"q": "多孔介质强化传热"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 0
    assert body["query_expanded"] is False


async def test_figure_search_uses_expanded_query(client, monkeypatch) -> None:
    async def fake_expand(raw: str):
        assert "多孔介质" in raw
        return ExpandedQuery(
            original=raw,
            text="porous medium enhanced heat transfer cubic lattice",
            keywords=["porous medium", "heat transfer enhancement"],
            english_query="porous medium enhanced heat transfer",
            provider="fake",
            model="fake-query-expand",
        )

    monkeypatch.setattr("litbench.routers.figures.expand_query", fake_expand)
    response = await client.get("/api/figures", params={"q": "多孔介质强化传热"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["query_expanded"] is True
    assert body["engine"] == "bm25"
    assert body["expanded_keywords"] == ["porous medium", "heat transfer enhancement"]
    assert body["expansion_provider"] == "fake"
    assert body["total"] == 1
    assert "改写" in body["hint"]


async def test_paper_search_uses_expanded_query(client, monkeypatch) -> None:
    empty = await client.get("/api/papers", params={"q": "多孔介质强化传热"})
    assert empty.status_code == 200, empty.text
    assert empty.json() == []

    async def fake_expand(raw: str):
        return ExpandedQuery(
            original=raw,
            text="porous media heat transfer review",
            keywords=["porous media"],
            english_query="porous media heat transfer review",
            provider="fake",
            model="fake-query-expand",
        )

    monkeypatch.setattr("litbench.routers.papers.expand_query", fake_expand)
    response = await client.get("/api/papers", params={"q": "多孔介质强化传热"})
    assert response.status_code == 200, response.text
    assert len(response.json()) == 1
