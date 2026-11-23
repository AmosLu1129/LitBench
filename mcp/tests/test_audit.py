# SPDX-License-Identifier: MIT
"""审计 JSONL：字段、preview 上限、脱敏、**绝不写文献正文**。"""

from __future__ import annotations

import json

from fake_api import INTRO_TEXT, PAPER_ATTENTION
from helpers import call_tool

from litbench_mcp.audit import SENSITIVE_KEYS, AuditLogger, summarize_params


def _read_audit(path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines]


async def test_audit_records_tool_call(mcp, settings) -> None:
    await call_tool(mcp, "collection.tree")
    records = _read_audit(settings.audit_path)
    assert len(records) == 1
    record = records[0]
    assert set(record) == {
        "ts",
        "actor",
        "tool",
        "params_digest",
        "params_preview",
        "ok",
        "error_code",
        "duration_ms",
    }
    assert record["actor"] == "stdio"
    assert record["tool"] == "collection.tree"
    assert record["ok"] is True
    assert record["error_code"] is None
    assert isinstance(record["duration_ms"], int)
    assert len(record["params_digest"]) == 64
    assert record["params_preview"] == "{}"


async def test_audit_never_contains_paper_text(mcp, settings) -> None:
    data = await call_tool(mcp, "evidence.get", {"ref": PAPER_ATTENTION})
    assert data["items"], "前置条件：应该取到证据块"
    raw = settings.audit_path.read_text(encoding="utf-8")
    # 文献正文（哪怕前 40 字）绝不能出现在审计里
    assert INTRO_TEXT[:40] not in raw
    assert "Transformer uses self attention" not in raw
    record = _read_audit(settings.audit_path)[0]
    assert record["params_preview"] == '{"limit":8,"ref":"' + PAPER_ATTENTION + '"}'


async def test_audit_records_errors(mcp, settings, fake) -> None:
    fake.failures[("GET", "/api/papers")] = 500
    data = await call_tool(mcp, "library.search", {"query": "coal"})
    assert data["ok"] is False
    record = _read_audit(settings.audit_path)[0]
    assert record["ok"] is False
    assert record["error_code"] == "upstream_error"


def test_params_preview_is_short_and_single_line() -> None:
    long_claim = "claim " * 500
    digest, preview = summarize_params({"claim_text": long_claim, "limit": 3})
    assert len(digest) == 64
    assert len(preview) <= 80
    assert "\n" not in preview


def test_params_digest_tracks_params_but_not_in_clear() -> None:
    digest_a, _ = summarize_params({"query": "coal"})
    digest_b, _ = summarize_params({"query": "coal"})
    digest_c, _ = summarize_params({"query": "pyrolysis"})
    assert digest_a == digest_b
    assert digest_a != digest_c
    assert digest_a != "coal"


def test_sensitive_params_are_redacted() -> None:
    for key in SENSITIVE_KEYS:
        digest, preview = summarize_params({key: "super-secret-value"})
        assert "super-secret-value" not in preview
        assert "***" in preview
        assert "super-secret-value" not in digest


def test_audit_append_only(tmp_path) -> None:
    logger = AuditLogger(tmp_path / "audit.jsonl")
    logger.write(actor="dsh", tool="a", params={}, ok=True, error_code=None, duration_ms=1)
    logger.write(actor="dsh", tool="b", params={}, ok=False, error_code="not_found", duration_ms=2)
    records = _read_audit(tmp_path / "audit.jsonl")
    assert [record["tool"] for record in records] == ["a", "b"]


def test_audit_write_failure_does_not_raise(tmp_path) -> None:
    # 用一个「路径是目录」的位置模拟写失败：只告警不抛异常
    logger = AuditLogger(tmp_path)
    logger.write(actor="dsh", tool="a", params={}, ok=True, error_code=None, duration_ms=1)
