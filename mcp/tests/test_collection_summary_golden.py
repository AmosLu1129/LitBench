# SPDX-License-Identifier: MIT
"""collection.tree / summary.get / dataset.golden。"""

from __future__ import annotations

from fake_api import (
    BLOCK_INTRO,
    COLLECTION_NLP,
    COLLECTION_TRANSFORMERS,
    PAPER_ATTENTION,
    PAPER_METADATA_ONLY,
    SUMMARY_ATTENTION_SECTION,
    SUMMARY_ATTENTION_WHOLE,
)
from helpers import call_tool


# ---------------------------------------------------------------------------
# collection.tree
# ---------------------------------------------------------------------------
async def test_collection_tree_structure_and_counts(mcp) -> None:
    data = await call_tool(mcp, "collection.tree")
    assert data["ok"] is True
    assert data["count"] == 3
    roots = data["tree"]
    assert [node["id"] for node in roots] == [COLLECTION_NLP, "c3" + "c3" * 15]
    nlp = roots[0]
    assert nlp["name"] == "NLP"
    assert nlp["paper_count"] == 1
    child = nlp["children"][0]
    assert child["id"] == COLLECTION_TRANSFORMERS
    assert child["name"] == "Transformers"
    assert child["children"] == []
    assert roots[1]["name"] == "化工"


async def test_collection_tree_empty(mcp, fake) -> None:
    fake.library.collections = []
    data = await call_tool(mcp, "collection.tree")
    assert data["ok"] is True
    assert data["tree"] == []
    assert data["count"] == 0


async def test_collection_tree_orphan_is_root(mcp, fake) -> None:
    fake.library.collections.append(
        {
            "id": "ef" * 16,
            "name": "Orphan",
            "parent_id": "ff" * 16,  # 父节点不存在 → 当根返回，不丢
            "sort_order": 9,
            "source": "manual",
            "zotero_key": None,
            "paper_count": 0,
        }
    )
    data = await call_tool(mcp, "collection.tree")
    assert data["count"] == 4
    assert any(node["name"] == "Orphan" for node in data["tree"])


async def test_collection_tree_upstream_500(mcp, fake) -> None:
    fake.failures[("GET", "/api/collections")] = 500
    data = await call_tool(mcp, "collection.tree")
    assert data["ok"] is False
    assert data["error_code"] == "upstream_error"


# ---------------------------------------------------------------------------
# summary.get
# ---------------------------------------------------------------------------
async def test_summary_get_whole_paper(mcp) -> None:
    data = await call_tool(mcp, "summary.get", {"paper": "vaswani2017attention"})
    assert data["ok"] is True
    assert data["paper_id"] == PAPER_ATTENTION
    assert data["content_trust"] == "untrusted"
    summary = data["summary"]
    assert summary["summary_id"] == SUMMARY_ATTENTION_WHOLE
    assert summary["section_key"] == "__paper__"
    assert summary["status"] == "published"
    assert summary["content"]
    assert summary["key_points"]
    assert len(data["evidence"]) == 2
    evidence = data["evidence"][0]
    assert evidence["block_id"] == BLOCK_INTRO
    assert evidence["text"]
    assert evidence["truncated"] is False


async def test_summary_get_section(mcp) -> None:
    data = await call_tool(
        mcp, "summary.get", {"paper": PAPER_ATTENTION, "section_key": "sec-1"}
    )
    assert data["ok"] is True
    assert data["summary"]["summary_id"] == SUMMARY_ATTENTION_SECTION
    assert data["summary"]["section_key"] == "sec-1"
    assert [item["block_id"] for item in data["evidence"]] == [BLOCK_INTRO]


async def test_summary_not_generated_returns_error_code(mcp) -> None:
    whole = await call_tool(mcp, "summary.get", {"paper": "wang2019coal"})
    assert whole["ok"] is False
    assert whole["error_code"] == "not_generated"
    assert "整篇速览" in whole["error"]

    section = await call_tool(
        mcp, "summary.get", {"paper": PAPER_ATTENTION, "section_key": "sec-404"}
    )
    assert section["ok"] is False
    assert section["error_code"] == "not_generated"

    metadata_only = await call_tool(mcp, "summary.get", {"paper": PAPER_METADATA_ONLY})
    assert metadata_only["ok"] is False
    assert metadata_only["error_code"] == "not_generated"


async def test_summary_unknown_paper_is_not_found(mcp) -> None:
    data = await call_tool(mcp, "summary.get", {"paper": "no-such-paper"})
    assert data["ok"] is False
    assert data["error_code"] == "not_found"


async def test_summary_upstream_500(mcp, fake) -> None:
    fake.failures[("GET", f"/api/papers/{PAPER_ATTENTION}/summary")] = 500
    data = await call_tool(mcp, "summary.get", {"paper": PAPER_ATTENTION})
    assert data["ok"] is False
    assert data["error_code"] == "upstream_error"


# ---------------------------------------------------------------------------
# dataset.golden
# ---------------------------------------------------------------------------
async def test_dataset_golden_passthrough(mcp, fake) -> None:
    data = await call_tool(mcp, "dataset.golden", {})
    assert data["ok"] is True
    assert data["content_trust"] == "untrusted"
    assert data["schema"] == "litbench.golden/v1"
    assert data["item_count"] == 1
    assert data["items"][0]["cite_key"] == "vaswani-v1-fig1"
    assert data["include_pending"] is False
    assert fake.calls[-1]["params"]["include_pending"] == "false"


async def test_dataset_golden_include_pending_forwarded(mcp, fake) -> None:
    await call_tool(mcp, "dataset.golden", {"include_pending": True})
    assert fake.calls[-1]["params"]["include_pending"] == "true"


async def test_dataset_golden_empty(mcp, fake) -> None:
    fake.library.golden = {
        "schema": "litbench.golden/v1",
        "exported_at": "2026-11-13T00:00:00+00:00",
        "include_pending": False,
        "item_count": 0,
        "items": [],
    }
    data = await call_tool(mcp, "dataset.golden", {})
    assert data["ok"] is True
    assert data["item_count"] == 0
    assert data["items"] == []


async def test_dataset_golden_upstream_500(mcp, fake) -> None:
    fake.failures[("GET", "/api/dataset/golden")] = 500
    data = await call_tool(mcp, "dataset.golden", {})
    assert data["ok"] is False
    assert data["error_code"] == "upstream_error"

async def test_dataset_golden_bbox_set(mcp, fake) -> None:
    fake.library.golden_bboxes = {
        "schema": "litbench.golden.bboxes/1",
        "exported_at": "2026-11-13T00:00:00+00:00",
        "include_pending": False,
        "item_count": 1,
        "skipped_no_bbox": 2,
        "items": [
            {
                "item_id": "f" * 32,
                "paper_id": PAPER_ATTENTION,
                "page": 3,
                "bbox": [10.0, 20.0, 110.0, 70.0],
                "bbox_source": "override",
                "source_path": "pdfs/ab/figure.pdf",
            }
        ],
    }
    data = await call_tool(mcp, "dataset.golden", {"bbox_only": True})
    assert data["ok"] is True
    assert data["bbox_only"] is True
    assert data["content_trust"] == "untrusted"
    assert data["schema"] == "litbench.golden.bboxes/1"
    assert data["item_count"] == 1
    assert data["skipped_no_bbox"] == 2
    assert data["items"][0]["bbox_source"] == "override"
    assert fake.calls[-1]["params"]["format"] == "bboxes"
    assert fake.calls[-1]["params"]["include_pending"] == "false"


async def test_dataset_golden_default_does_not_request_bboxes(mcp, fake) -> None:
    await call_tool(mcp, "dataset.golden", {})
    assert "format" not in fake.calls[-1]["params"]
    assert fake.calls[-1]["params"]["include_pending"] == "false"
