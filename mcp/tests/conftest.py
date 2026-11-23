# SPDX-License-Identifier: MIT
"""pytest fixtures：假 API + in-process MCP 客户端（不依赖 8000 端口/真实数据）。

in-process Client 必须在**同一个 asyncio Task** 里 enter/exit（anyio cancel scope 的限制），
所以不用 async generator fixture 持有 Client，而是每次调用现开现关（进程内握手，开销可忽略）。
"""

from __future__ import annotations

from typing import Any

import pytest
from fake_api import FakeAPI, default_library
from mcp import Client

from litbench_mcp.api import LitBenchAPI
from litbench_mcp.audit import AuditLogger
from litbench_mcp.auth import SlidingWindowRateLimiter, TokenStore
from litbench_mcp.config import Settings
from litbench_mcp.server import create_server
from litbench_mcp.tools import ToolDeps


@pytest.fixture
def settings(tmp_path) -> Settings:
    """测试配置：状态目录在 tmp，限流放宽到 1000 次避免干扰普通用例。"""
    return Settings(
        api_base="http://fake-api",
        state_dir=tmp_path / "state",
        rate_limit=1000,
        rate_window_s=60.0,
        api_timeout_s=5.0,
        log_level="WARNING",
    )


@pytest.fixture
def fake() -> FakeAPI:
    return FakeAPI(default_library())


@pytest.fixture
async def api(fake: FakeAPI):
    client = LitBenchAPI("http://fake-api", timeout_s=5.0, transport=fake.transport())
    yield client
    await client.aclose()


@pytest.fixture
def deps(settings: Settings, api: LitBenchAPI) -> ToolDeps:
    return ToolDeps(
        api=api,
        settings=settings,
        audit=AuditLogger(settings.audit_path),
        limiter=SlidingWindowRateLimiter(limit=settings.rate_limit, window_s=settings.rate_window_s),
    )


class InProcessMCP:
    """每次调用现开一个 in-process MCP Client（同一 Task 内 enter/exit）。"""

    def __init__(self, server) -> None:
        self._server = server

    @property
    def instructions(self) -> str | None:
        return self._server.instructions

    async def list_tools(self):
        async with Client(self._server) as client:
            return await client.list_tools()

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None):
        async with Client(self._server) as client:
            return await client.call_tool(name, arguments or {})


@pytest.fixture
def mcp(settings: Settings, api: LitBenchAPI) -> InProcessMCP:
    return InProcessMCP(create_server(settings, api=api))


@pytest.fixture
def token_store(settings: Settings) -> TokenStore:
    return TokenStore(settings.tokens_path)
