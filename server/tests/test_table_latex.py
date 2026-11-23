# SPDX-License-Identifier: AGPL-3.0-or-later
"""表格 → LaTeX（M9.3a/M9.3c，2026-11-15）：合并单元格 / 转义 / 宽表换行 / API 语义。

守五条：

1. ``colspan`` → ``\\multicolumn``、``rowspan`` → ``\\multirow``，且 rowspan 后续行
   的列位要留空，不能让后面的单元格错位；
2. 单元格文本按 LaTeX 规则转义（``& % # _ { }``），不把用户表格弄坏；
3. 表题优先用库里的 caption；没有表号时从「Table 3 ...」推导显示 label，不改库；
4. 没有结构化 HTML / 没有行列 → 422，不硬造空表；跨文献 table_id → 404；
5. 宽表自动把 ``l`` 列换成 ``p{…\\linewidth}``（M9.3c），``width=plain`` 可强制旧行为。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.db import get_session
from litbench.main import app
from litbench.models import Base, Paper, PaperVersion, TableBlock
from litbench.table_latex import TableLatexError, table_html_to_latex


# ---------------------------------------------------------------------------
# 纯转换
# ---------------------------------------------------------------------------
def test_simple_table_caption_and_escaping() -> None:
    html = (
        "<table><caption>Table 2 Results</caption>"
        "<tr><th>A</th><th>B</th></tr>"
        "<tr><td>1 &amp; 2</td><td>50%_x</td></tr></table>"
    )
    payload = table_html_to_latex(html, caption="Table 2 Results", table_id="t" * 32)
    assert payload.columns == 2
    assert payload.rows == 2
    assert payload.label == "Table 2"
    assert payload.label_key == "tab:2"
    assert payload.caption == "Results"
    assert r"\begin{tabular}{ll}" in payload.tabular
    assert r"1 \& 2" in payload.tabular
    assert r"50\%\_x" in payload.tabular
    assert r"\begin{table}[htbp]" in payload.code
    assert r"\caption{Results}" in payload.code
    assert r"\label{tab:2}" in payload.code


def test_colspan_and_rowspan() -> None:
    html = (
        "<table>"
        "<tr><th rowspan='2'>Case</th><th colspan='2'>Value</th></tr>"
        "<tr><th>mean</th><th>std</th></tr>"
        "<tr><td>L260</td><td>1.3</td><td>0.1</td></tr>"
        "</table>"
    )
    payload = table_html_to_latex(html, caption="", label_raw="表 3-1", label_norm="tab:3-1")
    assert payload.columns == 3
    assert r"\multirow{2}{*}{Case}" in payload.tabular
    assert r"\multicolumn{2}{l}{Value}" in payload.tabular
    # rowspan 的第二行要先留一个空单元格，再放 mean/std
    second_row = [line for line in payload.tabular.splitlines() if "mean" in line][0]
    assert second_row.startswith(" & ")
    assert any("multirow" in warning for warning in payload.warnings)
    assert payload.label_key == "tab:3-1"


def test_rowspan_without_colspan_keeps_columns() -> None:
    html = (
        "<table><tr><td rowspan='2'>X</td><td>a</td></tr>"
        "<tr><td>b</td></tr></table>"
    )
    payload = table_html_to_latex(html, table_id="t" * 32)
    lines = payload.tabular.splitlines()
    a_row = next(line for line in lines if "X" in line)
    b_row = next(line for line in lines if line.startswith(" & b"))
    assert r"\multirow{2}{*}{X}" in a_row
    assert a_row.endswith(r"\\")
    # 第二行是「空 & b」
    assert b_row.startswith(" & b")
    assert payload.columns == 2


def test_unicode_math_becomes_math_commands() -> None:
    payload = table_html_to_latex(
        "<table><tr><td>ℓ + 𝜃</td></tr></table>", table_id="t" * 32
    )
    assert r"$\ell$ + $\theta$" in payload.tabular


def test_missing_caption_does_not_fabricate() -> None:
    payload = table_html_to_latex(
        "<table><tr><td>1</td></tr></table>", table_id="t" * 32
    )
    assert r"\caption{" not in payload.code
    assert any("没有表题" in warning for warning in payload.warnings)


def test_missing_html_raises() -> None:
    with pytest.raises(TableLatexError):
        table_html_to_latex(None)
    with pytest.raises(TableLatexError):
        table_html_to_latex("<div>not a table</div>")


# ---------------------------------------------------------------------------
# M9.3c：宽表自动换行
# ---------------------------------------------------------------------------
LONG_TEXT = "The quick brown fox jumps over the lazy dog and keeps running to the end"


def _wide_html() -> str:
    return (
        "<table>"
        "<tr><th>Method</th><th>Description</th><th>Score</th></tr>"
        f"<tr><td>Ours</td><td>{LONG_TEXT}</td><td>0.91</td></tr>"
        "<tr><td>Baseline</td><td>short note</td><td>0.80</td></tr>"
        "</table>"
    )


def test_wide_table_wraps_into_p_columns() -> None:
    payload = table_html_to_latex(_wide_html(), table_id="t" * 32)
    assert payload.wrapped is True
    assert payload.column_spec.startswith("@{") and payload.column_spec.endswith("@{}")
    assert "p{" in payload.column_spec and r"\linewidth" in payload.column_spec
    assert r"\begin{tabular}{@{}p{" in payload.tabular
    assert any("自动换行" in warning for warning in payload.warnings)
    # 列宽比例之和要给 \tabcolsep 留余量，不能把页宽用满
    fractions = [
        float(value)
        for value in re.findall(r"p\{([0-9.]+)\\linewidth\}", payload.column_spec)
    ]
    assert len(fractions) == 3
    assert all(value >= 0.045 for value in fractions)
    assert 0.80 <= sum(fractions) <= 0.95


def test_short_table_stays_plain() -> None:
    payload = table_html_to_latex(
        "<table><tr><td>A</td><td>B</td></tr><tr><td>1</td><td>2</td></tr></table>",
        table_id="t" * 32,
    )
    assert payload.wrapped is False
    assert payload.column_spec == "ll"
    assert r"\begin{tabular}{ll}" in payload.tabular


def test_plain_width_forces_l_columns() -> None:
    payload = table_html_to_latex(_wide_html(), table_id="t" * 32, width="plain")
    assert payload.wrapped is False
    assert payload.column_spec == "lll"
    assert r"\begin{tabular}{lll}" in payload.tabular


def test_unknown_width_mode_raises() -> None:
    with pytest.raises(TableLatexError):
        table_html_to_latex(_wide_html(), table_id="t" * 32, width="full")


def test_wide_table_multicolumn_gets_p_width() -> None:
    html = (
        "<table>"
        f"<tr><th colspan='2'>{LONG_TEXT}</th><th>备注</th></tr>"
        f"<tr><td>{LONG_TEXT}</td><td>second</td><td>0.9</td></tr>"
        "</table>"
    )
    payload = table_html_to_latex(html, table_id="t" * 32)
    assert payload.wrapped is True
    assert r"\multicolumn{2}{p{" in payload.tabular
    assert r"\multicolumn{2}{l}" not in payload.tabular


def test_wide_table_rowspan_wraps_with_parbox() -> None:
    html = (
        "<table>"
        f"<tr><td rowspan='2'>{LONG_TEXT}</td><td>a</td><td>b</td></tr>"
        f"<tr><td>{LONG_TEXT}</td><td>c</td></tr>"
        "</table>"
    )
    payload = table_html_to_latex(html, table_id="t" * 32)
    assert payload.wrapped is True
    assert r"\multirow{2}{*}" in payload.tabular
    assert r"\parbox{\linewidth}" in payload.tabular


def test_too_many_columns_warns_about_overflow() -> None:
    cells = "".join(f"<td>M{index}0000</td>" for index in range(13))
    html = f"<table><tr><td>{LONG_TEXT}</td>{cells}</tr></table>"
    payload = table_html_to_latex(html, table_id="t" * 32)
    assert payload.wrapped is True
    assert any("放不下" in warning for warning in payload.warnings)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@pytest.fixture
async def client(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'tables.db').as_posix()}", poolclass=NullPool
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


async def _seed(client) -> dict:
    Session = client.session_factory
    async with Session() as s:
        paper = Paper(title="表格测试文献", title_norm="表格测试文献", status="parsed")
        other = Paper(title="另一篇", title_norm="另一篇", status="parsed")
        s.add_all([paper, other])
        await s.flush()
        version = PaperVersion(
            paper_id=paper.id,
            kind="uploaded",
            file_sha256="a" * 64,
            file_path="pdfs/a.pdf",
            page_count=2,
            parser="docling",
            parsed_at=datetime.now(UTC),
        )
        s.add(version)
        await s.flush()
        table = TableBlock(
            paper_version_id=version.id,
            page=1,
            label_raw="表 2-1",
            label_norm="tab:2-1",
            caption="Table 2-1 工况参数",
            html=(
                "<table><tr><td>Case</td><td>Value</td></tr>"
                "<tr><td>L260</td><td>1.3</td></tr></table>"
            ),
            quality="structured",
        )
        image_table = TableBlock(
            paper_version_id=version.id,
            page=2,
            caption="只有图片的表",
            html=None,
            quality="image",
        )
        wide_table = TableBlock(
            paper_version_id=version.id,
            page=3,
            caption="宽表",
            html=_wide_html(),
            quality="structured",
        )
        s.add_all([table, image_table, wide_table])
        await s.commit()
        return {
            "paper_id": paper.id,
            "other_id": other.id,
            "table_id": table.id,
            "image_table_id": image_table.id,
            "wide_table_id": wide_table.id,
        }


async def test_table_latex_api(client) -> None:
    seed = await _seed(client)
    response = await client.get(
        f"/api/papers/{seed['paper_id']}/tables/{seed['table_id']}/latex"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["columns"] == 2 and body["rows"] == 2
    assert r"\begin{tabular}{ll}" in body["tabular"]
    assert body["wrapped"] is False and body["column_spec"] == "ll"
    assert "工况参数" in body["caption"]
    assert body["label_key"] == "tab:2-1"

    wide = await client.get(
        f"/api/papers/{seed['paper_id']}/tables/{seed['wide_table_id']}/latex"
    )
    assert wide.status_code == 200, wide.text
    wide_body = wide.json()
    assert wide_body["wrapped"] is True
    assert "p{" in wide_body["column_spec"]
    plain = await client.get(
        f"/api/papers/{seed['paper_id']}/tables/{seed['wide_table_id']}/latex?width=plain"
    )
    assert plain.status_code == 200, plain.text
    assert plain.json()["wrapped"] is False
    assert plain.json()["column_spec"] == "lll"
    bad_width = await client.get(
        f"/api/papers/{seed['paper_id']}/tables/{seed['wide_table_id']}/latex?width=full"
    )
    assert bad_width.status_code == 422

    missing = await client.get(
        f"/api/papers/{seed['paper_id']}/tables/{seed['image_table_id']}/latex"
    )
    assert missing.status_code == 422
    assert "图片表" in missing.text

    other_paper = await client.get(
        f"/api/papers/{seed['other_id']}/tables/{seed['table_id']}/latex"
    )
    assert other_paper.status_code == 404

    not_found = await client.get(
        f"/api/papers/{seed['paper_id']}/tables/{'0' * 32}/latex"
    )
    assert not_found.status_code == 404
