# SPDX-License-Identifier: MIT
"""M9.4：cite.resolve 双档（词面 lexical / 可选模型复核 model）。

模型复核失败必须回退词面档并标明，不能把「调用了模型」当成已复核。
"""

from __future__ import annotations

from fake_api import (
    PAPER_ATTENTION,
    PAPER_COAL,
    PAPER_METADATA_ONLY,
)
from helpers import call_tool

CLAIM = "Scaled dot product attention normalizes compatibility scores with softmax."


def _set_review(fake, paper_id: str, **override) -> None:
    payload = {"verdict": "supported", "confidence": 0.9, "reason": "fake 复核"}
    payload.update(override)
    fake.library.citation_reviews[paper_id] = payload


async def test_default_tier_is_lexical(mcp) -> None:
    data = await call_tool(
        mcp,
        "cite.resolve",
        {"claim_text": CLAIM, "candidate_papers": ["vaswani2017attention"]},
    )
    assert data["ok"] is True
    assert data["tier"] == "lexical"
    assert data["review_requested"] is False
    assert data["results"][0]["verdict"] == "supported"


async def test_review_true_marks_model_tier(mcp, fake) -> None:
    _set_review(fake, PAPER_ATTENTION, verdict="supported", confidence=0.88)
    data = await call_tool(
        mcp,
        "cite.resolve",
        {
            "claim_text": CLAIM,
            "candidate_papers": ["vaswani2017attention"],
            "review": True,
        },
    )
    assert data["tier"] == "model"
    assert data["review_requested"] is True
    assert data["review"] == {"attempted": 1, "model": 1, "fallback": 0}
    item = data["results"][0]
    assert item["verdict"] == "supported"
    assert item["verdict_basis"] == "model"
    assert item["model_review"]["ok"] is True
    assert item["model_review"]["provider"] == "fake"
    assert item["model_review"]["confidence"] == 0.88
    assert item["lexical"]["verdict"] == "supported"
    assert item["block_ids"]


async def test_model_can_override_lexical_false_negative(mcp, fake) -> None:
    """词面 0 命中（同义改写）时，review 仍把候选块送模型；模型可以判 supported。"""
    _set_review(fake, PAPER_ATTENTION, verdict="supported", confidence=0.77)
    data = await call_tool(
        mcp,
        "cite.resolve",
        {
            "claim_text": "Banana bread requires ripe bananas and a very hot oven.",
            "candidate_papers": [PAPER_ATTENTION],
            "review": True,
        },
    )
    item = data["results"][0]
    assert item["lexical"]["verdict"] == "insufficient"
    assert item["verdict"] == "supported"
    assert item["verdict_basis"] == "model"


async def test_model_insufficient_overrides_lexical_supported(mcp, fake) -> None:
    _set_review(fake, PAPER_ATTENTION, verdict="insufficient", confidence=0.2)
    data = await call_tool(
        mcp,
        "cite.resolve",
        {"claim_text": CLAIM, "candidate_papers": [PAPER_ATTENTION], "review": True},
    )
    item = data["results"][0]
    assert item["lexical"]["verdict"] == "supported"
    assert item["verdict"] == "insufficient"
    assert item["verdict_basis"] == "model"


async def test_review_unavailable_falls_back_to_lexical(mcp, fake) -> None:
    # 没有配置模型：fake API 返回 503
    data = await call_tool(
        mcp,
        "cite.resolve",
        {"claim_text": CLAIM, "candidate_papers": [PAPER_ATTENTION], "review": True},
    )
    assert data["tier"] == "lexical"
    assert data["review_note"].startswith("模型复核没有成功")
    assert data["review"] == {"attempted": 1, "model": 0, "fallback": 1}
    item = data["results"][0]
    assert item["verdict_basis"] == "lexical"
    assert item["verdict"] == "supported"
    assert item["model_review"]["ok"] is False
    assert item["model_review"]["error_code"] == "llm_not_configured"


async def test_review_without_evidence_is_marked(mcp, fake) -> None:
    data = await call_tool(
        mcp,
        "cite.resolve",
        {
            "claim_text": CLAIM,
            "candidate_papers": [PAPER_METADATA_ONLY],
            "review": True,
        },
    )
    item = data["results"][0]
    assert item["verdict_basis"] == "lexical"
    assert item["model_review"]["error_code"] == "no_evidence"
    assert data["tier"] == "lexical"
    assert data.get("review", {}).get("attempted") == 0


async def test_review_mixed_candidates_keep_order(mcp, fake) -> None:
    _set_review(fake, PAPER_ATTENTION, verdict="supported")
    _set_review(fake, PAPER_COAL, verdict="insufficient")
    data = await call_tool(
        mcp,
        "cite.resolve",
        {
            "claim_text": CLAIM,
            "candidate_papers": [PAPER_ATTENTION, PAPER_COAL, "no-such-paper"],
            "review": True,
        },
    )
    results = data["results"]
    assert [item["ref"] for item in results] == [
        PAPER_ATTENTION,
        PAPER_COAL,
        "no-such-paper",
    ]
    assert results[0]["verdict_basis"] == "model"
    assert results[1]["verdict_basis"] == "model"
    assert results[2]["verdict"] == "not_found"
    assert results[2].get("model_review") is None
    assert data["review"] == {"attempted": 2, "model": 2, "fallback": 0}
