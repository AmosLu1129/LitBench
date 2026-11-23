# SPDX-License-Identifier: MIT
"""figure.get / table.get。"""

from __future__ import annotations

from dataclasses import replace

from fake_api import (
    FIG_ATTENTION_1,
    FIG_ATTENTION_2,
    PAPER_ATTENTION,
    PAPER_COAL,
    TABLE_ATTENTION_1,
    VERSION_ATTENTION,
)
from helpers import call_tool
from mcp import Client

from litbench_mcp.server import create_server


# ---------------------------------------------------------------------------
# figure.get
# ---------------------------------------------------------------------------
async def test_figure_by_id(mcp) -> None:
    data = await call_tool(mcp, "figure.get", {"ref": FIG_ATTENTION_1})
    assert data["ok"] is True
    assert data["content_trust"] == "untrusted"
    assert data["matched_by"] == "figure_id"
    figure = data["figure"]
    assert figure["figure_id"] == FIG_ATTENTION_1
    assert figure["cite_key"]
    assert figure["paper_id"] == PAPER_ATTENTION
    assert figure["paper_title"] == "Attention Is All You Need"
    assert figure["year"] == 2017
    assert figure["page"] == 1
    assert figure["label_raw"] == "Figure 1"
    assert figure["label_norm"] == "fig:1"
    assert figure["caption"].startswith("Transformer architecture")
    assert len(figure["bbox"]) == 4
    assert figure["license_hint"] == "unknown"
    assert figure["label_confidence"] == 1.0
    # 不返回图片二进制 / URL
    assert "image" not in str(figure).lower() or "image_url" not in figure


async def test_figure_by_paper_cite_key_and_label(mcp) -> None:
    data = await call_tool(
        mcp, "figure.get", {"paper": "vaswani2017attention", "label": "Figure 2"}
    )
    assert data["matched_by"] == "paper+label"
    assert data["figure"]["figure_id"] == FIG_ATTENTION_2
    assert data["total_matches"] == 1


async def test_figure_by_paper_id_returns_first_match(mcp) -> None:
    data = await call_tool(mcp, "figure.get", {"paper": PAPER_ATTENTION})
    assert data["matched_by"] == "paper"
    assert data["total_matches"] == 2
    assert data["figure"]["figure_id"] == FIG_ATTENTION_1


async def test_figure_ref_can_be_a_paper_reference(mcp) -> None:
    data = await call_tool(mcp, "figure.get", {"ref": "wang2019coal"})
    assert data["ok"] is True
    assert data["figure"]["paper_id"] == PAPER_COAL


async def test_figure_not_found(mcp) -> None:
    missing = await call_tool(mcp, "figure.get", {"ref": "0" * 32})
    assert missing["ok"] is False
    assert missing["error_code"] == "not_found"

    missing_label = await call_tool(
        mcp, "figure.get", {"paper": PAPER_ATTENTION, "label": "Figure 9"}
    )
    assert missing_label["ok"] is False
    assert missing_label["error_code"] == "not_found"


async def test_figure_missing_arguments(mcp) -> None:
    no_args = await call_tool(mcp, "figure.get", {})
    assert no_args["ok"] is False
    assert no_args["error_code"] == "invalid_params"

    label_only = await call_tool(mcp, "figure.get", {"label": "Figure 1"})
    assert label_only["ok"] is False
    assert label_only["error_code"] == "invalid_params"


async def test_figure_upstream_500(mcp, fake) -> None:
    fake.failures[("GET", f"/api/figures/{FIG_ATTENTION_1}")] = 500
    data = await call_tool(mcp, "figure.get", {"ref": FIG_ATTENTION_1})
    assert data["ok"] is False
    assert data["error_code"] == "upstream_error"


# ---------------------------------------------------------------------------
# table.get
# ---------------------------------------------------------------------------
async def test_table_by_paper(mcp) -> None:
    data = await call_tool(mcp, "table.get", {"paper": "vaswani2017attention"})
    assert data["ok"] is True
    assert data["paper_id"] == PAPER_ATTENTION
    assert data["count"] == 1
    table = data["items"][0]
    assert table["table_id"] == TABLE_ATTENTION_1
    assert table["label"] == "Table 1"
    assert table["caption"] == "BLEU scores on WMT 2014"
    assert table["page"] == 4
    assert table["quality"] == "structured"
    assert table["html"].startswith("<table>")
    assert table["truncated"] is False
    assert data["content_trust"] == "untrusted"


async def test_table_filter_by_id_and_label(mcp) -> None:
    by_id = await call_tool(mcp, "table.get", {"paper": PAPER_ATTENTION, "table_id": TABLE_ATTENTION_1})
    assert by_id["count"] == 1

    by_label = await call_tool(mcp, "table.get", {"paper": PAPER_ATTENTION, "label": "表 1"})
    assert by_label["count"] == 1
    assert by_label["items"][0]["table_id"] == TABLE_ATTENTION_1

    by_english = await call_tool(mcp, "table.get", {"paper": PAPER_ATTENTION, "label": "Table 1"})
    assert by_english["count"] == 1


async def test_table_paper_without_tables_is_empty(mcp) -> None:
    data = await call_tool(mcp, "table.get", {"paper": PAPER_COAL})
    assert data["ok"] is True
    assert data["items"] == []
    assert data["count"] == 0


async def test_table_missing_table_or_paper(mcp) -> None:
    missing = await call_tool(mcp, "table.get", {"paper": PAPER_ATTENTION, "table_id": "x" * 32})
    assert missing["ok"] is False
    assert missing["error_code"] == "not_found"

    missing_label = await call_tool(mcp, "table.get", {"paper": PAPER_ATTENTION, "label": "表 9"})
    assert missing_label["ok"] is False
    assert missing_label["error_code"] == "not_found"

    missing_paper = await call_tool(mcp, "table.get", {"paper": "no-such-paper"})
    assert missing_paper["ok"] is False
    assert missing_paper["error_code"] == "not_found"


async def test_table_empty_html_has_note(mcp, fake) -> None:
    """Docling 只定位到表区域、抽不出 grid 时 html 为空；返回体要说明「图片表」而不是静默空串。"""
    fake.library.tables.append(
        {
            "table_id": "e" * 32,
            "paper_id": PAPER_ATTENTION,
            "version_id": VERSION_ATTENTION,
            "page": 9,
            "label_raw": "Table 2",
            "label_norm": "tab:2",
            "caption": "Image-only table",
            "quality": "image",
            "html": "",
        }
    )
    data = await call_tool(mcp, "table.get", {"paper": PAPER_ATTENTION, "label": "Table 2"})
    assert data["count"] == 1
    table = data["items"][0]
    assert table["html"] == ""
    assert table["quality"] == "image"
    assert "图片表" in table["note"]


async def test_table_html_truncated(settings, api) -> None:
    small = replace(settings, max_html_chars=12)
    server = create_server(small, api=api)
    async with Client(server) as client:
        data = await call_tool(client, "table.get", {"paper": PAPER_ATTENTION})
    table = data["items"][0]
    assert table["truncated"] is True
    assert len(table["html"]) == 12


async def test_table_upstream_500(mcp, fake) -> None:
    fake.failures[("GET", f"/api/papers/{PAPER_ATTENTION}/tables")] = 500
    data = await call_tool(mcp, "table.get", {"paper": PAPER_ATTENTION})
    assert data["ok"] is False
    assert data["error_code"] == "upstream_error"
