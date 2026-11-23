# SPDX-License-Identifier: AGPL-3.0-or-later
"""无文字层 → 排 OCR job 的测试（worker 页级触发）。

任务单要求：``text_layer=False`` 时自动排一个 OCR job，而不是停在 ``needs_ocr``；
OCR 挂了要能降级、不能静默。这里用假的 parser 验证状态机，不真的跑 MinerU。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.adapters import ParsedBlock, ParsedDocument
from litbench.config import settings
from litbench.db import get_session
from litbench.main import app
from litbench.models import Base, Job
from litbench.workers import run as worker
from pdf_fixture import make_pdf


def scanned_doc() -> ParsedDocument:
    """扫描件：没有文字层、没有 blocks。"""
    return ParsedDocument(
        page_count=3,
        engine="pdfium",
        engine_version="test",
        blocks=[],
        figures=[],
        tables=[],
        text_layer=False,
    )


def ocr_doc() -> ParsedDocument:
    """OCR 之后：有文字层、有块。"""
    blocks = [
        ParsedBlock(page=1, bbox=[0, 0, 300, 20], type="text", text="控制系统的频域分析。" * 5),
        ParsedBlock(page=1, bbox=[0, 30, 300, 50], type="caption", text="图 4-2 频率特性的几何表示"),
    ]
    return ParsedDocument(
        page_count=3,
        engine="mineru",
        engine_version="4.2.0",
        blocks=blocks,
        figures=[],
        tables=[],
        text_layer=True,
    )


class StaticParser:
    name = "static"

    def __init__(self, document: ParsedDocument) -> None:
        self.document = document

    async def parse(self, pdf_path):  # noqa: ANN001
        return self.document


@pytest.fixture
async def client(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setattr(settings, "storage_dir", storage)
    monkeypatch.setattr(settings, "parser_backend", "pdfium")
    monkeypatch.setattr(settings, "ocr_backend", "mineru")
    monkeypatch.setattr(worker, "ocr_available", lambda backend: (True, ""))

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
        files={"file": ("scan.pdf", make_pdf(), "application/pdf")},
        data={"title": "Scanned book"},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _jobs(client) -> list[Job]:
    async with client.session_factory() as s:  # type: ignore[attr-defined]
        return list((await s.execute(select(Job).order_by(Job.created_at))).scalars())


async def test_scanned_doc_enqueues_ocr_job(client, monkeypatch) -> None:
    monkeypatch.setattr(worker, "get_parser", lambda *a, **kw: StaticParser(scanned_doc()))
    created = await _upload(client)

    assert await worker.run_once() == "done"

    jobs = await _jobs(client)
    assert len(jobs) == 2
    parent = next(j for j in jobs if not (j.payload_json or {}).get("ocr_from"))
    child = next(j for j in jobs if (j.payload_json or {}).get("ocr_from"))
    assert child.status == "queued"
    assert child.payload_json["backend"] == "mineru"
    assert child.payload_json["ocr"] is True
    assert child.payload_json["ocr_from"] == parent.id
    assert parent.payload_json["ocr_to"] == child.id

    # 还没 OCR，文献状态必须是 needs_ocr（不能假装 parsed）
    paper = (await client.get(f"/api/papers/{created['paper_id']}")).json()
    assert paper["status"] == "needs_ocr"


async def test_ocr_job_runs_and_does_not_loop(client, monkeypatch) -> None:
    monkeypatch.setattr(worker, "get_parser", lambda *a, **kw: StaticParser(scanned_doc()))
    created = await _upload(client)
    assert await worker.run_once() == "done"

    seen: dict[str, object] = {}

    def factory(backend, **kwargs):  # noqa: ANN001
        seen["backend"] = backend
        seen["ocr"] = kwargs.get("ocr")
        return StaticParser(ocr_doc())

    monkeypatch.setattr(worker, "get_parser", factory)
    assert await worker.run_once() == "done"

    assert seen["backend"] == "mineru", "OCR job 必须用 payload 指定的后端"
    assert seen["ocr"] is True, "OCR job 必须把 ocr=True 传给 parser"
    paper = (await client.get(f"/api/papers/{created['paper_id']}")).json()
    assert paper["status"] == "parsed"

    jobs = await _jobs(client)
    assert len(jobs) == 2, "OCR 结果不能再排 OCR，也不能再触发按需升级"
    assert all(j.status == "done" for j in jobs)


async def test_ocr_disabled_leaves_needs_ocr(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "ocr_backend", "")
    monkeypatch.setattr(worker, "get_parser", lambda *a, **kw: StaticParser(scanned_doc()))
    created = await _upload(client)
    assert await worker.run_once() == "done"

    assert len(await _jobs(client)) == 1
    paper = (await client.get(f"/api/papers/{created['paper_id']}")).json()
    assert paper["status"] == "needs_ocr"


async def test_ocr_backend_unavailable_is_not_silent(client, monkeypatch) -> None:
    """后端不可用时不排注定失败的 job；文献停在 needs_ocr，日志里有原因。"""
    monkeypatch.setattr(
        worker, "ocr_available", lambda backend: (False, "mineru CLI 不存在")
    )
    monkeypatch.setattr(worker, "get_parser", lambda *a, **kw: StaticParser(scanned_doc()))
    created = await _upload(client)
    assert await worker.run_once() == "done"

    assert len(await _jobs(client)) == 1
    paper = (await client.get(f"/api/papers/{created['paper_id']}")).json()
    assert paper["status"] == "needs_ocr"
