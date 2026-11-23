# SPDX-License-Identifier: AGPL-3.0-or-later
"""M3c 框复核测试：坐标归一化 / 页内校验 / caption 锚点 / 提案 / 自动接受门 / 回滚 / 任务。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import litbench.bbox_review as bbox_review_mod
import litbench.routers.agent as agent_router_mod
import litbench.workers.tasks as worker_tasks_mod
from litbench.bbox_review import BboxReviewError, caption_anchor, normalize_bbox
from litbench.config import settings
from litbench.db import get_session
from litbench.llm import LLMResult
from litbench.main import app
from litbench.models import (
    AgentTask,
    Base,
    Block,
    Figure,
    Paper,
    PaperVersion,
)
from pdf_fixture import make_pdf

PAGE_W, PAGE_H = 612.0, 792.0
# make_pdf 里画的矢量框：100,520 -> 400,650；caption 在其下方 y≈512
CURRENT_BBOX = [100.0, 520.0, 400.0, 650.0]
CAPTION_BBOX = [100.0, 505.0, 400.0, 525.0]


class FakeClient:
    provider = "fake"
    model = "fake-bbox-model"

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    async def complete(self, messages, *, json_schema=None, meta=None, max_tokens=None, timeout_s=None):
        self.calls += 1
        # 断言多图消息：至少 2 张 image block
        content = messages[0]["content"]
        assert sum(1 for block in content if block.get("type") == "image") >= 2
        return LLMResult(
            text=json.dumps(self.payload, ensure_ascii=False),
            parsed=self.payload,
            provider=self.provider,
            model=self.model,
            tokens_in=800,
            tokens_out=80,
            cost=0.002,
        )


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(settings, "llm_config_file", None)
    monkeypatch.setattr(settings, "llm_base_url", None)
    monkeypatch.setattr(settings, "llm_api_key", None)
    monkeypatch.setattr(settings, "llm_model", None)
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "bbox_review_auto_accept", False)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'bbox.db').as_posix()}", poolclass=NullPool
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
        ac.tmp_path = tmp_path  # type: ignore[attr-defined]
        yield ac
    app.dependency_overrides.clear()
    await engine.dispose()


async def _seed(client) -> dict:
    pdf_dir = client.tmp_path / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    (pdf_dir / "test.pdf").write_bytes(make_pdf())
    Session = client.session_factory
    async with Session() as s:
        paper = Paper(title="M3c 框复核测试文献", title_norm="m3c框复核测试文献", status="parsed")
        s.add(paper)
        await s.flush()
        version = PaperVersion(
            paper_id=paper.id,
            kind="uploaded",
            file_sha256="d" * 64,
            file_path="pdfs/test.pdf",
            page_count=1,
            parser="pdfium",
            parsed_at=datetime.now(timezone.utc),
        )
        s.add(version)
        await s.flush()
        fig = Figure(
            paper_version_id=version.id,
            page=1,
            bbox_json=list(CURRENT_BBOX),
            label_raw="Fig. 1",
            label_norm="fig:1",
            label_num=1,
            caption="Fig. 1. Temperature dependence of the reaction rate.",
            cite_key="m3c-v1-fig1",
            label_source="text",
            label_confidence=0.9,
        )
        s.add(fig)
        s.add(
            Block(
                paper_version_id=version.id,
                page=1,
                order_idx=0,
                bbox_json=list(CAPTION_BBOX),
                type="caption",
                text="Fig. 1. Temperature dependence of the reaction rate.",
            )
        )
        await s.commit()
        return {"paper_id": paper.id, "version_id": version.id, "figure_id": fig.id}


def _mock_llm(monkeypatch, payload: dict) -> FakeClient:
    fake = FakeClient(payload)
    monkeypatch.setattr(bbox_review_mod, "get_client", lambda provider=None: fake)
    monkeypatch.setattr(bbox_review_mod, "effective_config", lambda: ("ui", None))
    monkeypatch.setattr(agent_router_mod, "effective_config", lambda: ("ui", None))
    return fake


# ---------------------------------------------------------------------------
# 纯函数：坐标与 IoU
# ---------------------------------------------------------------------------
def test_normalize_bbox_accepts_normalized_and_points() -> None:
    assert normalize_bbox([0.1, 0.2, 0.5, 0.6], PAGE_W, PAGE_H) == [61.2, 158.4, 306.0, 475.2]
    assert normalize_bbox([61.2, 158.4, 306.0, 475.2], PAGE_W, PAGE_H) == [
        61.2,
        158.4,
        306.0,
        475.2,
    ]


def test_normalize_bbox_rejects_tiny_or_invalid() -> None:
    with pytest.raises(BboxReviewError):
        normalize_bbox([0.1, 0.1, 0.11, 0.11], PAGE_W, PAGE_H)
    with pytest.raises(BboxReviewError):
        normalize_bbox([0.1, 0.1], PAGE_W, PAGE_H)
    with pytest.raises(BboxReviewError):
        normalize_bbox(["a", 0.1, 0.5, 0.6], PAGE_W, PAGE_H)


async def test_caption_anchor_finds_nearby_caption(client) -> None:
    seed = await _seed(client)
    Session = client.session_factory
    async with Session() as s:
        fig = await s.get(Figure, seed["figure_id"])
        anchor = await caption_anchor(s, fig, CURRENT_BBOX)
    assert anchor["ok"] is True
    assert anchor["block_id"]
    assert anchor["gap"] == 0


# ---------------------------------------------------------------------------
# 提案：默认只提议，不自动写
# ---------------------------------------------------------------------------
async def test_bbox_review_creates_pending_proposal(client, monkeypatch) -> None:
    seed = await _seed(client)
    _mock_llm(
        monkeypatch,
        {
            "verdict": "wrong",
            "bbox": [0.1, 0.55, 0.7, 0.6],
            "confidence": 0.9,
            "reason": "下边界少框了一段",
        },
    )
    r = await client.post(f"/api/figures/{seed['figure_id']}/bbox-review")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "figure_bbox"
    assert body["status"] == "pending"
    assert body["confidence"] == pytest.approx(0.9)
    proposed = body["proposed"]
    assert proposed["verdict"] == "wrong"
    assert proposed["needs_change"] is True
    assert len(proposed["bbox_points"]) == 4
    assert 0 <= proposed["iou"] < 0.2
    assert proposed["caption_anchor"]["ok"] is True
    # 默认不自动写图
    fig = (await client.get(f"/api/figures/{seed['figure_id']}")).json()
    assert not fig.get("bbox_override")


async def test_bbox_review_ok_produces_no_change(client, monkeypatch) -> None:
    seed = await _seed(client)
    _mock_llm(
        monkeypatch,
        {"verdict": "ok", "bbox": [], "confidence": 0.95, "reason": "框完整"},
    )
    r = await client.post(f"/api/figures/{seed['figure_id']}/bbox-review")
    assert r.status_code == 200
    assert r.json()["status"] == "no_change"


async def test_bbox_review_invalid_output_is_422(client, monkeypatch) -> None:
    seed = await _seed(client)
    _mock_llm(monkeypatch, {"verdict": "maybe", "confidence": 0.9})
    r = await client.post(f"/api/figures/{seed['figure_id']}/bbox-review")
    assert r.status_code == 422
    assert "verdict" in r.json()["detail"]


async def test_bbox_auto_accept_requires_switch(client, monkeypatch) -> None:
    seed = await _seed(client)
    _mock_llm(
        monkeypatch,
        {
            "verdict": "wrong",
            "bbox": [0.1, 0.55, 0.7, 0.6],
            "confidence": 0.98,
            "reason": "框偏了",
        },
    )
    # 开关打开前：pending
    first = await client.post(f"/api/figures/{seed['figure_id']}/bbox-review")
    assert first.json()["status"] == "pending"
    # 打开开关 + 高置信 + IoU 极低 + caption 锚点 → 自动接受
    monkeypatch.setattr(settings, "bbox_review_auto_accept", True)
    second = await client.post(f"/api/figures/{seed['figure_id']}/bbox-review")
    assert second.json()["status"] == "accepted"
    assert second.json()["auto_accepted"] is True
    fig = (await client.get(f"/api/figures/{seed['figure_id']}")).json()
    assert fig["bbox_override"] == pytest.approx([61.2, 435.6, 428.4, 475.2], abs=0.2)


async def test_accept_bbox_writes_override_and_rollback_restores(client, monkeypatch) -> None:
    seed = await _seed(client)
    _mock_llm(
        monkeypatch,
        {
            "verdict": "wrong",
            "bbox": [0.1, 0.55, 0.7, 0.6],
            "confidence": 0.9,
            "reason": "框偏了",
        },
    )
    proposal = (await client.post(f"/api/figures/{seed['figure_id']}/bbox-review")).json()
    proposal_id = proposal["proposal_id"]
    decided = await client.post(
        f"/api/proposals/{proposal_id}/decide",
        json={"action": "accept", "actor": "tester"},
    )
    assert decided.status_code == 200, decided.text
    assert "bbox_override" in decided.json()["applied"]
    fig = (await client.get(f"/api/figures/{seed['figure_id']}")).json()
    assert fig["bbox_override"]

    rolled = await client.post(
        f"/api/proposals/{proposal_id}/rollback", json={"actor": "tester"}
    )
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["proposal"]["status"] == "rolled_back"
    fig = (await client.get(f"/api/figures/{seed['figure_id']}")).json()
    assert fig.get("bbox_override") is None


async def test_bbox_review_task_worker(client, monkeypatch) -> None:
    seed = await _seed(client)
    Session = client.session_factory

    async def fake_review(session, figure_id):
        return {"proposal_id": "bbox-1", "kind": "figure_bbox", "status": "pending", "confidence": 0.9}

    monkeypatch.setattr(worker_tasks_mod, "ai_review_bbox", fake_review)
    monkeypatch.setattr(worker_tasks_mod, "get_sessionmaker", lambda: Session)
    async with Session() as s:
        task = AgentTask(
            kind="figure_bbox_review",
            target_type="figure",
            target_id=seed["figure_id"],
            payload_json={"figure_id": seed["figure_id"]},
        )
        s.add(task)
        await s.commit()
        task_id = task.id
    assert await worker_tasks_mod.run_task_once() == "done"
    task = (await client.get(f"/api/tasks/{task_id}")).json()
    assert task["status"] == "done"
    assert task["result"]["kind"] == "figure_bbox"


async def test_enqueue_bbox_review(client, monkeypatch) -> None:
    seed = await _seed(client)
    monkeypatch.setattr(agent_router_mod, "effective_config", lambda: ("ui", None))
    r = await client.post(
        "/api/tasks/enqueue-bbox-review",
        json={"paper_id": seed["paper_id"], "limit": 10},
    )
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1
    second = await client.post(
        "/api/tasks/enqueue-bbox-review",
        json={"paper_id": seed["paper_id"], "limit": 10},
    )
    assert second.json()["created"] == 0
    tasks = (await client.get("/api/tasks", params={"kind": "figure_bbox_review"})).json()
    assert len(tasks) == 1
    assert tasks[0]["target_id"] == seed["figure_id"]
