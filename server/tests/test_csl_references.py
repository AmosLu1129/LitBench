# SPDX-License-Identifier: AGPL-3.0-or-later
"""CSL 引用引擎（M8，2026-11-16）：CSL JSON 映射、pandoc 渲染、API 契约。

守三条：

1. **只搬运已有字段**，不补不猜：缺字段不会变成 ``None`` / 空括号；
2. pandoc 缺失时 CSL 引擎返回 503，13 种手写模板照常（模板行为不能回归）；
3. 真渲染测试在没装 pandoc 的环境自动跳过，CI 不会因外部二进制缺失变红。

真渲染用 ``pandoc --citeproc``（与 Zotero/Word 同一套 CSL 引擎）；不引入
citeproc-py 的原因见 ``litbench/csl.py`` 模块注释与 ``docs/15`` §12.42。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench import csl
from litbench.config import settings
from litbench.csl import (
    CSL_STYLES,
    CslError,
    _CslHtmlParser,
    build_csl_item,
    csl_engine_status,
    csl_style_keys,
    export_csl_json,
    find_pandoc,
    has_csl_style,
    render_bibliography,
)
from litbench.db import get_session
from litbench.main import app
from litbench.models import Base, Paper, PaperVersion

PANDOC = find_pandoc()
requires_pandoc = pytest.mark.skipif(
    PANDOC is None, reason="本机没有 pandoc，跳过真实 CSL 渲染"
)


# ---------------------------------------------------------------------------
# 夹具 / 造数据
# ---------------------------------------------------------------------------
@pytest.fixture
async def client(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'csl.db').as_posix()}", poolclass=NullPool
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
        title="Slip-patterned microchannels for enhanced heat transport",
        title_norm="slip-patterned microchannels",
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


def _cn_journal() -> Paper:
    """中文期刊、没有 DOI：GB/T 官方样式的 [J] 标识最容易在这种条目上丢。"""
    return Paper(
        id="b" * 32,
        title="膜状凝结是工程设计的依据",
        title_norm="膜状凝结是工程设计的依据",
        year=2025,
        venue="工程热物理学报",
        volume="46",
        issue="2",
        pages="321–328",
        authors_json=[{"name": "王磊"}, {"name": "张三"}],
        entry_type="journal",
    )


def _thesis() -> Paper:
    return Paper(
        id="c" * 32,
        title="多孔介质内流动与传热的格子Boltzmann模拟",
        title_norm="多孔介质内流动与传热的格子boltzmann模拟",
        year=2025,
        authors_json=[{"name": "王五"}],
        entry_type="thesis",
        thesis_school="清华大学",
        thesis_degree="博士学位论文",
        thesis_place="北京",
    )


def _preprint() -> Paper:
    return Paper(
        id="d" * 32,
        title="Swirl flow in microchannels",
        title_norm="swirl flow in microchannels",
        arxiv_id="2603.09607",
        cite_key="arXiv:2603.09607",
        year=2026,
        authors_json=[{"name": "Wang Lei"}],
        entry_type="preprint",
    )


async def _seed(client) -> dict:
    Session = client.session_factory
    async with Session() as s:
        papers = [_journal(), _cn_journal(), _thesis(), _preprint()]
        s.add_all(papers)
        await s.flush()
        s.add(
            PaperVersion(
                paper_id=_journal().id,
                kind="uploaded",
                file_sha256="e" * 64,
                file_path="pdfs/e.pdf",
                page_count=1,
                parser="pdfium",
                parsed_at=datetime.now(UTC),
            )
        )
        await s.commit()
        return {
            "journal": papers[0].id,
            "cn_journal": papers[1].id,
            "thesis": papers[2].id,
            "preprint": papers[3].id,
        }


# ---------------------------------------------------------------------------
# 纯函数：字段映射 / CSL JSON
# ---------------------------------------------------------------------------
def test_style_registry_files_exist() -> None:
    assert csl_style_keys() == ["gbt7714", "apa7", "ieee"]
    assert {style.key for style in CSL_STYLES} == {"gbt7714", "apa7", "ieee"}
    style_dir = Path(csl.__file__).with_name("csl_styles")
    for style in CSL_STYLES:
        assert (style_dir / style.filename).is_file(), style.filename
    assert has_csl_style("gbt7714") and not has_csl_style("mla9")


def test_build_csl_item_maps_journal_fields() -> None:
    item = build_csl_item(_journal())
    assert item["id"] == "a" * 32
    assert item["type"] == "article-journal"
    assert item["title"].startswith("Slip-patterned")
    assert item["author"] == [
        {"family": "Zhang", "given": "Wei"},
        {"family": "Li", "given": "Ming"},
    ]
    assert item["container-title"] == "International Journal of Heat and Mass Transfer"
    assert (item["volume"], item["issue"], item["page"]) == ("231", "3", "112–128")
    assert item["issued"] == {"date-parts": [[2026]]}
    assert item["DOI"] == "10.1016/j.ijheatmasstransfer.2026.112128"
    assert item["language"] == "en-US"
    assert "None" not in str(item)


def test_build_csl_item_keeps_chinese_names_and_thesis_fields() -> None:
    item = build_csl_item(_thesis())
    assert item["type"] == "thesis"
    # 中文姓名不拆 family/given，用 literal 原样输出
    assert item["author"] == [{"literal": "王五"}]
    assert item["publisher"] == "清华大学"
    assert item["publisher-place"] == "北京"
    assert item["genre"] == "博士学位论文"
    assert item["language"] == "zh-CN"


def test_build_csl_item_preprint_uses_publisher_for_arxiv() -> None:
    item = build_csl_item(_preprint())
    assert item["type"] == "article"
    assert item["genre"] == "Preprint"
    assert item["publisher"] == "arXiv"
    assert item["number"] == "2603.09607"
    assert item["URL"] == "https://arxiv.org/abs/2603.09607"


def test_build_csl_item_bare_paper_has_no_placeholder_fields() -> None:
    bare = SimpleNamespace(id="e" * 32, title="Only a title", authors_json=[], year=None)
    item = build_csl_item(bare)
    assert item == {"id": "e" * 32, "type": "article-journal", "title": "Only a title",
                    "language": "en-US"}


def test_export_csl_json_keeps_input_order() -> None:
    papers = [_preprint(), _journal(), _thesis()]
    ids = [item["id"] for item in export_csl_json(papers)]
    assert ids == ["d" * 32, "a" * 32, "c" * 32]


def test_html_parser_extracts_entries_and_ids() -> None:
    html = (
        '<div id="refs" class="references csl-bib-body">'
        '<div id="ref-abc" class="csl-entry">'
        '<div class="csl-left-margin">[1] </div>'
        '<div class="csl-right-inline">Zhang, W. &amp; Li, M. <em>Journal</em>.</div>'
        "</div>"
        '<div id="ref-def" class="csl-entry">Adam&#8217;s entry</div>'
        "</div>"
    )
    parser = _CslHtmlParser()
    parser.feed(html)
    parser.close()
    assert parser.entries == [
        ("ref-abc", "[1] Zhang, W. & Li, M. Journal."),
        ("ref-def", "Adam’s entry"),
    ]


# ---------------------------------------------------------------------------
# pandoc / CSL 引擎状态
# ---------------------------------------------------------------------------
def test_engine_status_reports_missing_pandoc(monkeypatch) -> None:
    monkeypatch.setattr(csl, "find_pandoc", lambda: None)
    available, note = csl_engine_status()
    assert available is False
    assert "pandoc" in note


def test_engine_status_reports_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "csl_enabled", False)
    available, note = csl_engine_status()
    assert available is False
    assert "关闭" in note


def test_find_pandoc_prefers_bundled_over_path(tmp_path, monkeypatch) -> None:
    """PATH 上有旧 pandoc 时，装了便携版应当优先用便携版（否则 --sandbox/CSL 行为不一致）。"""
    import os

    executable = "pandoc.exe" if os.name == "nt" else "pandoc"
    monkeypatch.setattr("litbench.toolchain._PROJECT_ROOT", tmp_path / "no-project-tools")
    monkeypatch.setattr("litbench.toolchain.shutil.which", lambda name: "/usr/bin/old-pandoc")

    for label, rel in (
        ("flat", ("tools", "pandoc", "3.14", executable)),
        ("posix", ("tools", "pandoc", "3.14", "bin", executable)),
    ):
        storage = tmp_path / label
        bundled = storage.joinpath(*rel)
        bundled.parent.mkdir(parents=True)
        bundled.write_text("")
        monkeypatch.setattr(settings, "storage_dir", storage)
        assert csl.find_pandoc() == str(bundled)


@requires_pandoc
def test_render_gbt7714_keeps_type_markers() -> None:
    """citeproc-py 会吞掉的 [J]/[D] 标识，pandoc 必须保留（选型的核心理由）。"""
    papers = [_journal(), _cn_journal(), _thesis()]
    result = render_bibliography(papers, "gbt7714")
    assert result["engine"] == "csl"
    assert result["paper_count"] == 3
    entries = [item["entry"] for item in result["items"]]
    assert "[J/OL]" in entries[0]  # 有 DOI → 载体标识 OL
    assert "[J]" in entries[1]  # 无 DOI 也不能丢 [J]
    assert "[D]" in entries[2]
    assert result["content"].count("\n") == 2
    assert [item["paper_id"] for item in result["items"]] == [p.id for p in papers]


@requires_pandoc
def test_render_apa_sorts_and_maps_ids() -> None:
    adams = SimpleNamespace(
        id="f" * 32,
        title="Earlier work",
        authors_json=[{"firstName": "John", "lastName": "Adams"}],
        year=2020,
        venue="Journal of Testing",
        volume="1",
        issue="1",
        pages="1–9",
        doi=None,
        arxiv_id=None,
        entry_type="journal",
    )
    zhang = _journal()
    result = render_bibliography([zhang, adams], "apa7")
    assert [item["paper_id"] for item in result["items"]] == [adams.id, zhang.id]
    assert result["items"][0]["entry"].startswith("Adams, J.")


@requires_pandoc
def test_render_unknown_style_raises() -> None:
    with pytest.raises(CslError):
        render_bibliography([_journal()], "mla9")


def test_render_without_pandoc_raises_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(csl, "find_pandoc", lambda: None)
    with pytest.raises(csl.CslUnavailable):
        render_bibliography([_journal()], "gbt7714")


# ---------------------------------------------------------------------------
# HTTP 契约
# ---------------------------------------------------------------------------
async def test_styles_expose_csl_availability(client) -> None:
    response = await client.get("/api/references/styles")
    assert response.status_code == 200, response.text
    data = response.json()
    by_key = {row["key"]: row for row in data["styles"]}
    assert by_key["gbt7714"]["csl_available"] is True
    assert by_key["mla9"]["csl_available"] is False
    assert isinstance(data["csl_engine_available"], bool)
    assert data["csl_note"]


async def test_template_engine_is_still_default(client) -> None:
    seed = await _seed(client)
    response = await client.post(
        "/api/references/format",
        json={"paper_ids": [seed["journal"]], "style": "gbt7714"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["engine"] == "template"
    assert data["items"][0]["entry"].startswith("[1] ")


async def test_csl_engine_gbt7714_endpoint(client) -> None:
    if PANDOC is None:
        pytest.skip("本机没有 pandoc，跳过真实 CSL 渲染")
    seed = await _seed(client)
    response = await client.post(
        "/api/references/format",
        json={
            "paper_ids": [seed["journal"], seed["cn_journal"], seed["thesis"]],
            "style": "gbt7714",
            "engine": "csl",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["engine"] == "csl"
    assert data["paper_count"] == 3
    assert "[J/OL]" in data["items"][0]["entry"]
    assert "[J]" in data["items"][1]["entry"]
    assert "[D]" in data["items"][2]["entry"]
    assert "note" in data
    assert "None" not in data["content"]


async def test_csl_engine_unsupported_style(client) -> None:
    seed = await _seed(client)
    response = await client.post(
        "/api/references/format",
        json={"paper_ids": [seed["journal"]], "style": "mla9", "engine": "csl"},
    )
    assert response.status_code == 422
    assert "CSL 样式" in response.json()["detail"]


async def test_csl_engine_without_pandoc_returns_503(client, monkeypatch) -> None:
    seed = await _seed(client)
    monkeypatch.setattr(csl, "find_pandoc", lambda: None)
    response = await client.post(
        "/api/references/format",
        json={"paper_ids": [seed["journal"]], "style": "gbt7714", "engine": "csl"},
    )
    assert response.status_code == 503
    assert "pandoc" in response.json()["detail"]


async def test_csl_engine_disabled_returns_503(client, monkeypatch) -> None:
    seed = await _seed(client)
    monkeypatch.setattr(settings, "csl_enabled", False)
    response = await client.post(
        "/api/references/format",
        json={"paper_ids": [seed["journal"]], "style": "gbt7714", "engine": "csl"},
    )
    assert response.status_code == 503
    assert "关闭" in response.json()["detail"]


async def test_csl_json_export(client) -> None:
    seed = await _seed(client)
    response = await client.post(
        "/api/references/csl-json",
        json={"paper_ids": [seed["thesis"], seed["journal"], seed["thesis"]]},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["count"] == 2
    assert [item["id"] for item in data["items"]] == [seed["thesis"], seed["journal"]]
    thesis = data["items"][0]
    assert thesis["type"] == "thesis"
    assert thesis["publisher"] == "清华大学"
    assert "None" not in str(data)


async def test_csl_json_export_permissions_and_errors(client) -> None:
    await _seed(client)
    missing = await client.post(
        "/api/references/csl-json", json={"paper_ids": ["0" * 32]}
    )
    assert missing.status_code == 404
    empty = await client.post("/api/references/csl-json", json={"paper_ids": []})
    assert empty.status_code == 422
