# SPDX-License-Identifier: AGPL-3.0-or-later
"""M3 两个新功能的端到端测试：整篇速览（AI 一键总结）+ 图提取 AI 纠错。

守的约束：
1. 整篇速览只调一次模型；证据必须属于本版本；人工编辑锁定后不覆盖（除非 force）；
2. 图纠错只产出 proposal，不直接改图；图号建议必须在候选集合内；
3. 采纳才写正式字段（图号 / 图注 / 裁框标记），拒绝留记录；
4. 未配置模型时给可读错误（不是 500，也不是假数据）。
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import litbench.figure_review as figure_review_mod
import litbench.paper_summary as paper_summary_mod
from litbench.config import settings
from litbench.db import get_session
from litbench.llm import LLMResult
from litbench.main import app
from litbench.models import Base, Block, Figure, Paper, PaperVersion, Summary, SummaryRevision

# 1x1 白色 PNG（测试用最小图片）
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FallbackClient:
    """第一次视觉调用抛 ReadTimeout，第二次文字调用成功。"""

    provider = "fake"
    model = "fake-vision-model"

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    async def complete(self, messages, *, json_schema=None, meta=None, max_tokens=None, timeout_s=None):
        self.calls += 1
        if self.calls == 1:
            from litbench.llm import LLMError

            raise LLMError("连不上 https://x/v1/chat/completions：ReadTimeout: ")
        return LLMResult(
            text=json.dumps(self.payload, ensure_ascii=False),
            parsed=self.payload,
            provider=self.provider,
            model=self.model,
            tokens_in=10,
            tokens_out=10,
        )


class FakeClient:
    """把预设 JSON 当模型输出；记录收到的 messages 供断言。"""

    provider = "fake"
    model = "fake-vision-model"

    def __init__(self, payload: dict, *, raise_error: str | None = None) -> None:
        self.payload = payload
        self.raise_error = raise_error
        self.calls: list[dict] = []

    async def complete(
        self, messages, *, json_schema=None, meta=None, max_tokens=None, timeout_s=None
    ):
        self.calls.append(
            {"messages": messages, "meta": meta, "max_tokens": max_tokens}
        )
        if self.raise_error:
            from litbench.llm import LLMError

            raise LLMError(self.raise_error)
        text = json.dumps(self.payload, ensure_ascii=False)
        return LLMResult(
            text=text,
            parsed=self.payload,
            provider=self.provider,
            model=self.model,
            tokens_in=120,
            tokens_out=60,
            cost=0.0012,
            latency_ms=42,
        )


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(settings, "llm_config_file", None)
    monkeypatch.setattr(settings, "llm_base_url", None)
    monkeypatch.setattr(settings, "llm_api_key", None)
    monkeypatch.setattr(settings, "llm_model", None)
    monkeypatch.setattr(settings, "llm_provider", "mock")
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'agent.db').as_posix()}", poolclass=NullPool
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


async def _seed(client, *, with_image: bool = True) -> dict:
    Session = client.session_factory
    async with Session() as s:
        paper = Paper(
            title="测试文献：图提取纠错",
            title_norm="测试文献图提取纠错",
            status="parsed",
            abstract="本文研究图提取与图号校核，提出一套可回溯的纠错流程。",
            authors_json=[{"firstName": "Ada", "lastName": "Byron"}],
            year=2024,
            venue="测试期刊",
        )
        s.add(paper)
        await s.flush()
        version = PaperVersion(
            paper_id=paper.id,
            kind="uploaded",
            file_sha256="b" * 64,
            file_path="pdfs/test.pdf",
            page_count=4,
            parser="docling",
            parsed_at=datetime.now(timezone.utc),
        )
        s.add(version)
        await s.flush()

        raw = [
            (1, 0, "section", "1. 引言"),
            (1, 1, "text", "图提取是文献证据库的第一道工序，错一张图就会影响后续引用。"),
            (1, 2, "text", "本文提出把 AI 纠错做成建议队列，人工采纳后才写正式字段。"),
            (2, 3, "section", "2. 方法"),
            (2, 4, "text", "方法分为图号复核、图注清洗与裁框检查三步，每步都有证据校验。"),
            (3, 5, "section", "3. 结论"),
            (3, 6, "text", "实验表明建议队列能把人工复核成本降低，同时保留可追溯性。"),
            (4, 7, "text", "局限性：样本只覆盖五本教材，跨语言文献还未验证。"),
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

        if with_image:
            image_dir = client.tmp_path / "images"
            image_dir.mkdir(parents=True, exist_ok=True)
            (image_dir / "fig-test.png").write_bytes(PNG_1PX)
            image_path = "images/fig-test.png"
        else:
            image_path = None

        figs = []
        for idx, (label, caption, page) in enumerate(
            [
                ("图 1-1", "图 1-1 图提取流程示意", 2),
                ("图 1-2", "图 1-2 图 号 校 核 的 三 个 步 骤", 2),
                ("图 1-3", "图 1-3 建议队列与人工采纳", 3),
            ]
        ):
            fig = Figure(
                paper_version_id=version.id,
                page=page,
                bbox_json=[10.0 + idx * 10, 100.0, 200.0 + idx * 10, 220.0],
                label_raw=label,
                label_norm=f"fig:1-{'123'[idx]}",
                label_num=idx + 1,
                caption=caption,
                license_hint="unknown",
                cite_key=f"test-v1-f{idx + 1}",
                label_source="text",
                label_confidence=0.5 if idx == 1 else 0.9,
                image_path=image_path,
                origin="parser",
                review_status="pending",
            )
            s.add(fig)
            figs.append(fig)
        await s.commit()
        return {
            "paper_id": paper.id,
            "version_id": version.id,
            "block_ids": [b.id for b in blocks],
            "figure_ids": [f.id for f in figs],
        }


def _mock_llm(monkeypatch, module, client_obj) -> None:
    monkeypatch.setattr(module, "get_client", lambda provider=None: client_obj)
    monkeypatch.setattr(module, "effective_config", lambda: ("ui", None))


# ---------------------------------------------------------------------------
# 整篇速览
# ---------------------------------------------------------------------------
async def test_paper_summary_404_before_generation(client) -> None:
    seed = await _seed(client, with_image=False)
    r = await client.get(f"/api/papers/{seed['paper_id']}/summary")
    assert r.status_code == 404
    assert "整篇速览" in r.json()["detail"]


async def test_paper_summary_generate_and_read_detail(client, monkeypatch) -> None:
    seed = await _seed(client, with_image=False)
    payload = {
        "summary": "本文研究图提取纠错，提出建议队列 + 人工采纳的流程，实验证明能降低成本。",
        "key_points": ["问题：图提取错误影响引用", "方法：建议队列 + 证据校验", "结果：复核成本下降"],
        "evidence_block_ids": seed["block_ids"][:3],
        "confidence": 0.93,
    }
    fake = FakeClient(payload)
    _mock_llm(monkeypatch, paper_summary_mod, fake)

    r = await client.post(f"/api/papers/{seed['paper_id']}/summary", json={})
    assert r.status_code == 200, r.text
    stats = r.json()
    assert stats["status"] == "created"
    assert stats["provider"] == "fake"
    assert stats["tokens_in"] == 120 and stats["tokens_out"] == 60
    assert stats["cost"] == pytest.approx(0.0012)
    assert stats["auto_accepted"] is True
    assert stats["summary_id"]
    # 只有一次模型调用（整篇一次，不是按章）
    assert len(fake.calls) == 1
    prompt_text = fake.calls[0]["messages"][0]["content"]
    assert "测试文献：图提取纠错" in prompt_text
    assert "章节大纲" in prompt_text
    assert str(fake.calls[0]["max_tokens"]) == str(settings.paper_summary_max_tokens)

    detail = (await client.get(f"/api/papers/{seed['paper_id']}/summary")).json()
    assert detail["section_key"] == "__paper__"
    assert detail["section_title"] == "整篇速览"
    assert detail["status"] == "published"
    # M2b：整篇速览也记录自评置信度（和章节摘要同一套状态机）
    assert detail["confidence"] == pytest.approx(0.93)
    assert detail["evidence_block_ids"] == seed["block_ids"][:3]
    assert len(detail["evidence"]) == 3
    assert detail["content"].startswith("本文研究图提取纠错")
    # 章节摘要列表里不应混进整篇速览
    chapters = (await client.get(f"/api/papers/{seed['paper_id']}/summaries")).json()
    assert all(s["section_key"] != "__paper__" for s in chapters)

    # 重新生成：更新 + 留 revision
    # （首次生成自动接受后 status=published；不 force 会被当作「已发布」拒绝覆盖，
    #   与章节摘要 skipped_existing 语义对齐——要覆盖必须显式 force）
    payload2 = dict(payload, summary="更新后的速览。", confidence=0.8)
    fake.payload = payload2
    no_force = await client.post(f"/api/papers/{seed['paper_id']}/summary", json={})
    assert no_force.status_code == 422
    assert "覆盖" in no_force.json()["detail"]
    r2 = await client.post(
        f"/api/papers/{seed['paper_id']}/summary", json={"force": True}
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "updated"
    revisions = (
        await client.get(f"/api/summaries/{stats['summary_id']}/revisions")
    ).json()
    assert any(rev["reason"] == "regenerated" for rev in revisions)


async def test_paper_summary_requires_configured_model(client) -> None:
    seed = await _seed(client, with_image=False)
    r = await client.post(f"/api/papers/{seed['paper_id']}/summary", json={})
    assert r.status_code == 422
    assert "还没有配置模型" in r.json()["detail"]


async def test_paper_summary_invalid_evidence_is_rejected(client, monkeypatch) -> None:
    seed = await _seed(client, with_image=False)
    fake = FakeClient(
        {
            "summary": "编造摘要",
            "key_points": ["编造"],
            "evidence_block_ids": ["not-a-block"],
            "confidence": 0.99,
        }
    )
    _mock_llm(monkeypatch, paper_summary_mod, fake)
    r = await client.post(f"/api/papers/{seed['paper_id']}/summary", json={})
    assert r.status_code == 422
    Session = client.session_factory
    async with Session() as s:
        rows = (
            await s.execute(select(Summary).where(Summary.section_key == "__paper__"))
        ).scalars().all()
    assert rows == []


async def test_paper_summary_locked_needs_force(client, monkeypatch) -> None:
    seed = await _seed(client, with_image=False)
    fake = FakeClient(
        {
            "summary": "第一版速览。",
            "key_points": ["要点"],
            "evidence_block_ids": seed["block_ids"][:2],
            "confidence": 0.95,
        }
    )
    _mock_llm(monkeypatch, paper_summary_mod, fake)
    created = (await client.post(f"/api/papers/{seed['paper_id']}/summary", json={})).json()

    # 人工编辑 → 锁定
    r = await client.patch(
        f"/api/summaries/{created['summary_id']}",
        json={"content": "人工改过的速览内容。", "key_points": ["人工要点"]},
    )
    assert r.status_code == 200

    locked = await client.post(f"/api/papers/{seed['paper_id']}/summary", json={})
    assert locked.status_code == 422
    assert "人工编辑锁定" in locked.json()["detail"]

    forced = await client.post(
        f"/api/papers/{seed['paper_id']}/summary", json={"force": True}
    )
    assert forced.status_code == 200
    assert forced.json()["status"] == "updated"
    detail = (await client.get(f"/api/papers/{seed['paper_id']}/summary")).json()
    assert detail["content"] == "第一版速览。"


# ---------------------------------------------------------------------------
# 图提取 AI 纠错
# ---------------------------------------------------------------------------
def _figure_payload(**overrides) -> dict:
    payload = {
        "label_ok": False,
        "suggested_label": "图 1-3",
        "caption_issue": "ocr_noise",
        "cleaned_caption": "图 1-2 图号校核的三个步骤",
        "crop_issue": "cut_off",
        "confidence": 0.91,
        "reason": "caption 中有明显空格断字，且图号与邻近图不连续",
        "summary": "建议改图号为图 1-3，并清洗图注",
    }
    payload.update(overrides)
    return payload


async def test_figure_ai_review_creates_proposal_only(client, monkeypatch) -> None:
    seed = await _seed(client)
    target = seed["figure_ids"][1]
    fake = FakeClient(_figure_payload())
    _mock_llm(monkeypatch, figure_review_mod, fake)

    r = await client.post(f"/api/figures/{target}/ai-review")
    assert r.status_code == 200, r.text
    proposal = r.json()
    assert proposal["status"] == "pending"
    assert proposal["mode"] == "vision"  # seed 里有图片文件
    assert proposal["confidence"] == pytest.approx(0.91)
    assert proposal["proposed"]["suggested_label"] == "图 1-3"
    assert proposal["proposed"]["cleaned_caption"] == "图 1-2 图号校核的三个步骤"
    assert proposal["proposed"]["crop_issue"] == "cut_off"
    assert proposal["proposed"]["caption_issue"] == "ocr_noise"
    # 图片作为 content 块发给了模型
    content = fake.calls[0]["messages"][0]["content"]
    assert isinstance(content, list) and any(b.get("type") == "image" for b in content)

    # 只产出建议：图本身没变
    fig = (await client.get(f"/api/figures/{target}")).json()
    assert fig["label_raw"] == "图 1-2"
    assert fig["caption"] == "图 1-2 图 号 校 核 的 三 个 步 骤"

    pending = (await client.get("/api/proposals")).json()
    assert [p["proposal_id"] for p in pending] == [proposal["proposal_id"]]


async def test_figure_ai_review_rejects_label_outside_candidates(client, monkeypatch) -> None:
    seed = await _seed(client)
    fake = FakeClient(_figure_payload(suggested_label="图 9-9"))
    _mock_llm(monkeypatch, figure_review_mod, fake)
    proposal = (await client.post(f"/api/figures/{seed['figure_ids'][1]}/ai-review")).json()
    assert proposal["proposed"]["suggested_label"] is None
    notes = proposal["proposed"]["validation_notes"]
    assert any("不在候选集合" in note for note in notes)


async def test_figure_ai_review_text_mode_without_image(client, monkeypatch) -> None:
    seed = await _seed(client, with_image=False)
    fake = FakeClient(_figure_payload(crop_issue="unknown"))
    _mock_llm(monkeypatch, figure_review_mod, fake)
    proposal = (await client.post(f"/api/figures/{seed['figure_ids'][1]}/ai-review")).json()
    assert proposal["mode"] == "text"
    content = fake.calls[0]["messages"][0]["content"]
    assert isinstance(content, str)  # 纯文本，不是图片块


async def test_figure_proposal_accept_applies_and_reject_keeps(client, monkeypatch) -> None:
    seed = await _seed(client)
    fake = FakeClient(_figure_payload())
    _mock_llm(monkeypatch, figure_review_mod, fake)
    proposal = (await client.post(f"/api/figures/{seed['figure_ids'][1]}/ai-review")).json()

    r = await client.post(
        f"/api/proposals/{proposal['proposal_id']}/decide", json={"action": "accept"}
    )
    assert r.status_code == 200, r.text
    decided = r.json()
    assert decided["proposal"]["status"] == "accepted"
    assert decided["applied"]["label_raw"] == "图 1-3"
    assert decided["applied"]["caption"] == "图 1-2 图号校核的三个步骤"
    assert decided["applied"]["crop_issue"] == "cut_off"
    assert decided["figure"]["label_raw"] == "图 1-3"
    assert decided["figure"]["label_source"] == "review"
    assert decided["figure"]["review_status"] == "wrong"

    # 同一条建议不能重复采纳
    again = await client.post(
        f"/api/proposals/{proposal['proposal_id']}/decide", json={"action": "accept"}
    )
    assert again.status_code == 422

    # 拒绝：图不变，记录保留
    fig2 = seed["figure_ids"][2]
    proposal2 = (await client.post(f"/api/figures/{fig2}/ai-review")).json()
    r2 = await client.post(
        f"/api/proposals/{proposal2['proposal_id']}/decide",
        json={"action": "reject", "reason": "人工看是原编号对"},
    )
    assert r2.status_code == 200
    assert r2.json()["proposal"]["status"] == "rejected"
    fig_after = (await client.get(f"/api/figures/{fig2}")).json()
    assert fig_after["label_raw"] == "图 1-3"
    rejected = (await client.get("/api/proposals", params={"status": "rejected"})).json()
    assert [p["proposal_id"] for p in rejected] == [proposal2["proposal_id"]]


async def test_figure_ai_review_supersedes_old_pending(client, monkeypatch) -> None:
    seed = await _seed(client)
    target = seed["figure_ids"][1]
    fake = FakeClient(_figure_payload())
    _mock_llm(monkeypatch, figure_review_mod, fake)
    first = (await client.post(f"/api/figures/{target}/ai-review")).json()
    second = (await client.post(f"/api/figures/{target}/ai-review")).json()
    pending = (await client.get("/api/proposals")).json()
    assert [p["proposal_id"] for p in pending] == [second["proposal_id"]]
    old = (
        await client.get(f"/api/proposals/{first['proposal_id']}")
    ).json()
    assert old["status"] == "superseded"


def test_candidate_labels_include_neighbor_sequence() -> None:
    """当前图无编号时，邻图编号 ±1 应进候选（真机：图 HM4 实为图 10-14）。"""
    from litbench.figure_review import _candidate_labels, _ordered_figures

    current = Figure(
        id="cur", page=19, bbox_json=[0, 0, 10, 10], label_raw=None, label_num=None
    )
    prev = Figure(
        id="prev", page=19, bbox_json=[0, 20, 10, 30], label_raw="图 10-13", label_num=13
    )
    nxt = Figure(
        id="next", page=19, bbox_json=[0, 40, 10, 50], label_raw="图 10-15", label_num=15
    )
    candidates = _candidate_labels(current, _ordered_figures([current, prev, nxt]))
    assert "图 10-14" in candidates
    assert "图 10-16" in candidates


async def test_figure_ai_review_no_change_skips_pending_queue(client, monkeypatch) -> None:
    """检查后没问题 → status=no_change，不进待审队列（避免每条正常图都占一个人工点）。"""
    seed = await _seed(client)
    fake = FakeClient(
        _figure_payload(
            label_ok=True,
            suggested_label="",
            caption_issue="none",
            cleaned_caption="",
            crop_issue="none",
            confidence=0.95,
        )
    )
    _mock_llm(monkeypatch, figure_review_mod, fake)
    proposal = (await client.post(f"/api/figures/{seed['figure_ids'][1]}/ai-review")).json()
    assert proposal["status"] == "no_change"
    assert proposal["proposed"]["needs_change"] is False
    assert (await client.get("/api/proposals")).json() == []


async def test_figure_ai_review_falls_back_to_text_when_vision_fails(client, monkeypatch) -> None:
    """视觉调用超时/不支持图片时降级为纯文字判断，仍产出建议（裁框标 unknown）。"""
    seed = await _seed(client)
    fake = FallbackClient(
        _figure_payload(
            suggested_label="图 1-3",
            cleaned_caption="图 1-2 图号校核的三个步骤",
            crop_issue="unknown",
        )
    )
    _mock_llm(monkeypatch, figure_review_mod, fake)
    proposal = (await client.post(f"/api/figures/{seed['figure_ids'][1]}/ai-review")).json()
    assert fake.calls == 2
    assert proposal["mode"] == "text"
    assert proposal["proposed"]["crop_issue"] == "unknown"
    assert proposal["proposed"]["suggested_label"] == "图 1-3"


async def test_figure_ai_review_requires_configured_model(client) -> None:
    seed = await _seed(client)
    r = await client.post(f"/api/figures/{seed['figure_ids'][1]}/ai-review")
    assert r.status_code == 422
    assert "还没有配置模型" in r.json()["detail"]


async def test_figure_ai_review_model_error_is_readable(client, monkeypatch) -> None:
    seed = await _seed(client)
    fake = FakeClient({}, raise_error="API 返回 HTTP 400：不支持图片输入")
    _mock_llm(monkeypatch, figure_review_mod, fake)
    r = await client.post(f"/api/figures/{seed['figure_ids'][1]}/ai-review")
    assert r.status_code == 422
    assert "不支持图片输入" in r.json()["detail"]


async def test_proposals_filter_by_paper(client, monkeypatch) -> None:
    seed = await _seed(client)
    fake = FakeClient(_figure_payload())
    _mock_llm(monkeypatch, figure_review_mod, fake)
    await client.post(f"/api/figures/{seed['figure_ids'][1]}/ai-review")
    same = (
        await client.get("/api/proposals", params={"paper_id": seed["paper_id"]})
    ).json()
    assert len(same) == 1
    other = (await client.get("/api/proposals", params={"paper_id": "nope"})).json()
    assert other == []
