# SPDX-License-Identifier: AGPL-3.0-or-later
"""M9.4：引用核验的模型复核档（``POST /api/citations/review``）。

覆盖：模型 supported/insufficient 两档、JSON 解析兜底、未配置模型回退 503、
证据块不属于该文献 422、跨文献请求不越权。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench import citation_review
from litbench.citation_review import CitationReviewError
from litbench.db import get_session
from litbench.llm import LLMResult
from litbench.main import app
from litbench.models import Base, Block, Paper, PaperVersion


class FakeClient:
    provider = "fake"
    model = "fake-cite-review-v1"

    def __init__(self, payload: dict | str | None = None, *, fail: str | None = None) -> None:
        self.payload = payload
        self.fail = fail

    async def complete(self, messages, *, json_schema=None, meta=None, max_tokens=None, timeout_s=None):
        if self.fail:
            raise citation_review.LLMError(self.fail)
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        parsed = None if isinstance(self.payload, str) else self.payload
        return LLMResult(
            text=text or "{}",
            parsed=parsed,
            provider=self.provider,
            model=self.model,
            tokens_in=12,
            tokens_out=8,
            latency_ms=3,
        )


@pytest.fixture
async def db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'cite.db').as_posix()}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        paper = Paper(title="Attention", title_norm="attention", status="parsed")
        other = Paper(title="Other", title_norm="other", status="parsed")
        s.add_all([paper, other])
        await s.flush()
        version = PaperVersion(
            paper_id=paper.id,
            kind="uploaded",
            file_sha256="a" * 64,
            file_path="pdfs/a.pdf",
            page_count=3,
            parser="docling",
            parsed_at=datetime.now(UTC),
        )
        other_version = PaperVersion(
            paper_id=other.id,
            kind="uploaded",
            file_sha256="b" * 64,
            file_path="pdfs/b.pdf",
            page_count=1,
            parser="docling",
            parsed_at=datetime.now(UTC),
        )
        s.add_all([version, other_version])
        await s.flush()
        relevant = Block(
            paper_version_id=version.id,
            page=1,
            type="text",
            text="Scaled dot product attention normalizes compatibility scores with softmax.",
        )
        unrelated = Block(
            paper_version_id=version.id,
            page=2,
            type="text",
            text="Banana bread requires ripe bananas, flour and a very hot oven.",
        )
        foreign = Block(
            paper_version_id=other_version.id,
            page=1,
            type="text",
            text="This block belongs to another paper.",
        )
        s.add_all([relevant, unrelated, foreign])
        await s.commit()
        fixture = {
            "paper_id": paper.id,
            "other_id": other.id,
            "block_id": relevant.id,
            "unrelated_id": unrelated.id,
            "foreign_id": foreign.id,
        }
    async def _session():
        async with Session() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        ac.session_factory = Session  # type: ignore[attr-defined]
        yield ac, fixture
    app.dependency_overrides.clear()
    await engine.dispose()


async def _use_fake(monkeypatch, client: FakeClient) -> None:
    monkeypatch.setattr(citation_review, "effective_config", lambda: ("ui", object()))
    monkeypatch.setattr(citation_review, "get_client", lambda *a, **k: client)


def _payload(fixture: dict, **overrides) -> dict:
    data = {
        "claim_text": "Attention normalizes compatibility scores with softmax.",
        "paper_id": fixture["paper_id"],
        "block_ids": [fixture["block_id"], fixture["unrelated_id"]],
    }
    data.update(overrides)
    return data


async def test_model_supported(db, monkeypatch) -> None:
    client, fixture = db
    await _use_fake(
        monkeypatch,
        FakeClient(
            {"verdict": "supported", "confidence": 0.92, "reason": "证据直接给出 softmax 归一化"}
        ),
    )
    r = await client.post("/api/citations/review", json=_payload(fixture))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "supported"
    assert body["confidence"] == 0.92
    assert body["provider"] == "fake"
    assert body["model"] == "fake-cite-review-v1"
    assert body["block_ids"] == [fixture["block_id"], fixture["unrelated_id"]]
    assert body["prompt_version"].startswith("cite-review/")


async def test_model_insufficient(db, monkeypatch) -> None:
    client, fixture = db
    await _use_fake(
        monkeypatch,
        FakeClient({"verdict": "insufficient", "confidence": 0.3, "reason": "证据只讲面包"}),
    )
    r = await client.post(
        "/api/citations/review",
        json=_payload(fixture, claim_text="Bananas are blue."),
    )
    assert r.status_code == 200
    assert r.json()["verdict"] == "insufficient"


async def test_bad_json_falls_back_to_insufficient(db, monkeypatch) -> None:
    client, fixture = db
    await _use_fake(monkeypatch, FakeClient("not json at all"))
    r = await client.post("/api/citations/review", json=_payload(fixture))
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "insufficient"
    assert body["confidence"] == 0.5


async def test_unconfigured_model_returns_503(db, monkeypatch) -> None:
    client, fixture = db
    monkeypatch.setattr(citation_review, "effective_config", lambda: ("mock", None))
    r = await client.post("/api/citations/review", json=_payload(fixture))
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["code"] == "llm_not_configured"


async def test_llm_failure_returns_502(db, monkeypatch) -> None:
    client, fixture = db
    await _use_fake(monkeypatch, FakeClient(fail="upstream timeout"))
    r = await client.post("/api/citations/review", json=_payload(fixture))
    assert r.status_code == 502
    assert r.json()["detail"]["code"] == "llm_error"


async def test_foreign_block_is_rejected(db, monkeypatch) -> None:
    client, fixture = db
    await _use_fake(monkeypatch, FakeClient({"verdict": "supported", "confidence": 1}))
    r = await client.post(
        "/api/citations/review",
        json=_payload(fixture, block_ids=[fixture["foreign_id"]]),
    )
    assert r.status_code == 422
    assert "证据块" in r.text


async def test_empty_claim_is_schema_error(db) -> None:
    client, fixture = db
    r = await client.post("/api/citations/review", json=_payload(fixture, claim_text=""))
    assert r.status_code == 422


def test_review_citation_rejects_invalid_params() -> None:
    with pytest.raises(CitationReviewError) as exc:
        asyncio_run(citation_review.review_citation("x", []))
    assert exc.value.code == "invalid_params"


def asyncio_run(coro):  # noqa: ANN001 — 小工具避免 import 顺序噪音
    import asyncio

    return asyncio.run(coro)
