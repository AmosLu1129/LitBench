# SPDX-License-Identifier: AGPL-3.0-or-later
"""按需升级（pdfium → Docling）的测试。

两条线：

1. ``find_weakness`` 是纯函数，直接构造 ``ParsedDocument`` 单测——阈值以后要调，改完立刻能验证；
2. worker 里的行为（弱就排增强 job、强就不排、只升一级、同版本不重复排）用真实 HTTP + SQLite 验证。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.adapters import ParsedBlock, ParsedDocument, ParsedFigure, ParsedTable
from litbench.config import settings
from litbench.db import get_session
from litbench.escalate import can_escalate, find_weakness
from litbench.main import app
from litbench.models import Base, Job
from litbench.workers import run as worker
from pdf_fixture import make_pdf

# ----------------------------------------------------------------------
# 纯函数：判据
# ----------------------------------------------------------------------
def make_doc(
    *,
    captions: tuple[str, ...] = (),
    figures: tuple[tuple[str, str], ...] = (),
    text_layer: bool = True,
) -> ParsedDocument:
    blocks = [
        ParsedBlock(page=1, bbox=[0, 0, 10, 10], type="caption", text=text, order_idx=i)
        for i, text in enumerate(captions)
    ]
    figs = [
        ParsedFigure(page=1, bbox=[0, 0, 10, 10], label_raw=label, caption=caption)
        for label, caption in figures
    ]
    return ParsedDocument(
        page_count=1,
        engine="fake",
        engine_version="0",
        blocks=blocks,
        figures=figs,
        tables=[],
        text_layer=text_layer,
    )


LONG = "x" * 80  # 超过 40 字阈值


def test_missing_anchor_is_flagged() -> None:
    """图注里有 Fig.2，但没有对应的图 → 轻量档漏了一张。"""
    doc = make_doc(
        captions=("Fig. 1. Long enough caption here", "Fig. 2. Another caption"),
        figures=(("Fig. 1", LONG),),
    )
    weakness = find_weakness(doc)
    assert weakness is not None
    assert weakness.code == "unanchored_captions"
    assert weakness.missing_labels == 1


def test_all_anchored_is_fine() -> None:
    doc = make_doc(
        captions=("Fig. 1. Long enough caption here",),
        figures=(("Fig. 1", LONG),),
    )
    assert find_weakness(doc) is None


def test_short_captions_are_flagged() -> None:
    """两张图都在，但图注只有几个字——版面其实没读懂，Docling 的 caption 通常 200+ 字。"""
    doc = make_doc(
        captions=("Fig. 1. Short", "Fig. 2. Also short"),
        figures=(("Fig. 1", "Fig. 1. Short"), ("Fig. 2", "Fig. 2. Also")),
    )
    weakness = find_weakness(doc)
    assert weakness is not None
    assert weakness.code == "short_captions"
    assert weakness.avg_caption_chars < 40


def test_scanned_pdf_does_not_escalate() -> None:
    """没有文字层是 OCR 的事，拿版面模型去跑只会得到一堆空 block。"""
    doc = make_doc(captions=("Fig. 1.",), figures=(), text_layer=False)
    assert find_weakness(doc) is None


def test_min_missing_threshold_filters_one_off() -> None:
    """缺 1 个可能是子图/合并图号，可以把阈值调到 2 只抓明显的。"""
    doc = make_doc(
        captions=("Fig. 1. Long enough caption", "Fig. 2. Missing one"),
        figures=(("Fig. 1", LONG),),
    )
    assert find_weakness(doc) is not None
    assert find_weakness(doc, min_missing=2) is None


def test_can_escalate_rejects_unusable_targets() -> None:
    assert can_escalate("", "pdfium")[0] is False
    assert can_escalate("docling", "docling")[0] is False  # 已经是增强档
    assert can_escalate("nonsense", "pdfium")[0] is False
    assert can_escalate("mineru", "pdfium", mineru_url=None)[0] is False


# ----------------------------------------------------------------------
# worker：真的会排 job、排一次、且不会自己繁殖
# ----------------------------------------------------------------------
class StaticParser:
    """不做解析，直接返回一份构造好的结果。"""

    name = "static"

    def __init__(self, document: ParsedDocument) -> None:
        self.document = document

    async def parse(self, pdf_path):  # noqa: ANN001
        return self.document


WEAK = make_doc(
    captions=("Fig. 1. Long enough caption here", "Fig. 2. Missing anchor"),
    figures=(("Fig. 1", LONG),),
)
HEALTHY = make_doc(
    captions=("Fig. 1. Long enough caption here",),
    figures=(("Fig. 1", LONG),),
)


@pytest.fixture
async def client(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setattr(settings, "storage_dir", storage)
    monkeypatch.setattr(settings, "parser_backend", "pdfium")
    monkeypatch.setattr(settings, "escalate_backend", "docling")
    # 测试不依赖本机是否装了 Docling：可用性判断单独测（见 test_can_escalate_*）
    monkeypatch.setattr(worker, "can_escalate", lambda *a, **kw: (True, ""))

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(worker, "get_sessionmaker", lambda: Session)

    async def _session():
        async with Session() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        ac.session_factory = Session  # type: ignore[attr-defined]
        yield ac
    app.dependency_overrides.clear()
    await engine.dispose()


async def _upload(client) -> dict:
    r = await client.post(
        "/api/papers",
        files={"file": ("paper.pdf", make_pdf(), "application/pdf")},
        data={"title": "Escalation test"},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _jobs(client) -> list[Job]:
    async with client.session_factory() as s:  # type: ignore[attr-defined]
        return list((await s.execute(select(Job).order_by(Job.created_at))).scalars())


async def test_weak_parse_enqueues_escalation(client, monkeypatch) -> None:
    monkeypatch.setattr(worker, "get_parser", lambda *a, **kw: StaticParser(WEAK))
    await _upload(client)

    assert await worker.run_once() == "done"

    jobs = await _jobs(client)
    assert len(jobs) == 2
    parent = next(j for j in jobs if not (j.payload_json or {}).get("escalated_from"))
    child = next(j for j in jobs if (j.payload_json or {}).get("escalated_from"))

    assert parent.status == "done"
    assert child.status == "queued", "增强 job 应该排队等下一次 worker 取"
    assert child.payload_json["backend"] == "docling"
    assert child.payload_json["escalated_from"] == parent.id
    assert child.payload_json["code"] == "unanchored_captions"
    assert parent.payload_json["escalated_to"] == child.id
    assert "图号" in parent.payload_json["escalate_reason"]

    # API 上要能看见「为什么升级」——否则用户只看到一个莫名其妙的慢 job
    detail = (await client.get(f"/api/jobs/{child.id}")).json()
    assert detail["stage"] == "docling"
    assert detail["escalate_reason"]


async def test_escalated_job_runs_with_docling_and_does_not_loop(client, monkeypatch) -> None:
    monkeypatch.setattr(worker, "get_parser", lambda *a, **kw: StaticParser(WEAK))
    await _upload(client)
    assert await worker.run_once() == "done"

    seen: dict[str, str] = {}

    def factory(backend, **kwargs):  # noqa: ANN001
        seen["backend"] = backend
        return StaticParser(HEALTHY)  # 增强档的结果是好的

    monkeypatch.setattr(worker, "get_parser", factory)
    assert await worker.run_once() == "done"

    assert seen["backend"] == "docling", "增强 job 必须用 payload 里指定的后端"
    jobs = await _jobs(client)
    assert len(jobs) == 2, "只升一级：增强档解析完不能再排 job，否则会无限繁殖"
    assert all(j.status == "done" for j in jobs)


async def test_reparse_preserves_human_review(client, monkeypatch) -> None:
    """重新解析不能抹掉人工复核结果——用户点过的「定位正确」不是解析产物。"""
    monkeypatch.setattr(worker, "get_parser", lambda *a, **kw: StaticParser(HEALTHY))
    created = await _upload(client)
    assert await worker.run_once() == "done"

    figs = (await client.get(f"/api/papers/{created['paper_id']}/figures")).json()
    assert len(figs) == 1
    figure_id = figs[0]["figure_id"]
    await client.post(
        f"/api/figures/{figure_id}/review",
        json={"verdict": "ok", "note": "人工确认过", "reviewer": "human"},
    )

    # 再解析一次（模拟重新上传 / 按需升级 / 手动重试）
    await _upload(client)
    assert await worker.run_once() == "done"

    figs = (await client.get(f"/api/papers/{created['paper_id']}/figures")).json()
    assert len(figs) == 1
    assert figs[0]["review_status"] == "ok"
    assert figs[0]["review_note"] == "人工确认过"


async def test_reparse_preserves_bbox_override(client, monkeypatch) -> None:
    """人工画的修正框也不能丢——overlay 靠它才显示得对。"""
    monkeypatch.setattr(worker, "get_parser", lambda *a, **kw: StaticParser(HEALTHY))
    created = await _upload(client)
    assert await worker.run_once() == "done"

    figure_id = (await client.get(f"/api/papers/{created['paper_id']}/figures")).json()[0][
        "figure_id"
    ]
    fixed = [11.0, 22.0, 111.0, 72.0]
    await client.post(
        f"/api/figures/{figure_id}/review",
        json={"verdict": "wrong", "bbox": fixed, "reviewer": "human", "note": "框错位"},
    )

    await _upload(client)
    assert await worker.run_once() == "done"

    fig = (await client.get(f"/api/papers/{created['paper_id']}/figures")).json()[0]
    assert fig["review_status"] == "wrong"
    assert fig["bbox_override"] == fixed


async def test_healthy_parse_is_not_escalated(client, monkeypatch) -> None:
    monkeypatch.setattr(worker, "get_parser", lambda *a, **kw: StaticParser(HEALTHY))
    await _upload(client)
    assert await worker.run_once() == "done"
    assert len(await _jobs(client)) == 1


async def test_escalation_can_be_disabled(client, monkeypatch) -> None:
    """关掉开关就退回纯 pdfium，行为要和原来完全一样。"""
    monkeypatch.setattr(worker, "can_escalate", can_escalate)  # 用真实判断
    monkeypatch.setattr(settings, "escalate_backend", "")
    monkeypatch.setattr(worker, "get_parser", lambda *a, **kw: StaticParser(WEAK))
    await _upload(client)
    assert await worker.run_once() == "done"
    assert len(await _jobs(client)) == 1


async def test_tables_are_persisted_and_exposed(client, monkeypatch) -> None:
    """增强档的最大产出是表格。存了却取不出来等于没做，所以这里顺带验证读写两端。"""
    doc = make_doc(captions=("Fig. 1. Long enough caption here",), figures=(("Fig. 1", LONG),))
    doc.tables.append(
        ParsedTable(
            page=2,
            bbox=[0, 0, 10, 10],
            html="<table><tr><td>a</td><td>b</td></tr></table>",
            cells=[],
            caption="Table 1. Results",
            quality="structured",
        )
    )
    monkeypatch.setattr(worker, "get_parser", lambda *a, **kw: StaticParser(doc))
    created = await _upload(client)
    assert await worker.run_once() == "done"

    rows = (await client.get(f"/api/papers/{created['paper_id']}/tables")).json()
    assert len(rows) == 1
    assert rows[0]["page"] == 2
    assert rows[0]["quality"] == "structured"
    assert "<td>a</td>" in rows[0]["html"]
    assert rows[0]["paper_id"] == created["paper_id"]


async def test_reupload_does_not_escalate_twice(client, monkeypatch) -> None:
    """同一份 PDF 再传一次（幂等重解析）不该再排一个 60 秒的增强 job。"""
    monkeypatch.setattr(worker, "get_parser", lambda *a, **kw: StaticParser(WEAK))
    await _upload(client)
    assert await worker.run_once() == "done"  # pdfium → 排队一个 docling
    assert await worker.run_once() == "done"  # docling 跑完
    assert len(await _jobs(client)) == 2

    await _upload(client)  # 相同字节：此时没有 pending job，会新建一个 pdfium job
    assert len(await _jobs(client)) == 3
    assert await worker.run_once() == "done"

    jobs = await _jobs(client)
    assert len(jobs) == 3, "同一个版本已经有增强档结果，不该再排一次"
    assert sum(1 for j in jobs if (j.payload_json or {}).get("backend") == "docling") == 1
