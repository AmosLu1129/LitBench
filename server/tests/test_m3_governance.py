# SPDX-License-Identifier: AGPL-3.0-or-later
"""M3 正式化测试：审计 / 自动接受器 / 回滚 / 批量决策 / agent 任务租约。

守的约束（docs/15 §4 M3 / §5 D3）：
1. 只有「白名单字段 + 证据非空 + 置信 ≥ 0.95 + 目标未人工改过」才自动接受；
2. 人工改过的图号/图注永不被自动覆盖；
3. 每次正式写都有 audit_log（before/after），采纳可一键回滚；
4. 任务租约：claim 原子、heartbeat 续租、失败重试、过期回收、幂等任务不重复；
5. worker 能执行 figure_review 任务并写回结果。
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import litbench.figure_review as figure_review_mod
import litbench.routers.agent as agent_router_mod
import litbench.workers.tasks as worker_tasks_mod
from litbench.auto_accept import evaluate_auto_accept
from litbench.config import settings
from litbench.db import get_session
from litbench.llm import LLMResult
from litbench.main import app
from litbench.models import (
    AgentTask,
    Base,
    Block,
    Figure,
    FigureProposal,
    Paper,
    PaperVersion,
)

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FakeClient:
    provider = "fake"
    model = "fake-vision-model"

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    async def complete(self, messages, *, json_schema=None, meta=None, max_tokens=None, timeout_s=None):
        self.calls += 1
        return LLMResult(
            text=json.dumps(self.payload, ensure_ascii=False),
            parsed=self.payload,
            provider=self.provider,
            model=self.model,
            tokens_in=100,
            tokens_out=50,
            cost=0.001,
        )


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(settings, "llm_config_file", None)
    monkeypatch.setattr(settings, "llm_base_url", None)
    monkeypatch.setattr(settings, "llm_api_key", None)
    monkeypatch.setattr(settings, "llm_model", None)
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "auto_accept_enabled", True)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'm3.db').as_posix()}", poolclass=NullPool
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
    Session = client.session_factory
    async with Session() as s:
        paper = Paper(
            title="M3 测试文献：图号复核",
            title_norm="m3测试文献图号复核",
            status="parsed",
            year=2026,
        )
        s.add(paper)
        await s.flush()
        version = PaperVersion(
            paper_id=paper.id,
            kind="uploaded",
            file_sha256="c" * 64,
            file_path="pdfs/test.pdf",
            page_count=2,
            parser="pdfium",
            parsed_at=datetime.now(timezone.utc),
        )
        s.add(version)
        await s.flush()
        for order, text in enumerate(
            ["图 1-1 是流程图。", "图 1-2 是校核步骤。", "图 1-3 是结果曲线。"]
        ):
            s.add(
                Block(
                    paper_version_id=version.id,
                    page=1,
                    order_idx=order,
                    bbox_json=[10.0, 10.0 + order * 20, 200.0, 300.0],
                    type="text",
                    text=text,
                )
            )
        image_dir = client.tmp_path / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        (image_dir / "fig-m3.png").write_bytes(PNG_1PX)
        figs = []
        for idx, label in enumerate(["图 1-1", "图 1-2", "图 1-3"]):
            fig = Figure(
                paper_version_id=version.id,
                page=1,
                bbox_json=[10.0 + idx * 5, 100.0 + idx * 30, 200.0, 220.0 + idx * 30],
                label_raw=label,
                label_norm=f"fig:1-{idx + 1}",
                label_num=idx + 1,
                caption=f"{label} 的图注内容（长度足够通过校验）",
                cite_key=f"m3-v1-f{idx + 1}",
                label_source="text",
                label_confidence=0.5 if idx == 1 else 0.9,
                image_path="images/fig-m3.png",
                review_status="pending",
            )
            s.add(fig)
            figs.append(fig)
        await s.commit()
        return {
            "paper_id": paper.id,
            "version_id": version.id,
            "figure_ids": [f.id for f in figs],
        }


def _mock_llm(monkeypatch, payload: dict) -> FakeClient:
    fake = FakeClient(payload)
    monkeypatch.setattr(figure_review_mod, "get_client", lambda provider=None: fake)
    monkeypatch.setattr(figure_review_mod, "effective_config", lambda: ("ui", None))
    monkeypatch.setattr(agent_router_mod, "effective_config", lambda: ("ui", None))
    return fake


def _label_payload(*, confidence: float = 0.98, label: str = "图 1-4") -> dict:
    return {
        "needs_change": True,
        "label_ok": False,
        "suggested_label": label,
        "caption_issue": "none",
        "cleaned_caption": "",
        "crop_issue": "none",
        "confidence": confidence,
        "reason": "图号与上下文序列不连续",
    }


async def _create_proposal(client, figure_id: str, version_id: str, **overrides) -> str:
    Session = client.session_factory
    async with Session() as s:
        proposal = FigureProposal(
            figure_id=figure_id,
            paper_version_id=version_id,
            kind="figure_review",
            proposed_json=overrides.get(
                "proposed",
                {
                    "needs_change": True,
                    "label_ok": False,
                    "suggested_label": "图 1-4",
                    "crop_issue": "none",
                    "confidence": 0.99,
                },
            ),
            evidence_json=[{"type": "candidate_label", "value": "图 1-4"}],
            confidence=overrides.get("confidence", 0.99),
            status="pending",
            provider="fake",
            model="fake",
            prompt_version="p1",
            mode="text",
        )
        s.add(proposal)
        await s.commit()
        return proposal.id


# ---------------------------------------------------------------------------
# 自动接受器
# ---------------------------------------------------------------------------
async def test_auto_accept_high_confidence_label(client, monkeypatch) -> None:
    seed = await _seed(client)
    _mock_llm(monkeypatch, _label_payload())
    r = await client.post(f"/api/figures/{seed['figure_ids'][1]}/ai-review")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["auto_accepted"] is True
    assert body["decided_by"] == "auto-accepter"
    assert body["before"]["label_raw"] == "图 1-2"

    fig = (await client.get(f"/api/figures/{seed['figure_ids'][1]}")).json()
    assert fig["label_raw"] == "图 1-4"
    assert fig["label_source"] == "agent"
    # 自动接受的置信度如实落模型自评（0.98），不是一律 1.0
    assert fig["label_confidence"] == pytest.approx(0.98)

    audit = (await client.get("/api/audit", params={"target_type": "figure_proposal"})).json()
    assert audit and audit[0]["action"] == "proposal.auto_accept"
    assert audit[0]["actor"] == "auto-accepter"
    assert audit[0]["before"]["label_raw"] == "图 1-2"
    assert audit[0]["after"]["label_raw"] == "图 1-4"


async def test_auto_accept_skips_human_edited_label(client, monkeypatch) -> None:
    seed = await _seed(client)
    Session = client.session_factory
    async with Session() as s:
        fig = await s.get(Figure, seed["figure_ids"][1])
        fig.label_source = "review"
        fig.label_raw = "图 1-9"
        fig.label_norm = "fig:1-9"
        await s.commit()
    _mock_llm(monkeypatch, _label_payload())
    r = await client.post(f"/api/figures/{seed['figure_ids'][1]}/ai-review")
    assert r.status_code == 200
    assert r.json()["status"] == "pending"
    assert r.json()["auto_accepted"] is False
    fig = (await client.get(f"/api/figures/{seed['figure_ids'][1]}")).json()
    assert fig["label_raw"] == "图 1-9"  # 人工改过，自动接受器不许动


async def test_auto_accept_skips_low_confidence_and_crop_issue(client, monkeypatch) -> None:
    seed = await _seed(client)
    _mock_llm(monkeypatch, _label_payload(confidence=0.7))
    low = await client.post(f"/api/figures/{seed['figure_ids'][1]}/ai-review")
    assert low.json()["status"] == "pending"

    crop_payload = dict(_label_payload(), crop_issue="cut_off")
    _mock_llm(monkeypatch, crop_payload)
    crop = await client.post(f"/api/figures/{seed['figure_ids'][2]}/ai-review")
    assert crop.json()["status"] == "pending"


async def test_auto_accept_skips_when_disabled(client, monkeypatch) -> None:
    seed = await _seed(client)
    monkeypatch.setattr(settings, "auto_accept_enabled", False)
    _mock_llm(monkeypatch, _label_payload())
    r = await client.post(f"/api/figures/{seed['figure_ids'][1]}/ai-review")
    assert r.json()["status"] == "pending"


async def test_auto_accept_sweep_endpoint(client) -> None:
    seed = await _seed(client)
    proposal_id = await _create_proposal(client, seed["figure_ids"][1], seed["version_id"])
    r = await client.post("/api/proposals/auto-accept", json={"limit": 10})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] == 1
    assert body["items"][0]["proposal_id"] == proposal_id
    fig = (await client.get(f"/api/figures/{seed['figure_ids'][1]}")).json()
    assert fig["label_raw"] == "图 1-4"
    stats = (await client.get("/api/proposals/stats")).json()
    assert stats["accepted"] >= 1


# ---------------------------------------------------------------------------
# 回滚 / 批量 / 审计
# ---------------------------------------------------------------------------
async def test_rollback_restores_before_and_audits(client) -> None:
    seed = await _seed(client)
    proposal_id = await _create_proposal(client, seed["figure_ids"][1], seed["version_id"])
    assert (await client.post("/api/proposals/auto-accept", json={})).json()["accepted"] == 1

    r = await client.post(f"/api/proposals/{proposal_id}/rollback", json={"actor": "tester"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proposal"]["status"] == "rolled_back"
    assert body["proposal"]["rolled_back_by"] == "tester"
    fig = (await client.get(f"/api/figures/{seed['figure_ids'][1]}")).json()
    assert fig["label_raw"] == "图 1-2"
    assert fig["label_source"] == "text"

    audit = (
        await client.get(
            "/api/audit", params={"target_type": "figure_proposal", "target_id": proposal_id}
        )
    ).json()
    actions = [row["action"] for row in audit]
    assert "proposal.auto_accept" in actions
    assert "proposal.rollback" in actions


async def test_decide_batch_accept_and_reject(client) -> None:
    seed = await _seed(client)
    p1 = await _create_proposal(
        client, seed["figure_ids"][1], seed["version_id"], confidence=0.5
    )
    p2 = await _create_proposal(
        client, seed["figure_ids"][2], seed["version_id"], confidence=0.5
    )
    r = await client.post(
        "/api/proposals/decide-batch",
        json={"proposal_ids": [p1, p2], "action": "accept", "actor": "tester"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 2
    assert (await client.get(f"/api/figures/{seed['figure_ids'][1]}")).json()["label_raw"] == "图 1-4"

    # 已处理的不能再次采纳；批量接口要返回明细而不是整批 500
    again = await client.post(
        "/api/proposals/decide-batch",
        json={"proposal_ids": [p1], "action": "accept"},
    )
    assert again.json()["failed"][0]["proposal_id"] == p1


async def test_reject_writes_audit(client) -> None:
    seed = await _seed(client)
    proposal_id = await _create_proposal(
        client, seed["figure_ids"][1], seed["version_id"], confidence=0.4
    )
    r = await client.post(
        f"/api/proposals/{proposal_id}/decide",
        json={"action": "reject", "actor": "tester", "reason": "证据不足"},
    )
    assert r.status_code == 200
    assert r.json()["proposal"]["status"] == "rejected"
    audit = (await client.get("/api/audit", params={"action": "proposal.reject"})).json()
    assert audit and audit[0]["target_id"] == proposal_id


# ---------------------------------------------------------------------------
# 任务租约
# ---------------------------------------------------------------------------
async def test_task_lifecycle_claim_heartbeat_complete(client) -> None:
    r = await client.post(
        "/api/tasks",
        json={"kind": "auto_accept_sweep", "payload": {"limit": 5}},
    )
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]

    claimed = await client.post(f"/api/tasks/{task_id}/claim", json={"actor": "tester"})
    assert claimed.status_code == 200
    assert claimed.json()["status"] == "running"
    assert claimed.json()["attempts"] == 1
    assert claimed.json()["actor"] == "tester"

    beat = await client.post(
        f"/api/tasks/{task_id}/heartbeat", json={"actor": "tester", "lease_seconds": 60}
    )
    assert beat.status_code == 200
    assert beat.json()["lease_until"]

    # 别人不能 complete
    denied = await client.post(
        f"/api/tasks/{task_id}/complete", json={"actor": "other", "result": {}}
    )
    assert denied.status_code == 409

    done = await client.post(
        f"/api/tasks/{task_id}/complete",
        json={"actor": "tester", "result": {"ok": True}},
    )
    assert done.status_code == 200
    assert done.json()["status"] == "done"
    assert done.json()["result"]["ok"] is True


async def test_task_fail_retries_then_fails(client) -> None:
    r = await client.post("/api/tasks", json={"kind": "auto_accept_sweep", "max_attempts": 2})
    task_id = r.json()["id"]
    await client.post(f"/api/tasks/{task_id}/claim", json={"actor": "tester"})
    retry = await client.post(
        f"/api/tasks/{task_id}/fail", json={"actor": "tester", "error": "boom"}
    )
    assert retry.json()["status"] == "queued"
    assert retry.json()["attempts"] == 1

    await client.post(f"/api/tasks/{task_id}/claim", json={"actor": "tester"})
    final = await client.post(
        f"/api/tasks/{task_id}/fail", json={"actor": "tester", "error": "boom again"}
    )
    assert final.json()["status"] == "failed"
    assert final.json()["attempts"] == 2


async def test_expired_task_reclaimed_and_recovered(client) -> None:
    r = await client.post("/api/tasks", json={"kind": "auto_accept_sweep"})
    task_id = r.json()["id"]
    await client.post(f"/api/tasks/{task_id}/claim", json={"actor": "tester"})
    Session = client.session_factory
    async with Session() as s:
        task = await s.get(AgentTask, task_id)
        task.lease_until = datetime.now(timezone.utc) - timedelta(seconds=5)
        await s.commit()
    recovered = await client.post("/api/tasks/recover-expired")
    assert recovered.json()["recovered"] == 1
    task = (await client.get(f"/api/tasks/{task_id}")).json()
    assert task["status"] == "queued"
    assert task["actor"] is None


async def test_task_idempotency_key(client) -> None:
    body = {
        "kind": "auto_accept_sweep",
        "idempotency_key": "sweep-001",
        "payload": {"limit": 3},
    }
    first = await client.post("/api/tasks", json=body)
    second = await client.post("/api/tasks", json=body)
    assert first.json()["id"] == second.json()["id"]
    tasks = (await client.get("/api/tasks", params={"kind": "auto_accept_sweep"})).json()
    assert len([t for t in tasks if t["id"] == first.json()["id"]]) == 1


async def test_enqueue_figures_creates_and_skips(client, monkeypatch) -> None:
    seed = await _seed(client)
    monkeypatch.setattr(agent_router_mod, "effective_config", lambda: ("ui", None))
    body = {"label_state": "unreviewed_all", "limit": 10}
    first = await client.post("/api/tasks/enqueue-figures", json=body)
    assert first.status_code == 200, first.text
    assert first.json()["created"] == 3
    second = await client.post("/api/tasks/enqueue-figures", json=body)
    assert second.json()["created"] == 0
    assert second.json()["skipped"] == 3
    tasks = (await client.get("/api/tasks", params={"kind": "figure_review"})).json()
    assert len(tasks) == 3
    assert {t["target_id"] for t in tasks} == set(seed["figure_ids"])


async def test_worker_executes_figure_review_task(client, monkeypatch) -> None:
    seed = await _seed(client)
    Session = client.session_factory

    async def fake_ai_review(session, figure_id):
        return {
            "proposal_id": "p1",
            "status": "accepted",
            "confidence": 0.99,
            "auto_accepted": True,
            "proposed": {"needs_change": True},
        }

    monkeypatch.setattr(worker_tasks_mod, "ai_review_figure", fake_ai_review)
    monkeypatch.setattr(worker_tasks_mod, "get_sessionmaker", lambda: Session)
    async with Session() as s:
        task = AgentTask(
            kind="figure_review",
            target_type="figure",
            target_id=seed["figure_ids"][1],
            payload_json={"figure_id": seed["figure_ids"][1]},
        )
        s.add(task)
        await s.commit()
        task_id = task.id

    assert await worker_tasks_mod.run_task_once() == "done"
    task = (await client.get(f"/api/tasks/{task_id}")).json()
    assert task["status"] == "done"
    assert task["result"]["proposal_id"] == "p1"
    assert task["attempts"] == 1
    assert await worker_tasks_mod.run_task_once() is None


async def test_evaluate_auto_accept_returns_reasons() -> None:
    """纯函数级校验：不满足条件时要说清哪条不满足（便于 UI/日志解释）。"""
    fig = Figure(
        paper_version_id="v",
        page=1,
        bbox_json=[0, 0, 1, 1],
        label_raw="图 1-1",
        label_norm="fig:1-1",
        label_source="text",
        caption="caption",
        caption_source="parser",
        cite_key="x",
    )
    proposal = FigureProposal(
        figure_id="f",
        paper_version_id="v",
        kind="figure_review",
        proposed_json={"needs_change": True, "suggested_label": "图 1-2", "crop_issue": "none"},
        evidence_json=[{"type": "candidate_label", "value": "图 1-2"}],
        confidence=0.5,
    )
    eligible, reason, blocked = evaluate_auto_accept(fig, proposal)
    assert eligible is False
    assert blocked and "置信" in reason
