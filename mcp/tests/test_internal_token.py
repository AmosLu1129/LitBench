# SPDX-License-Identifier: MIT
"""MCP → API 的内部服务令牌（M7 第三步，docs/17 §11.1）。

认证打开后 MCP 调 ``/api/*`` 会被 401；MCP 侧要做的是把
``LITBENCH_MCP_API_TOKEN`` 放进 ``X-LitBench-Internal-Token`` 请求头。
这里用 httpx.MockTransport 抓实际发出的头，不依赖真实 API。
"""

from __future__ import annotations

import httpx

from litbench_mcp.api import LitBenchAPI


def _recording_transport(seen: dict[str, str | None]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get("x-litbench-internal-token")
        return httpx.Response(200, json=[])

    return httpx.MockTransport(handler)


async def test_api_client_sends_internal_token() -> None:
    seen: dict[str, str | None] = {}
    async with LitBenchAPI(
        "http://litbench.test",
        api_token="internal-token-123456",
        transport=_recording_transport(seen),
    ) as api:
        await api.list_papers()
    assert seen["token"] == "internal-token-123456"


async def test_api_client_without_token_omits_header() -> None:
    seen: dict[str, str | None] = {}
    async with LitBenchAPI(
        "http://litbench.test",
        transport=_recording_transport(seen),
    ) as api:
        await api.list_papers()
    assert seen["token"] is None
