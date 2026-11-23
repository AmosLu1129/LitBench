# SPDX-License-Identifier: AGPL-3.0-or-later
"""M5：外部 agent 投递提案 + M3 收尾（任务人工重试）。

外部提案比内置 AI 多两条硬约束：

1. 必须带真实、同版本的 ``evidence_block_ids``；
2. ``needs_change=false`` 的空提案直接拒绝（agent 应该把任务标 done）。

任务重试只允许 failed / canceled → queued，attempts 归零。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.db import get_session
from litbench.main import app
from litbench.models import Base, Block, Figure, Paper, PaperVersion


@pytest.fixture
async def client(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'm5.db').as_posix()}", poolclass=NullPool
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


async def _seed_figure(client, sha: str = "c" * 64) -> dict:
    Session = client.session_factory
    async with Session() as s:
        paper = Paper(title="测试论文：凝结", title_norm="测试论文凝结", status="parsed")
        s.add(paper)
        await s.flush()
        version = PaperVersion(
            paper_id=paper.id,
            kind="uploaded",
            file_sha256=sha,
            file_path="pdfs/c.pdf",
            page_count=2,
            parser="pdfium",
            parsed_at=datetime.now(timezone.utc),
        )
        s.add(version)
        await s.flush()
        block = Block(
            paper_version_id=version.id,
            page=2,
            order_idx=0,
            type="text",
            text="图 7-1 展示了不同润湿能力壁面上液膜形成的接触角。",
            bbox_json=[0, 0, 10, 10],
        )
        figure = Figure(
            paper_version_id=version.id,
            page=2,
            label_raw="图 7-1",
            label_norm="图7-1",
            caption="图 7-1 不同润湿能力壁面上液膜形成的contact angle 测量",
            bbox_json=[0, 0, 100, 100],
            cite_key=f"P-{sha[:6]}-F1",
        )
        s.add_all([block, figure])
        await s.commit()
        return {
            "paper_id": paper.id,
            "version_id": version.id,
            "block_id": block.id,
            "figure_id": figure.id,
        }


def _proposal_body(seed: dict, **overrides) -> dict:
    body = {
        "kind": "figure_review",
        "figure_id": seed["figure_id"],
        "proposed": {
            "label_ok": True,
            "caption_issue": "ocr_noise",
            "cleaned_caption": "图 7-1 不同润湿能力壁面上液膜形成的接触角测量",
            "crop_issue": "none",
            "confidence": 0.96,
            "reason": "把英文 contact angle 改成中文，属于 OCR 混排",
        },
        "evidence_block_ids": [seed["block_id"]],
        "confidence": 0.96,
        "actor": "dsh-agent",
        "note": "MCP proposal.create 测试",
        **overrides,
    }
    return body


async def test_external_proposal_creates_pending(client) -> None:
    seed = await _seed_figure(client)
    response = await client.post("/api/proposals", json=_proposal_body(seed))
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["status"] == "pending"
    assert data["provider"] == "external"
    assert data["model"] == "dsh-agent"
    assert data["confidence"] == 0.96
    assert data["proposed"]["needs_change"] is True
    assert data["evidence"][0]["block_id"] == seed["block_id"]

    audit = await client.get(
        f"/api/audit?action=proposal.create&target_id={data['proposal_id']}"
    )
    assert audit.status_code == 200
    assert audit.json()[0]["actor"] == "dsh-agent"
    assert audit.json()[0]["after"]["figure_id"] == seed["figure_id"]


async def test_external_proposal_requires_evidence(client) -> None:
    seed = await _seed_figure(client)
    body = _proposal_body(seed)
    body["evidence_block_ids"] = []
    response = await client.post("/api/proposals", json=body)
    assert response.status_code == 422
    assert "证据" in response.text

    body = _proposal_body(seed)
    body["evidence_block_ids"] = ["0" * 32]
    response = await client.post("/api/proposals", json=body)
    assert response.status_code == 422


async def test_external_proposal_rejects_no_change(client) -> None:
    seed = await _seed_figure(client)
    body = _proposal_body(seed)
    body["proposed"] = {
        "label_ok": True,
        "caption_issue": "none",
        "crop_issue": "none",
        "confidence": 0.9,
    }
    response = await client.post("/api/proposals", json=body)
    assert response.status_code == 422
    assert "needs_change" in response.text


async def test_external_proposal_supersedes_old_pending(client) -> None:
    seed = await _seed_figure(client)
    first = await client.post("/api/proposals", json=_proposal_body(seed))
    assert first.status_code == 201
    second = await client.post("/api/proposals", json=_proposal_body(seed))
    assert second.status_code == 201
    rows = await client.get("/api/proposals?status=all")
    statuses = {row["proposal_id"]: row["status"] for row in rows.json()}
    assert statuses[first.json()["proposal_id"]] == "superseded"
    assert statuses[second.json()["proposal_id"]] == "pending"


async def test_task_manual_retry(client) -> None:
    created = await client.post(
        "/api/tasks",
        json={
            "kind": "auto_accept_sweep",
            "target_type": "global",
            "target_id": "",
            "payload": {},
        },
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]

    claimed = await client.post(f"/api/tasks/{task_id}/claim", json={"actor": "dsh"})
    assert claimed.status_code == 200
    failed = await client.post(
        f"/api/tasks/{task_id}/fail",
        json={"actor": "dsh", "error": "模型超时", "retry": False},
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert failed.json()["attempts"] == 1

    retried = await client.post(f"/api/tasks/{task_id}/retry")
    assert retried.status_code == 200
    data = retried.json()
    assert data["status"] == "queued"
    assert data["attempts"] == 0
    assert data["error"] is None
    assert data["actor"] is None

    again = await client.post(f"/api/tasks/{task_id}/retry")
    assert again.status_code == 409, "queued 任务不能重复重试"
