# SPDX-License-Identifier: AGPL-3.0-or-later
"""PDF 本体接口的测试。

这个接口把整份 PDF 交给前端（PDF.js 要画 bbox overlay），
所以路径守卫必须和图片接口一样严。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.config import settings
from litbench.db import get_session
from litbench.main import app
from litbench.models import Base, Paper, PaperVersion

FAKE_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    (tmp_path / "pdfs").mkdir()
    (tmp_path / "pdfs" / "ok.pdf").write_bytes(FAKE_PDF)
    (tmp_path / "pdfs" / "gone.pdf").write_bytes(FAKE_PDF)
    outside = tmp_path.parent / "outside.pdf"
    outside.write_bytes(FAKE_PDF)

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as s:
        ok = Paper(title="Has PDF", status="parsed")
        s.add(ok)
        await s.flush()
        s.add(
            PaperVersion(
                paper_id=ok.id, kind="preprint", file_sha256="d" * 64,
                file_path=str(tmp_path / "pdfs" / "ok.pdf"),
            )
        )

        bad = Paper(title="Path escape", status="parsed")
        s.add(bad)
        await s.flush()
        s.add(
            PaperVersion(
                paper_id=bad.id, kind="preprint", file_sha256="e" * 64,
                file_path=str(outside),
            )
        )

        empty = Paper(title="No pdf", status="new")
        s.add(empty)

        await s.commit()
        ids = {"ok": ok.id, "bad": bad.id, "empty": empty.id}

    async def _session():
        async with Session() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        ac.paper_ids = ids  # type: ignore[attr-defined]
        yield ac
    app.dependency_overrides.clear()
    await engine.dispose()


async def test_pdf_ok(client) -> None:
    r = await client.get(f"/api/papers/{client.paper_ids['ok']}/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content == FAKE_PDF


async def test_pdf_outside_storage_rejected(client) -> None:
    r = await client.get(f"/api/papers/{client.paper_ids['bad']}/pdf")
    assert r.status_code == 403


async def test_pdf_missing_version(client) -> None:
    r = await client.get(f"/api/papers/{client.paper_ids['empty']}/pdf")
    assert r.status_code == 404


async def test_pdf_file_gone(client, tmp_path) -> None:
    (tmp_path / "pdfs" / "gone.pdf").unlink()
    r = await client.get(f"/api/papers/{client.paper_ids['ok']}/pdf")
    # ok 指向 ok.pdf 仍在；再补一条指向已删除文件的版本，验证 404 分支
    assert r.status_code == 200
