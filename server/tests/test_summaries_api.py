# SPDX-License-Identifier: AGPL-3.0-or-later
"""章节摘要端到端测试（M2a）：章节树 → mock 生成 → 证据校验 → 落库 → 编辑/回滚。

所有断言都走 HTTP（用户真实路径），只在需要制造「模型输出异常」时替换 provider。
重点守三条硬约束：
1. 证据必须真实、非空，否则**不落库**；
2. 人工编辑后（source=human_edited）自动生成不再覆盖，除非显式 force；
3. 生成/编辑前留 revision，可回滚。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.config import settings
from litbench.db import get_session
from litbench.main import app
from litbench.models import Base, Block, Paper, PaperVersion, Summary, SummaryRevision
from litbench.llm import LLMError, MockLLM
import litbench.summaries as summaries_mod


@pytest.fixture
async def client(tmp_path, monkeypatch):
    # 摘要测试固定走 mock：不要被本机 .data/llm-config.json 或环境变量里的真实 key 影响
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(settings, "llm_config_file", None)
    monkeypatch.setattr(settings, "llm_base_url", None)
    monkeypatch.setattr(settings, "llm_api_key", None)
    monkeypatch.setattr(settings, "llm_model", None)
    monkeypatch.setattr(settings, "llm_provider", "mock")
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'sum.db').as_posix()}", poolclass=NullPool
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


async def _seed(client, *, page_count: int = 3, parser: str = "docling") -> dict:
    Session = client.session_factory
    async with Session() as s:
        paper = Paper(title="测试文献：相变传热", title_norm="测试文献相变传热", status="parsed")
        s.add(paper)
        await s.flush()
        version = PaperVersion(
            paper_id=paper.id,
            kind="uploaded",
            file_sha256="a" * 64,
            file_path="pdfs/test.pdf",
            page_count=page_count,
            parser=parser,
            parsed_at=datetime.now(timezone.utc),
        )
        s.add(version)
        await s.flush()
        raw = [
            (1, 0, "section", "第 7 章 相变传热"),
            (1, 1, "text", "本章介绍相变传热的基本规律与工程计算方法，重点讨论凝结与沸腾。"),
            (1, 2, "text", "相变过程伴随潜热释放，是工程换热设备设计的核心问题之一。"),
            (2, 3, "section", "7.1 凝结传热的模式"),
            (2, 4, "text", "凝结分为珠状凝结与膜状凝结两种基本形式，膜状凝结是工程设计的依据。"),
            (2, 5, "caption", "图 7-1 不同润湿能力壁面上液膜形成的接触角"),
            (3, 6, "section", "7.2 沸腾传热的模式"),
            (3, 7, "text", "大容器沸腾分为自然对流区、核态沸腾区、过渡沸腾区和膜态沸腾区。"),
            (3, 8, "text", "临界热流密度是沸腾传热设备安全运行的重要限制条件。"),
        ]
        blocks = []
        for page, order, btype, text in raw:
            block = Block(
                paper_version_id=version.id,
                page=page,
                order_idx=order,
                bbox_json=[10.0, 10.0, 200.0, 40.0],
                type=btype,
                text=text,
            )
            s.add(block)
            blocks.append(block)
        await s.commit()
        return {
            "paper_id": paper.id,
            "version_id": version.id,
            "block_ids": [b.id for b in blocks],
        }


async def _db_count(client, table) -> int:
    Session = client.session_factory
    async with Session() as s:
        if table == "summary":
            rows = (await s.execute(select(Summary))).scalars().all()
        else:
            rows = (await s.execute(select(SummaryRevision))).scalars().all()
        return len(rows)


async def test_sections_endpoint_builds_tree_without_summaries(client) -> None:
    seed = await _seed(client)
    r = await client.get(f"/api/papers/{seed['paper_id']}/sections")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["version_id"] == seed["version_id"]
    titles = [s["title"] for s in data["sections"]]
    assert titles == ["第 7 章 相变传热", "7.1 凝结传热的模式", "7.2 沸腾传热的模式"]
    assert all(s["summary"] is None for s in data["sections"])
    assert all(s["block_count"] >= 1 and s["char_count"] > 0 for s in data["sections"])


async def test_generate_mock_end_to_end_and_evidence_is_valid(client) -> None:
    seed = await _seed(client)
    r = await client.post(f"/api/papers/{seed['paper_id']}/summaries/generate", json={})
    assert r.status_code == 200, r.text
    stats = r.json()
    assert stats["provider"] == "mock"
    assert stats["created"] == 3
    assert stats["failed"] == 0
    assert stats["prompt_version"]

    summaries = (await client.get(f"/api/papers/{seed['paper_id']}/summaries")).json()
    assert len(summaries) == 3
    allowed = set(seed["block_ids"])
    for summary in summaries:
        assert summary["source"] == "agent"
        assert summary["status"] == "published"
        assert summary["auto_accepted"] is True
        assert summary["evidence_block_ids"], "证据不能为空"
        assert set(summary["evidence_block_ids"]) <= allowed
        assert summary["content"]

    # 详情带证据块原文（点证据能回原文）
    detail = (await client.get(f"/api/summaries/{summaries[0]['summary_id']}")).json()
    assert detail["evidence"]
    assert detail["evidence"][0]["text"]
    assert detail["evidence"][0]["block_id"] in allowed
    # M2b：模型自评置信度落库（自动接受阈值校准的依据）
    assert detail["confidence"] == pytest.approx(0.95)


async def test_edit_locks_summary_and_regenerate_skips(client) -> None:
    seed = await _seed(client)
    await client.post(f"/api/papers/{seed['paper_id']}/summaries/generate", json={})
    summaries = (await client.get(f"/api/papers/{seed['paper_id']}/summaries")).json()
    target = summaries[0]

    r = await client.patch(
        f"/api/summaries/{target['summary_id']}",
        json={"content": "这是人工改写后的摘要，保留原意但换了措辞。", "key_points": ["人工要点"]},
    )
    assert r.status_code == 200, r.text
    edited = r.json()
    assert edited["source"] == "human_edited"
    assert edited["human_locked"] is True
    assert edited["status"] == "published"

    revisions = (await client.get(f"/api/summaries/{target['summary_id']}/revisions")).json()
    assert len(revisions) == 1
    assert revisions[0]["reason"] == "edited"
    assert revisions[0]["content"] == target["content"]

    # 不 force：人工锁住的不覆盖，已发布的 agent 摘要也不重复生成
    stats = (
        await client.post(f"/api/papers/{seed['paper_id']}/summaries/generate", json={})
    ).json()
    assert stats["created"] == 0
    assert stats["skipped_locked"] == 1
    assert stats["skipped_existing"] == 2

    # force：显式重生成（旧内容留 revision）
    stats = (
        await client.post(
            f"/api/papers/{seed['paper_id']}/summaries/generate", json={"force": True}
        )
    ).json()
    assert stats["updated"] == 3
    refreshed = (await client.get(f"/api/papers/{seed['paper_id']}/summaries")).json()
    assert all(s["source"] == "agent" for s in refreshed)
    assert await _db_count(client, "revision") == 4  # 1 次编辑 + 3 次重生成


async def test_invalid_evidence_is_rejected_and_nothing_stored(client, monkeypatch) -> None:
    seed = await _seed(client)
    monkeypatch.setattr(
        summaries_mod,
        "get_client",
        lambda provider=None: MockLLM(evidence_ids=["block-id-not-in-this-section"]),
    )
    r = await client.post(f"/api/papers/{seed['paper_id']}/summaries/generate", json={})
    assert r.status_code == 200, r.text
    stats = r.json()
    assert stats["failed"] == 3
    assert stats["created"] == 0
    assert "不属于本章节" in stats["results"][0]["error"]
    assert await _db_count(client, "summary") == 0  # 证据不合格 → 不落库


async def test_bad_json_and_low_confidence(client, monkeypatch) -> None:
    seed = await _seed(client)
    monkeypatch.setattr(
        summaries_mod, "get_client", lambda provider=None: MockLLM(raw="这不是 JSON")
    )
    stats = (
        await client.post(f"/api/papers/{seed['paper_id']}/summaries/generate", json={})
    ).json()
    assert stats["failed"] == 3
    assert "JSON" in stats["results"][0]["error"]
    # M2b：失败也要能诊断（把模型原始输出前 200 字带出来），并给这次失败记账
    assert "原始输出前 200 字" in stats["results"][0]["error"]
    assert stats["results"][0]["tokens_in"] > 0
    assert stats["tokens_in_total"] > 0

    monkeypatch.setattr(
        summaries_mod, "get_client", lambda provider=None: MockLLM(confidence=0.5)
    )
    stats = (
        await client.post(f"/api/papers/{seed['paper_id']}/summaries/generate", json={})
    ).json()
    assert stats["created"] == 3
    summaries = (await client.get(f"/api/papers/{seed['paper_id']}/summaries")).json()
    assert all(s["status"] == "draft" and s["auto_accepted"] is False for s in summaries)
    assert all(s["confidence"] == pytest.approx(0.5) for s in summaries)


async def test_publish_and_restore_revision(client) -> None:
    seed = await _seed(client)
    await client.post(f"/api/papers/{seed['paper_id']}/summaries/generate", json={})
    summaries = (await client.get(f"/api/papers/{seed['paper_id']}/summaries")).json()
    target = summaries[0]
    original = target["content"]

    await client.patch(
        f"/api/summaries/{target['summary_id']}",
        json={"content": "第一版人工摘要。"},
    )
    await client.patch(
        f"/api/summaries/{target['summary_id']}",
        json={"content": "第二版人工摘要。"},
    )
    revisions = (await client.get(f"/api/summaries/{target['summary_id']}/revisions")).json()
    assert len(revisions) == 2
    first_revision = next(r for r in revisions if r["content"] == original)

    r = await client.post(
        f"/api/summaries/{target['summary_id']}/revisions/{first_revision['revision_id']}/restore"
    )
    assert r.status_code == 200, r.text
    restored = r.json()
    assert restored["content"] == original
    assert restored["source"] == "human_edited"
    assert restored["human_locked"] is True
    assert len((await client.get(f"/api/summaries/{target['summary_id']}/revisions")).json()) == 3


async def test_paper_without_version_returns_readable_error(client) -> None:
    Session = client.session_factory
    async with Session() as s:
        paper = Paper(title="没解析的文献", title_norm="没解析的文献")
        s.add(paper)
        await s.commit()
        paper_id = paper.id

    r = await client.get(f"/api/papers/{paper_id}/sections")
    assert r.status_code == 422
    assert "解析版本" in r.json()["detail"]
    r = await client.post(f"/api/papers/{paper_id}/summaries/generate", json={})
    assert r.status_code == 422


async def test_unconfigured_real_provider_returns_readable_error(client, monkeypatch) -> None:
    from litbench.config import settings

    seed = await _seed(client)
    monkeypatch.setattr(settings, "llm_provider", "deepseek")
    r = await client.post(f"/api/papers/{seed['paper_id']}/summaries/generate", json={})
    assert r.status_code == 422
    assert "还没有配置模型" in r.json()["detail"]


# ---------------------------------------------------------------------------
# M2b：置信度落库、费用记账、推理预算耗尽自动重试
# ---------------------------------------------------------------------------
class _BudgetRetryClient:
    """第一次调用模拟「推理吃光输出预算」，第二次（翻倍预算）成功。"""

    provider = "mock"
    model = "budget-retry"

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._inner = MockLLM()

    async def complete(
        self,
        messages,
        *,
        json_schema=None,
        meta=None,
        max_tokens=None,
        timeout_s=None,
    ):
        self.calls.append({"max_tokens": max_tokens, "timeout_s": timeout_s})
        if len(self.calls) == 1:
            raise LLMError("模型只输出了推理过程、没有正文（输出预算被推理耗尽）")
        return await self._inner.complete(
            messages,
            json_schema=json_schema,
            meta=meta,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
        )


async def test_generate_records_confidence_and_spend(client) -> None:
    seed = await _seed(client)
    stats = (
        await client.post(f"/api/papers/{seed['paper_id']}/summaries/generate", json={})
    ).json()
    assert stats["tokens_in_total"] > 0
    assert stats["tokens_out_total"] > 0
    assert stats["cost_total"] == pytest.approx(0.0)  # mock 不计费
    assert all(r["confidence"] == pytest.approx(0.95) for r in stats["results"])

    # force 重生成：旧行压进 revision，revision 里也留着当时的置信度
    await client.post(
        f"/api/papers/{seed['paper_id']}/summaries/generate", json={"force": True}
    )
    summaries = (await client.get(f"/api/papers/{seed['paper_id']}/summaries")).json()
    revisions = (
        await client.get(f"/api/summaries/{summaries[0]['summary_id']}/revisions")
    ).json()
    assert revisions
    assert revisions[0]["confidence"] == pytest.approx(0.95)


async def test_generate_uses_section_budget_and_retries_when_reasoning_exhausts(
    client, monkeypatch
) -> None:
    seed = await _seed(client)
    sections = (
        await client.get(f"/api/papers/{seed['paper_id']}/sections")
    ).json()["sections"]
    target = next(s for s in sections if s["block_count"] > 0)

    fake = _BudgetRetryClient()
    monkeypatch.setattr(summaries_mod, "get_client", lambda provider=None: fake)
    stats = (
        await client.post(
            f"/api/papers/{seed['paper_id']}/summaries/generate",
            json={"section_keys": [target["key"]]},
        )
    ).json()
    assert stats["created"] == 1
    assert fake.calls[0]["max_tokens"] == settings.section_summary_max_tokens
    assert fake.calls[1]["max_tokens"] == settings.section_summary_max_tokens * 2
    assert fake.calls[1]["timeout_s"] is not None

