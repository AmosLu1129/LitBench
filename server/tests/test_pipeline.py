# SPDX-License-Identifier: AGPL-3.0-or-later
"""上传 → worker → 检索 的端到端测试。

这条链路是产品的脊椎：``POST /api/papers`` 建 job，worker 消费 job 解析入库，
图能被前端检索到。它此前从未被跑通过——库里所有数据都是 ``seed_dev.py``
绕过 HTTP 直连解析造出来的，所以 job 状态机、worker 的路径解析、缩略图渲染
这三处都只是「看起来对」。

这里的断言刻意对着**用户真实会经历的路径**（HTTP → 队列 → 解析 → 检索），
只在需要制造故障时才伸手改内部状态。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.adapters import ParsedDocument, ParserError
from litbench.config import settings
from litbench.db import get_session
from litbench.main import app
from litbench.models import Base, Figure, Job, Paper, PaperVersion
from litbench.workers import run as worker
from pdf_fixture import make_pdf

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
async def client(tmp_path, monkeypatch):
    """真实依赖：SQLite 测试库 + 临时 storage 目录 + 真实 pdfium 解析器。"""
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setattr(settings, "storage_dir", storage)
    monkeypatch.setattr(settings, "parser_backend", "pdfium")

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    # worker 自己拿的是模块级 get_sessionmaker()，必须指向同一个测试库
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


async def _upload(client, data: bytes | None = None) -> dict:
    r = await client.post(
        "/api/papers",
        files={"file": ("synthetic.pdf", data or make_pdf(), "application/pdf")},
        data={"title": "Synthetic paper"},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_upload_generates_unique_cite_key(client) -> None:
    """手工上传的文献也要有 cite_key（M4 引用锚点）。

    以前只有图有 cite_key，paper.cite_key 为 null，MCP 的 agent 回答只能引用 paper_id。
    规则：arXiv > DOI > p-<paper id 前 8 位>；同一 arXiv 的不同 PDF 不撞唯一约束。
    """
    r1 = await client.post(
        "/api/papers",
        files={
            "file": (
                "a.pdf",
                make_pdf(caption="Fig. 1. First upload on cite key."),
                "application/pdf",
            )
        },
        data={"title": "Cite key paper", "arxiv_id": "2611.00001", "year": "2026"},
    )
    assert r1.status_code == 201, r1.text
    first = r1.json()
    paper = (await client.get(f"/api/papers/{first['paper_id']}")).json()
    assert paper["cite_key"] == "arXiv:2611.00001"

    r2 = await client.post(
        "/api/papers",
        files={
            "file": (
                "b.pdf",
                make_pdf(caption="Fig. 2. Second upload on the same arXiv id."),
                "application/pdf",
            )
        },
        data={"title": "Cite key paper v2", "arxiv_id": "2611.00001"},
    )
    assert r2.status_code == 201, r2.text
    second = r2.json()
    assert second["deduplicated"] is False
    paper2 = (await client.get(f"/api/papers/{second['paper_id']}")).json()
    assert paper2["cite_key"].startswith("arXiv:2611.00001#")

    r3 = await client.post(
        "/api/papers",
        files={
            "file": (
                "c.pdf",
                make_pdf(caption="Fig. 3. Upload without any identifier."),
                "application/pdf",
            )
        },
        data={"title": "No identifier"},
    )
    assert r3.status_code == 201, r3.text
    third = r3.json()
    paper3 = (await client.get(f"/api/papers/{third['paper_id']}")).json()
    assert paper3["cite_key"] == f"p-{third['paper_id'][:8]}"


async def test_upload_parse_search_closed_loop(client) -> None:
    """一条命令走完：上传 → job 排队 → worker 解析 → 图可检索 → 缩略图可显示。"""
    created = await _upload(client)
    assert created["deduplicated"] is False

    # 1. 上传立刻建了 job，但还没人消费
    job = (await client.get(f"/api/jobs/{created['job_id']}")).json()
    assert job["status"] == "queued"
    assert job["attempts"] == 0

    # 2. worker 消费
    assert await worker.run_once() == "done"

    job = (await client.get(f"/api/jobs/{created['job_id']}")).json()
    assert job["status"] == "done"
    assert job["progress"] == 1.0
    assert job["attempts"] == 1
    assert job["error"] is None

    # 3. 文献状态从 new 变成 parsed，并且数得出图
    paper = (await client.get(f"/api/papers/{created['paper_id']}")).json()
    assert paper["status"] == "parsed"
    assert paper["figure_count"] >= 1

    figs = (await client.get(f"/api/papers/{created['paper_id']}/figures")).json()
    assert figs, "解析出 0 张图：caption 锚定生长可能退化"
    fig = figs[0]
    assert fig["label_norm"] == "fig:1"
    assert fig["page"] == 1
    assert len(fig["bbox"]) == 4
    # 缩略图是本条链路最容易漏的一环（worker 曾经不渲染图）
    assert fig["image_url"], "图没有渲染缩略图：worker 的 render_images 没打开"

    img = await client.get(fig["image_url"])
    assert img.status_code == 200
    assert img.headers["content-type"] == "image/png"
    assert img.content.startswith(PNG_MAGIC)

    # 4. 出处检索命中——这是「证据可溯源」的最小承诺
    r = await client.get(
        "/api/figures", params={"paper": created["paper_id"], "label": "Fig. 1"}
    )
    body = r.json()
    assert body["mode"] == "provenance"
    assert body["total"] == 1
    assert body["items"][0]["cite_key"].endswith("-fig1")

    # 5. 库里的路径必须是「相对 storage 根」的：换挂载点不该全 404
    async with client.session_factory() as s:  # type: ignore[attr-defined]
        version = (await s.execute(select(PaperVersion))).scalars().one()
        figure = (await s.execute(select(Figure))).scalars().first()
    assert version.file_path.startswith("pdfs/")
    assert not version.file_path.startswith(("/", "D:", "C:"))
    assert figure.image_path and figure.image_path.startswith("images/")


async def test_reupload_uses_existing_content_addressed_file_when_locked(
    client, monkeypatch
) -> None:
    """重复上传同一 PDF 时，内容寻址的目标文件可能正被查看器/杀毒占用。

    Windows 上 ``os.replace`` 覆盖被占用的文件会抛 PermissionError（曾让重复上传 500）。
    文件已存在且大小一致时应该直接复用，不再写盘。
    """
    first = await _upload(client)
    assert first["deduplicated"] is False

    def locked_replace(self, target):  # noqa: ANN001
        raise PermissionError(13, "目标文件被占用（模拟）")

    monkeypatch.setattr("pathlib.Path.replace", locked_replace)
    second = await _upload(client)
    assert second["deduplicated"] is True
    assert second["sha256"] == first["sha256"]
    assert second["version_id"] == first["version_id"]


async def test_claim_never_hands_out_same_job_twice(client) -> None:
    """两个 worker 同时起来时，同一个 job 只能被领走一次。

    Postgres 靠 FOR UPDATE SKIP LOCKED；SQLite 会静默忽略这个子句，
    所以 claim_job 里还有一次 ``WHERE status='queued'`` 的 CAS 兜底。
    """
    created = await _upload(client)

    async with client.session_factory() as s1, client.session_factory() as s2:  # type: ignore[attr-defined]
        first = await worker.claim_job(s1)
        second = await worker.claim_job(s2)

    assert first is not None and first.id == created["job_id"]
    assert second is None, "同一个 job 被两个 worker 领走了：CAS 没生效"

    async with client.session_factory() as s:  # type: ignore[attr-defined]
        job = await s.get(Job, created["job_id"])
        assert job.status == "running"
        assert job.attempts == 1, "领任务时 attempts 必须只加一次"


async def test_reupload_is_idempotent(client) -> None:
    """同一份 PDF 再传一次：不新增版本，重解析也不产生重复的图。"""
    first = await _upload(client)
    assert await worker.run_once() == "done"
    before = (await client.get(f"/api/papers/{first['paper_id']}")).json()

    second = await _upload(client)
    assert second["deduplicated"] is True
    assert second["paper_id"] == first["paper_id"]
    assert second["job_id"] != first["job_id"], "重复上传要重新排一次解析（幂等，不是跳过）"

    assert await worker.run_once() == "done"
    after = (await client.get(f"/api/papers/{first['paper_id']}")).json()
    assert after["figure_count"] == before["figure_count"]

    async with client.session_factory() as s:  # type: ignore[attr-defined]
        versions = (await s.execute(select(PaperVersion))).scalars().all()
        jobs = (await s.execute(select(Job))).scalars().all()
    assert len(versions) == 1
    assert len(jobs) == 2 and all(j.status == "done" for j in jobs)


async def test_stale_running_job_is_requeued(client) -> None:
    """worker 被 kill 后重启：卡在 running 的 job 会被回收重跑。"""
    created = await _upload(client)

    from datetime import datetime, timedelta, timezone

    async with client.session_factory() as s:  # type: ignore[attr-defined]
        job = await s.get(Job, created["job_id"])
        # 模拟「worker 已经领了任务、正在跑的时候被 kill」：状态 running、attempts 已记账
        job.status = "running"
        job.attempts = 1
        job.started_at = datetime.now(timezone.utc) - timedelta(
            minutes=worker.STALE_RUNNING_MINUTES + 5
        )
        await s.commit()

    # main(once=True) 启动时先 recover_stale，再消费一个 job
    assert await worker.main(poll_interval=0.01, once=True) == 0

    job = (await client.get(f"/api/jobs/{created['job_id']}")).json()
    assert job["status"] == "done"
    assert job["attempts"] == 2, "重跑要体现在 attempts 上，不能假装第一次"


async def test_parse_failure_is_recorded_not_swallowed(client, monkeypatch) -> None:
    """解析炸了要留下可读的失败现场，而不是静默把文献标成已解析。"""

    class BoomParser:
        name = "boom"

        async def parse(self, pdf_path):  # noqa: ANN001
            raise ParserError("模拟解析失败")

    monkeypatch.setattr(worker, "get_parser", lambda *a, **kw: BoomParser())

    created = await _upload(client)
    assert await worker.run_once() == "failed"

    job = (await client.get(f"/api/jobs/{created['job_id']}")).json()
    assert job["status"] == "failed"
    assert "ParserError" in (job["error"] or "")
    assert job["attempts"] == 1

    paper = (await client.get(f"/api/papers/{created['paper_id']}")).json()
    assert paper["status"] == "new", "解析失败不能把文献标成 parsed"


async def test_figure_bbox_is_top_left_origin(client) -> None:
    """pdfium 内部是 PDF 原生左下原点，出口必须翻成左上原点。

    不翻的后果不是"偏一点"：裁剪会镜像到页面的另一半，overlay 也会画到错误位置。
    make_pdf 的矩形在原生坐标 y=520..650（页高 792），左上原点应是 142..272。
    """
    created = await _upload(client)
    assert await worker.run_once() == "done"
    figs = (await client.get(f"/api/papers/{created['paper_id']}/figures")).json()
    assert figs
    bbox = figs[0]["bbox"]
    assert 120 <= bbox[1] <= 165, f"bbox 上边应在 139 附近，实际 {bbox[1]}（是不是没翻原点？）"
    assert 250 <= bbox[3] <= 300, f"bbox 下边应在 275 附近，实际 {bbox[3]}"


async def test_scanned_pdf_is_marked_needs_ocr(client, monkeypatch) -> None:
    """没有文字层的扫描件要显式标记，不能让下游以为「解析成功但内容为空」。"""

    class NoTextParser:
        name = "fake"

        async def parse(self, pdf_path):  # noqa: ANN001
            return ParsedDocument(
                page_count=3,
                engine="fake",
                engine_version="0",
                blocks=[],
                figures=[],
                tables=[],
                text_layer=False,
            )

    monkeypatch.setattr(worker, "get_parser", lambda *a, **kw: NoTextParser())

    created = await _upload(client)
    assert await worker.run_once() == "done"

    paper = (await client.get(f"/api/papers/{created['paper_id']}")).json()
    assert paper["status"] == "needs_ocr"


async def test_attach_pdf_to_metadata_only_paper(client) -> None:
    """Zotero 只读导入的文献只有元数据、没有 PDF：补传 PDF 后应建版本 + 排队解析。"""
    Session = client.session_factory
    async with Session() as s:
        paper = Paper(title="Zotero metadata only", metadata_source="zotero", status="new")
        s.add(paper)
        await s.commit()
        paper_id = paper.id
        other = Paper(title="Another metadata only", metadata_source="zotero", status="new")
        s.add(other)
        await s.commit()
        other_id = other.id

    papers = (await client.get("/api/papers")).json()
    assert next(p for p in papers if p["id"] == paper_id)["has_version"] is False

    data = make_pdf()
    r = await client.post(
        f"/api/papers/{paper_id}/pdf",
        files={"file": ("attach.pdf", data, "application/pdf")},
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["paper_id"] == paper_id
    assert created["deduplicated"] is False
    assert created["job_id"]

    detail = (await client.get(f"/api/papers/{paper_id}")).json()
    assert detail["has_version"] is True
    job = (await client.get(f"/api/jobs/{created['job_id']}")).json()
    assert job["status"] == "queued"
    assert job["kind"] == "parse"

    # 同一文件再传：幂等，不重复建版本
    r = await client.post(
        f"/api/papers/{paper_id}/pdf",
        files={"file": ("attach.pdf", data, "application/pdf")},
    )
    assert r.status_code == 201
    assert r.json()["deduplicated"] is True
    assert r.json()["version_id"] == created["version_id"]

    # 同一 PDF 已属于别的文献 → 409，不替用户合并
    r = await client.post(
        f"/api/papers/{other_id}/pdf",
        files={"file": ("attach.pdf", data, "application/pdf")},
    )
    assert r.status_code == 409
    assert "已经属于" in r.json()["detail"]
