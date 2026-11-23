# SPDX-License-Identifier: MIT
"""evidence.get：paper / cite_key / DOI / arXiv / figure / summary 六种 ref + 排序/截断/错误。"""

from __future__ import annotations

from fake_api import (
    ATTN_TEXT,
    BLOCK_ATTN,
    BLOCK_INTRO,
    BLOCK_LONG,
    FIG_ATTENTION_1,
    INTRO_TEXT,
    PAPER_ATTENTION,
    PAPER_METADATA_ONLY,
    SUMMARY_ATTENTION_WHOLE,
    VERSION_ATTENTION,
)
from helpers import call_tool


async def test_evidence_by_paper_id(mcp) -> None:
    data = await call_tool(mcp, "evidence.get", {"ref": PAPER_ATTENTION})
    assert data["ok"] is True
    assert data["kind"] == "paper"
    assert data["paper_id"] == PAPER_ATTENTION
    assert data["count"] == 4
    assert data["content_trust"] == "untrusted"
    block = data["items"][0]
    assert block["block_id"] == BLOCK_INTRO
    assert block["paper_version_id"] == VERSION_ATTENTION
    assert block["page"] == 1
    assert block["bbox"] == [10.0, 20.0, 300.0, 40.0]
    assert block["text"] == INTRO_TEXT


async def test_evidence_by_cite_key(mcp) -> None:
    data = await call_tool(mcp, "evidence.get", {"ref": "vaswani2017attention"})
    assert data["kind"] == "paper"
    assert data["paper_id"] == PAPER_ATTENTION
    assert {item["block_id"] for item in data["items"]} >= {BLOCK_INTRO, BLOCK_ATTN}


async def test_evidence_by_doi_and_arxiv(mcp) -> None:
    by_doi = await call_tool(mcp, "evidence.get", {"ref": "10.5555/attention.2017"})
    assert by_doi["paper_id"] == PAPER_ATTENTION
    by_arxiv = await call_tool(mcp, "evidence.get", {"ref": "1706.03762"})
    assert by_arxiv["paper_id"] == PAPER_ATTENTION


async def test_evidence_by_figure_id_returns_same_page_blocks(mcp) -> None:
    data = await call_tool(mcp, "evidence.get", {"ref": FIG_ATTENTION_1})
    assert data["ok"] is True
    assert data["kind"] == "figure"
    assert data["paper_id"] == PAPER_ATTENTION
    assert {item["block_id"] for item in data["items"]} == {BLOCK_INTRO, BLOCK_ATTN}
    assert all(item["page"] == 1 for item in data["items"])


async def test_evidence_by_summary_id(mcp) -> None:
    data = await call_tool(mcp, "evidence.get", {"ref": SUMMARY_ATTENTION_WHOLE})
    assert data["ok"] is True
    assert data["kind"] == "summary"
    assert {item["block_id"] for item in data["items"]} == {BLOCK_INTRO, BLOCK_ATTN}
    assert all(item["paper_version_id"] == VERSION_ATTENTION for item in data["items"])


async def test_evidence_query_sorts_by_lexical_relevance(mcp) -> None:
    data = await call_tool(
        mcp, "evidence.get", {"ref": PAPER_ATTENTION, "query": "scaled dot product softmax"}
    )
    assert data["items"][0]["block_id"] == BLOCK_ATTN
    assert data["items"][0]["text"] == ATTN_TEXT


async def test_evidence_long_text_is_truncated(mcp, settings) -> None:
    data = await call_tool(mcp, "evidence.get", {"ref": PAPER_ATTENTION, "limit": 10})
    long_block = next(item for item in data["items"] if item["block_id"] == BLOCK_LONG)
    assert long_block["truncated"] is True
    assert len(long_block["text"]) == settings.max_text_chars
    assert data["truncated"] is True
    # 短文不截断
    short_block = next(item for item in data["items"] if item["block_id"] == BLOCK_INTRO)
    assert short_block["truncated"] is False


async def test_evidence_limit_and_count(mcp) -> None:
    data = await call_tool(mcp, "evidence.get", {"ref": PAPER_ATTENTION, "limit": 2})
    assert data["count"] == 2
    assert len(data["items"]) == 2


async def test_evidence_unknown_ref_is_not_found(mcp) -> None:
    data = await call_tool(mcp, "evidence.get", {"ref": "no-such-ref-xyz"})
    assert data["ok"] is False
    assert data["error_code"] == "not_found"
    assert "找不到" in data["error"]


async def test_evidence_paper_without_version_is_not_found(mcp) -> None:
    data = await call_tool(mcp, "evidence.get", {"ref": PAPER_METADATA_ONLY})
    assert data["ok"] is False
    assert data["error_code"] == "not_found"


async def test_evidence_upstream_500(mcp, fake) -> None:
    fake.failures[("GET", f"/api/papers/{PAPER_ATTENTION}/text")] = 500
    data = await call_tool(mcp, "evidence.get", {"ref": PAPER_ATTENTION})
    assert data["ok"] is False
    assert data["error_code"] == "upstream_error"


async def test_evidence_figure_without_text_returns_empty_with_hint(mcp, fake) -> None:
    # 图属于「只有元数据、没有 PDF 版本」的文献
    fake.library.figures.append(
        {
            "figure_id": "f9" * 16,
            "cite_key": "meta-v1-f1",
            "paper_id": PAPER_METADATA_ONLY,
            "paper_title": "Metadata only paper",
            "year": 2020,
            "version_id": "e1" * 16,
            "page": 1,
            "label_raw": "Figure 1",
            "label_norm": "fig:1",
            "caption": "no pdf",
            "bbox": [1.0, 2.0, 3.0, 4.0],
            "license_hint": "unknown",
            "label_confidence": 0.5,
            "review_status": "pending",
        }
    )
    data = await call_tool(mcp, "evidence.get", {"ref": "f9" * 16})
    assert data["ok"] is True
    assert data["kind"] == "figure"
    assert data["items"] == []
    assert "还没有解析版本" in (data["hint"] or "")


async def test_evidence_bad_argument_types_are_schema_errors(mcp) -> None:
    result = await mcp.call_tool("evidence.get", {"ref": 123})
    assert result.is_error is True
