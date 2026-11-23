# SPDX-License-Identifier: AGPL-3.0-or-later
"""llm.py / prompts.py / evidence.py 单测：M2 的「模型输出 → 证据校验」这一环。"""

from __future__ import annotations

import pytest

from litbench.config import settings
from litbench.evidence import (
    EvidenceError,
    normalize_evidence_ids,
    validate_summary_payload,
)
from litbench.llm import (
    LLMError,
    MockLLM,
    get_client,
    parse_json_object,
)
from litbench.prompts import PROMPT_DIR, load_prompt, render


def test_prompt_registry_versions_by_content_hash() -> None:
    prompt = load_prompt("section_summary")
    assert prompt.version == load_prompt("section_summary").version
    assert len(prompt.version) == 12
    assert (PROMPT_DIR / "section_summary.md").is_file()
    rendered = render(
        prompt,
        section_title="7.1 凝结传热",
        page_start=2,
        page_end=3,
        evidence_lines="blk1 | p2 | 正文",
    )
    assert "7.1 凝结传热" in rendered
    assert "{{section_title}}" not in rendered
    assert "blk1 | p2 | 正文" in rendered


def test_parse_json_object_tolerates_fences_and_chatter() -> None:
    payload = '{"summary": "x", "key_points": [], "evidence_block_ids": []}'
    assert parse_json_object(payload) == {
        "summary": "x",
        "key_points": [],
        "evidence_block_ids": [],
    }
    assert parse_json_object(f"```json\n{payload}\n```") is not None
    assert parse_json_object("前面废话 " + payload + " 后面废话") is not None
    assert parse_json_object("不是 JSON") is None


async def test_mock_llm_uses_real_block_ids_from_meta() -> None:
    client = MockLLM()
    result = await client.complete(
        [{"role": "user", "content": "hi"}],
        meta={
            "section_title": "7.1 凝结传热",
            "page_start": 2,
            "page_end": 3,
            "evidence_ids": ["block-a", "block-b"],
            "block_ids": ["block-a", "block-b", "block-c"],
            "texts": ["凝结分为珠状与膜状凝结。", "珠状凝结阻力小。"],
        },
    )
    assert result.provider == "mock"
    assert result.parsed is not None
    assert result.parsed["evidence_block_ids"] == ["block-a", "block-b"]
    assert "7.1 凝结传热" in result.parsed["summary"]
    assert result.tokens_in > 0 and result.tokens_out > 0


async def test_mock_llm_can_simulate_bad_outputs() -> None:
    bad_json = MockLLM(raw="这不是 JSON")
    assert (await bad_json.complete([{"role": "user", "content": "hi"}])).parsed is None

    fake_evidence = MockLLM(evidence_ids=["not-in-this-section"])
    result = await fake_evidence.complete([{"role": "user", "content": "hi"}])
    assert result.parsed is not None
    with pytest.raises(EvidenceError):
        validate_summary_payload(result.parsed, allowed={"real-block"}, min_ids=1)

    low_conf = MockLLM(confidence=0.1, evidence_ids=["b1"])
    parsed = (await low_conf.complete([{"role": "user", "content": "hi"}])).parsed
    validated = validate_summary_payload(parsed, allowed={"b1"})
    assert validated.confidence < 0.9

    failing = MockLLM(fail="网关超时")
    with pytest.raises(LLMError):
        await failing.complete([{"role": "user", "content": "hi"}])


async def test_get_client_uses_mock_and_explains_unconfigured(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(settings, "llm_config_file", None)
    monkeypatch.setattr(settings, "llm_base_url", None)
    monkeypatch.setattr(settings, "llm_api_key", None)
    monkeypatch.setattr(settings, "llm_provider", "mock")

    assert isinstance(get_client(), MockLLM)
    assert get_client("mock").provider == "mock"

    with pytest.raises(LLMError) as excinfo:
        get_client("deepseek")
    assert "设置" in str(excinfo.value)


def test_evidence_must_be_non_empty_and_known() -> None:
    allowed = {"b1", "b2"}
    with pytest.raises(EvidenceError):
        normalize_evidence_ids([], allowed=allowed)
    with pytest.raises(EvidenceError):
        normalize_evidence_ids(None, allowed=allowed)
    with pytest.raises(EvidenceError):
        normalize_evidence_ids(["b3"], allowed=allowed)
    assert normalize_evidence_ids(["b2", "b1", "b2"], allowed=allowed) == ["b2", "b1"]
    # 项目符号与空字符串都算无效
    with pytest.raises(EvidenceError):
        normalize_evidence_ids(["b1", "  "], allowed=allowed)


def test_validate_summary_payload_checks_content_and_points() -> None:
    allowed = {"b1"}
    with pytest.raises(EvidenceError):
        validate_summary_payload(
            {"summary": "", "key_points": ["x"], "evidence_block_ids": ["b1"]}, allowed=allowed
        )
    with pytest.raises(EvidenceError):
        validate_summary_payload(
            {"summary": "ok", "key_points": [], "evidence_block_ids": ["b1"]}, allowed=allowed
        )
    validated = validate_summary_payload(
        {
            "summary": "  这一段摘要基于证据块。 ",
            "key_points": ["要点一", "  ", "要点二"],
            "evidence_block_ids": ["b1"],
            "confidence": 1.7,
        },
        allowed=allowed,
    )
    assert validated.content == "这一段摘要基于证据块。"
    assert validated.key_points == ["要点一", "要点二"]
    assert validated.confidence == 1.0
