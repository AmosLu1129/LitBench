# SPDX-License-Identifier: MIT
"""限流：滑动窗口、per-actor、超限 MCP 错误 rate_limited。"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from helpers import call_tool
from mcp import Client
from mcp.shared.exceptions import MCPError

from litbench_mcp.auth import (
    RATE_LIMITED_CODE,
    RateLimitedError,
    SlidingWindowRateLimiter,
)
from litbench_mcp.server import create_server


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_sliding_window_allows_limit_then_blocks() -> None:
    clock = _Clock()
    limiter = SlidingWindowRateLimiter(limit=2, window_s=10.0, clock=clock)
    limiter.check("dsh")
    limiter.check("dsh")
    assert limiter.remaining("dsh") == 0

    with pytest.raises(RateLimitedError) as exc_info:
        limiter.check("dsh")
    error = exc_info.value
    assert error.code == RATE_LIMITED_CODE
    assert error.data["error_code"] == "rate_limited"
    assert error.data["actor"] == "dsh"
    assert 0 < error.data["retry_after_s"] <= 10.0
    assert "rate_limited" in str(error)

    # 窗口过期后恢复
    clock.advance(10.1)
    limiter.check("dsh")
    assert limiter.remaining("dsh") == 1


def test_rate_limit_is_per_actor() -> None:
    limiter = SlidingWindowRateLimiter(limit=1, window_s=60.0, clock=_Clock())
    limiter.check("dsh")
    limiter.check("claude")  # 另一个 actor 不受影响
    with pytest.raises(RateLimitedError):
        limiter.check("dsh")


async def test_rate_limited_tool_returns_mcp_error(settings, api) -> None:
    limited = replace(settings, rate_limit=2, rate_window_s=60.0)
    server = create_server(limited, api=api)
    async with Client(server) as client:
        await call_tool(client, "collection.tree")
        await call_tool(client, "collection.tree")
        with pytest.raises(MCPError) as exc_info:
            await client.call_tool("collection.tree", {})
    error = exc_info.value
    assert error.code == RATE_LIMITED_CODE
    assert error.data["error_code"] == "rate_limited"
    assert "rate_limited" in str(error)

    # 超限也要留审计（ok=false / error_code=rate_limited）
    lines = limited.audit_path.read_text(encoding="utf-8").strip().splitlines()
    records = [json.loads(line) for line in lines]
    assert records[-1]["tool"] == "collection.tree"
    assert records[-1]["ok"] is False
    assert records[-1]["error_code"] == "rate_limited"
    assert len(records) == 3


async def test_step_actor_has_its_own_bucket(settings, api) -> None:
    """stdio actor 与 token actor 分桶（这里直接验证 limiter key 行为）。"""
    limiter = SlidingWindowRateLimiter(limit=1, window_s=60.0, clock=_Clock())
    limiter.check("stdio")
    limiter.check("dsh")
    with pytest.raises(RateLimitedError):
        limiter.check("stdio")
