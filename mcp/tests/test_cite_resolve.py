# SPDX-License-Identifier: MIT
"""cite.resolve：确定性词面打分的三种 verdict + 数字一致性。"""

from __future__ import annotations

from fake_api import ATTN_TEXT, BLOCK_ATTN, PAPER_ATTENTION, PAPER_COAL
from helpers import call_tool

UNRELATED_CLAIM = "Banana bread requires ripe bananas, flour and a very hot oven."


async def test_supported_claim(mcp) -> None:
    data = await call_tool(
        mcp,
        "cite.resolve",
        {
            "claim_text": "Scaled dot product attention normalizes compatibility scores with softmax.",
            "candidate_papers": ["vaswani2017attention"],
        },
    )
    assert data["ok"] is True
    assert data["content_trust"] == "untrusted"
    result = data["results"][0]
    assert result["paper_id"] == PAPER_ATTENTION
    assert result["verdict"] == "supported"
    assert result["block_ids"][0] == BLOCK_ATTN
    assert result["score"] >= data["thresholds"]["supported_score"]


async def test_insufficient_claim(mcp) -> None:
    data = await call_tool(
        mcp,
        "cite.resolve",
        {
            "claim_text": UNRELATED_CLAIM,
            "candidate_papers": ["vaswani2017attention"],
        },
    )
    result = data["results"][0]
    assert result["verdict"] == "insufficient"
    assert result["block_ids"] == []  # 零重合的 block 不应被当成引用
    assert result["score"] < data["thresholds"]["supported_score"]


async def test_not_found_candidate(mcp) -> None:
    data = await call_tool(
        mcp,
        "cite.resolve",
        {
            "claim_text": "Some claim text.",
            "candidate_papers": ["no-such-paper-xyz"],
        },
    )
    result = data["results"][0]
    assert result["verdict"] == "not_found"
    assert result["paper_id"] is None
    assert result["block_ids"] == []


async def test_numeric_mismatch_downgrades_to_insufficient(mcp) -> None:
    """词面高度重合但数字对不上 → 不能判 supported（编数字是 citations 的最大风险）。"""
    data = await call_tool(
        mcp,
        "cite.resolve",
        {
            "claim_text": "We train on the WMT 2014 English German task with 128 GPUs.",
            "candidate_papers": ["vaswani2017attention"],
        },
    )
    result = data["results"][0]
    assert result["numeric_consistent"] is False
    assert result["verdict"] == "insufficient"


async def test_mixed_candidates_keep_order_and_verdicts(mcp) -> None:
    data = await call_tool(
        mcp,
        "cite.resolve",
        {
            "claim_text": "Scaled dot product attention normalizes compatibility scores with softmax.",
            "candidate_papers": ["vaswani2017attention", "wang2019coal", "no-such-paper"],
        },
    )
    results = data["results"]
    assert [result["ref"] for result in results] == [
        "vaswani2017attention",
        "wang2019coal",
        "no-such-paper",
    ]
    assert results[0]["verdict"] == "supported"
    assert results[1]["verdict"] == "insufficient"
    assert results[1]["paper_id"] == PAPER_COAL
    assert results[2]["verdict"] == "not_found"


async def test_paper_without_text_is_insufficient_not_error(mcp) -> None:
    # metadata-only 文献：文献找到了但没有正文 → insufficient（不是 not_found）
    from fake_api import PAPER_METADATA_ONLY

    data = await call_tool(
        mcp,
        "cite.resolve",
        {"claim_text": "Some claim.", "candidate_papers": [PAPER_METADATA_ONLY]},
    )
    result = data["results"][0]
    assert result["verdict"] == "insufficient"
    assert "没有解析版本" in result["note"]


async def test_empty_candidates_is_schema_error(mcp) -> None:
    result = await mcp.call_tool(
        "cite.resolve", {"claim_text": "x", "candidate_papers": []}
    )
    assert result.is_error is True


async def test_claim_is_echoed_as_length_only(mcp) -> None:
    """不把 claim 原文塞回结果（减少 agent 把用户文本当证据的错觉）。"""
    data = await call_tool(
        mcp,
        "cite.resolve",
        {
            "claim_text": ATTN_TEXT,
            "candidate_papers": ["vaswani2017attention"],
        },
    )
    assert "claim_text_chars" not in data
    assert "claim" not in str(data.keys())
