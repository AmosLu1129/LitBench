# SPDX-License-Identifier: AGPL-3.0-or-later
"""Word/PDF 导出（M8，2026-11-16）：claim → pandoc → docx/pdf。

守三条：

1. 正文只来自 claim 卡片，引用键必须能在文献库解析（解析不了 422，不静默丢引用）；
2. 缺 pandoc/typst 时导出接口 503，其他导出（LaTeX/Markdown）不受影响；
3. 真渲染测试在缺工具的环境自动跳过，CI 不会因外部二进制缺失变红。
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime

import pypdfium2 as pdfium
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench import csl, doc_export
from litbench.db import get_session
from litbench.doc_export import (
    DocExportError,
    _rewrite_citations,
    build_draft_markdown,
    find_typst,
)
from litbench.main import app
from litbench.models import Base, Claim, Paper, PaperVersion

PANDOC = csl.find_pandoc()
TYPST = find_typst()
requires_pandoc = pytest.mark.skipif(PANDOC is None, reason="本机没有 pandoc")
requires_typst = pytest.mark.skipif(
    TYPST is None or PANDOC is None, reason="本机没有 typst 或 pandoc"
)


@pytest.fixture
async def client(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'docs.db').as_posix()}", poolclass=NullPool
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


def _journal() -> Paper:
    return Paper(
        id="a" * 32,
        title="Experimental investigation of heat transfer enhancement in porous media",
        title_norm="experimental investigation heat transfer enhancement porous media",
        doi="10.1016/j.ijheatmasstransfer.2026.112128",
        cite_key="doi:10.1016/j.ijheatmasstransfer.2026.112128",
        year=2026,
        venue="International Journal of Heat and Mass Transfer",
        volume="231",
        issue="3",
        pages="112–128",
        authors_json=[
            {"firstName": "Wei", "lastName": "Zhang"},
            {"firstName": "Ming", "lastName": "Li"},
        ],
        entry_type="journal",
    )


def _thesis() -> Paper:
    return Paper(
        id="b" * 32,
        title="多孔介质内流动与传热的格子Boltzmann模拟",
        title_norm="多孔介质内流动与传热的格子boltzmann模拟",
        cite_key="arXiv:2603.09607",
        year=2025,
        authors_json=[{"name": "王五"}],
        entry_type="thesis",
        thesis_school="清华大学",
        thesis_degree="博士学位论文",
        thesis_place="北京",
    )


def _claims() -> list[Claim]:
    return [
        Claim(
            id="c" * 32,
            paper_id="a" * 32,
            text="多孔介质强化传热是重要的研究方向。",
            section="绪论",
            status="approved",
            order_idx=0,
            evidence_json=[],
        ),
        Claim(
            id="d" * 32,
            paper_id="b" * 32,
            text="格子 Boltzmann 方法可以模拟流动与传热。",
            section="方法",
            status="approved",
            order_idx=0,
            evidence_json=[],
        ),
        Claim(
            id="e" * 32,
            paper_id="a" * 32,
            text="实验结果显示传热系数提升。",
            section="实验与结果",
            status="draft",
            order_idx=0,
            evidence_json=[],
        ),
    ]


async def _seed(client, *, with_claims: bool = True) -> None:
    Session = client.session_factory
    async with Session() as s:
        papers = [_journal(), _thesis()]
        s.add_all(papers)
        await s.flush()
        s.add(
            PaperVersion(
                paper_id=papers[0].id,
                kind="uploaded",
                file_sha256="f" * 64,
                file_path="pdfs/f.pdf",
                page_count=1,
                parser="pdfium",
                parsed_at=datetime.now(UTC),
            )
        )
        if with_claims:
            s.add_all(_claims())
        await s.commit()


# ---------------------------------------------------------------------------
# 纯函数：Markdown / 引用改写
# ---------------------------------------------------------------------------
def test_build_draft_markdown_groups_sections_and_citations() -> None:
    claim = _claims()[0]
    markdown, papers = build_draft_markdown(
        [(claim, _journal())], title="测试标题", section_order=["绪论", "方法"]
    )
    assert markdown.startswith('---\ntitle: "测试标题"\nnocite: "@*"\n---')
    assert "## 绪论" in markdown
    assert f"多孔介质强化传热是重要的研究方向。 [@{'a' * 32}]" in markdown
    assert set(papers) == {"a" * 32}
    # 形如 None / 空段落这类占位不能漏进去
    assert "None" not in markdown


def test_rewrite_citations_maps_cite_key_and_rejects_unknown() -> None:
    papers = {"a" * 32: _journal()}
    rewritten, cited = _rewrite_citations(
        "正文 [@doi:10.1016/j.ijheatmasstransfer.2026.112128]", papers
    )
    assert f"[@{'a' * 32}]" in rewritten
    assert cited == {"a" * 32}
    with pytest.raises(DocExportError) as excinfo:
        _rewrite_citations("正文 [@no-such-key]", papers)
    assert "no-such-key" in str(excinfo.value)


def test_rewrite_citations_handles_latex_and_multi_keys() -> None:
    papers = {"a" * 32: _journal(), "b" * 32: _thesis()}
    markdown = (
        r"前人工作 \cite{doi:10.1016/j.ijheatmasstransfer.2026.112128} 与 "
        r"\citep{doi:10.1016/j.ijheatmasstransfer.2026.112128, kev-b}；"
        r"见 [@doi:10.1016/j.ijheatmasstransfer.2026.112128, p. 3; @kev-b]。"
    )
    # b 的 cite_key 是 arXiv:2603.09607（_thesis 没有 cite_key，用兜底 key）
    b_key = papers["b" * 32].cite_key or f"litbench-{('b' * 32)[:8]}"
    markdown = markdown.replace("kev-b", b_key)
    rewritten, cited = _rewrite_citations(markdown, papers)
    assert "\\cite" not in rewritten
    assert f"[@{'a' * 32}]" in rewritten
    assert f"[@{'a' * 32}; @{'b' * 32}]" in rewritten
    assert f"[@{'a' * 32}, p. 3; @{'b' * 32}]" in rewritten
    assert cited == {"a" * 32, "b" * 32}


# ---------------------------------------------------------------------------
# 能力探测 / HTTP 契约
# ---------------------------------------------------------------------------
async def test_capabilities_endpoint(client) -> None:
    response = await client.get("/api/writing/export/capabilities")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["pandoc"] is (PANDOC is not None)
    assert data["docx"] is (PANDOC is not None)
    assert data["pdf"] is (PANDOC is not None and TYPST is not None)
    assert isinstance(data["note"], str)


def test_find_typst_prefers_bundled_over_path(tmp_path, monkeypatch) -> None:
    import os

    executable = "typst.exe" if os.name == "nt" else "typst"
    bundled = tmp_path / "tools" / "typst" / "0.15.1" / executable
    bundled.parent.mkdir(parents=True)
    bundled.write_text("")
    monkeypatch.setattr(doc_export.settings, "storage_dir", tmp_path)
    monkeypatch.setattr("litbench.toolchain._PROJECT_ROOT", tmp_path / "no-project-tools")
    monkeypatch.setattr("litbench.toolchain.shutil.which", lambda name: "/usr/bin/old-typst")
    assert doc_export.find_typst() == str(bundled)


async def test_export_without_claims_422(client) -> None:
    await _seed(client, with_claims=False)
    response = await client.post(
        "/api/writing/export/document", json={"format": "docx", "title": "空稿"}
    )
    assert response.status_code == 422
    assert "没有可导出的 claim" in response.json()["detail"]


async def test_export_without_pandoc_503(client, monkeypatch) -> None:
    await _seed(client)
    monkeypatch.setattr(csl, "find_pandoc", lambda: None)
    response = await client.post(
        "/api/writing/export/document", json={"format": "docx"}
    )
    assert response.status_code == 503
    assert "pandoc" in response.json()["detail"]


async def test_export_engine_failure_502(client, monkeypatch) -> None:
    """pandoc 已启动但渲染失败 → 502（不是用户输入错误的 422）。"""
    await _seed(client)

    async def boom(*args, **kwargs):
        raise doc_export.DocExportFailed("pandoc 渲染失败（退出码 1）：bad markdown")

    monkeypatch.setattr("litbench.routers.writing.export_document", boom)
    response = await client.post(
        "/api/writing/export/document", json={"format": "docx"}
    )
    assert response.status_code == 502
    assert "pandoc" in response.json()["detail"]


async def test_export_claim_limit_422(client, monkeypatch) -> None:
    """超过 500 张卡片时明确 422，不静默截断。"""

    async def too_many(*args, **kwargs):
        return [(None, None)] * 501

    monkeypatch.setattr(doc_export, "_load_export_claims", too_many)
    response = await client.post(
        "/api/writing/export/document", json={"format": "docx"}
    )
    assert response.status_code == 422
    assert "500" in response.json()["detail"]


async def test_export_pdf_without_typst_503(client, monkeypatch) -> None:
    await _seed(client)
    monkeypatch.setattr(doc_export, "find_typst", lambda: None)
    response = await client.post(
        "/api/writing/export/document", json={"format": "pdf"}
    )
    assert response.status_code == 503
    assert "typst" in response.json()["detail"]


async def test_export_unknown_citation_422(client) -> None:
    Session = client.session_factory
    async with Session() as s:
        s.add_all([_journal(), _thesis()])
        await s.flush()
        s.add(
            Claim(
                id="f" * 32,
                paper_id="a" * 32,
                text="这条手写了引用[@no-such-key]。",
                section="绪论",
                status="approved",
                evidence_json=[],
            )
        )
        await s.commit()
    response = await client.post(
        "/api/writing/export/document", json={"format": "docx"}
    )
    assert response.status_code == 422
    assert "no-such-key" in response.json()["detail"]


async def test_export_unsupported_csl_style_422(client) -> None:
    await _seed(client)
    response = await client.post(
        "/api/writing/export/document",
        json={"format": "docx", "csl_style": "mla9"},
    )
    assert response.status_code == 422
    assert "CSL 样式" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 真渲染
# ---------------------------------------------------------------------------
@requires_pandoc
async def test_export_docx_endpoint(client) -> None:
    await _seed(client)
    response = await client.post(
        "/api/writing/export/document",
        json={"format": "docx", "title": "多孔介质强化传热研究进展"},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(doc_export.DOCX_MIME)
    assert response.headers["x-litbench-claims"] == "3"
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    content = response.content
    assert content.startswith(b"PK\x03\x04")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert "多孔介质强化传热" in xml
    assert "绪论" in xml and "方法" in xml
    assert "ZHANG W" in xml  # GB/T 参考文献条目进了 Word
    assert "王五" in xml


@requires_pandoc
async def test_export_docx_rewrites_latex_citations(client) -> None:
    """claim 里按项目习惯写 \\cite{key} 时，Word 里不能静默丢引用。"""
    Session = client.session_factory
    async with Session() as s:
        s.add_all([_journal(), _thesis()])
        await s.flush()
        s.add(
            Claim(
                id="a1" + "0" * 30,
                paper_id="a" * 32,
                text="前人工作\\cite{doi:10.1016/j.ijheatmasstransfer.2026.112128} 已证明。",
                section="绪论",
                status="approved",
                evidence_json=[],
            )
        )
        await s.commit()
    response = await client.post(
        "/api/writing/export/document", json={"format": "docx"}
    )
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert "\\cite" not in xml
    assert "ZHANG W" in xml  # 参考文献条目在
    assert "已证明" in xml


@requires_pandoc
async def test_export_docx_cites_other_visible_paper(client) -> None:
    """正文手写引用另一篇可见文献：即使它不挂 claim，也要解析并进参考文献表。"""
    Session = client.session_factory
    async with Session() as s:
        s.add_all([_journal(), _thesis()])
        await s.flush()
        s.add(
            Claim(
                id="a2" + "0" * 30,
                paper_id="a" * 32,
                text=f"前人工作 [@{'b' * 32}] 也观察到了。",
                section="绪论",
                status="approved",
                evidence_json=[],
            )
        )
        await s.commit()
    response = await client.post(
        "/api/writing/export/document", json={"format": "docx"}
    )
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert "多孔介质内流动与传热的格子Boltzmann模拟" in xml


@requires_pandoc
async def test_export_docx_paper_ids_include_uncited_reference(client) -> None:
    """只勾了参考文献范围、正文没引用的文献，也要靠 nocite 进参考文献表。"""
    await _seed(client)
    response = await client.post(
        "/api/writing/export/document",
        json={"format": "docx", "paper_ids": ["a" * 32, "b" * 32]},
    )
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert "多孔介质内流动与传热的格子Boltzmann模拟" in xml


@requires_pandoc
async def test_export_sandbox_blocks_local_image(tmp_path, client) -> None:
    """claim 文本里的本地图片引用不能让 pandoc 把服务器文件嵌进 docx。"""
    image = tmp_path / "secret.png"
    image.write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 40
    )
    Session = client.session_factory
    async with Session() as s:
        s.add_all([_journal(), _thesis()])
        await s.flush()
        s.add(
            Claim(
                id="a3" + "0" * 30,
                paper_id="a" * 32,
                text=f"看这张图 ![]({image.as_posix()})。",
                section="方法",
                status="approved",
                evidence_json=[],
            )
        )
        await s.commit()
    response = await client.post(
        "/api/writing/export/document", json={"format": "docx"}
    )
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert not [name for name in archive.namelist() if name.startswith("word/media/")]


@requires_typst
async def test_export_pdf_endpoint(client) -> None:
    await _seed(client)
    response = await client.post(
        "/api/writing/export/document",
        json={"format": "pdf", "title": "多孔介质强化传热研究进展"},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(doc_export.PDF_MIME)
    content = response.content
    assert content.startswith(b"%PDF")
    assert len(content) > 5_000
    document = pdfium.PdfDocument(content)
    text = "".join(page.get_textpage().get_text_range() for page in document)
    assert "多孔介质强化传热" in text
    assert "ZHANG W" in text
