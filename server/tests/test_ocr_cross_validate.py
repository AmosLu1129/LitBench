# SPDX-License-Identifier: AGPL-3.0-or-later
"""视觉模型 OCR 交叉校验：无网络单测。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from litbench import ocr_cross_validate as validator
from litbench.llm import LLMResult


class _FakeClient:
    provider = "fake"
    model = "fake-vision"

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.messages: list[dict] | None = None

    async def complete(self, messages, **_kwargs) -> LLMResult:
        self.messages = messages
        text = json.dumps(self.payload, ensure_ascii=False)
        return LLMResult(
            text=text,
            parsed=self.payload,
            provider=self.provider,
            model=self.model,
            tokens_in=100,
            tokens_out=20,
            cost=0.001,
            latency_ms=12,
        )


@pytest.fixture(autouse=True)
def _stub_vision(monkeypatch):
    monkeypatch.setattr(validator, "page_image_jpeg", lambda *_a, **_kw: b"jpeg")
    monkeypatch.setattr(validator, "effective_config", lambda: ("env", object()))


async def test_cross_check_same_page_is_accepted(monkeypatch) -> None:
    client = _FakeClient({"same": True, "differences": []})
    monkeypatch.setattr(validator, "get_client", lambda: client)
    result = await validator.cross_check_page(Path("fake.pdf"), 2, "图 1 实验装置")
    assert result is not None
    assert result.same is True
    assert result.model_text == "图 1 实验装置"


async def test_cross_check_difference_is_flagged(monkeypatch) -> None:
    client = _FakeClient(
        {
            "same": False,
            "corrected_text": "图 1 实验装置示意",
            "differences": [{"ocr": "图 1 实验装置", "model": "图 1 实验装置示意"}],
        }
    )
    monkeypatch.setattr(validator, "get_client", lambda: client)
    result = await validator.cross_check_page(Path("fake.pdf"), 2, "图 1 实验装置")
    assert result is not None
    assert result.same is False
    assert result.differences == [{"ocr": "图 1 实验装置", "model": "图 1 实验装置示意"}]
    assert result.tokens_in == 100
