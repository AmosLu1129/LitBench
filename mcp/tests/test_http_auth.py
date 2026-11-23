# SPDX-License-Identifier: MIT
"""Streamable HTTP 传输：Bearer 校验（401/403）、scope、审计 actor、完整调用闭环。"""

from __future__ import annotations

import json

import httpx
import pytest
from helpers import call_tool

from litbench_mcp.auth import READ_SCOPE
from litbench_mcp.server import create_http_app, create_server


def _initialize_payload() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        },
    }


async def _raw_post(app, settings, headers: dict | None = None) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.http_base_url
    ) as client:
        return await client.post(
            settings.mcp_path,
            json=_initialize_payload(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                **(headers or {}),
            },
        )


async def test_http_requires_bearer(settings, api, token_store) -> None:
    server = create_server(settings, api=api, token_store=token_store)
    app = create_http_app(server, settings)

    no_token = await _raw_post(app, settings)
    assert no_token.status_code == 401
    assert no_token.json()["error"] == "invalid_token"

    bad_token = await _raw_post(app, settings, {"Authorization": "Bearer lbmcp_wrong"})
    assert bad_token.status_code == 401


async def test_http_requires_agent_read_scope(settings, api, token_store) -> None:
    issued = token_store.issue(scope="agent-propose", label="proposer")
    server = create_server(settings, api=api, token_store=token_store)
    app = create_http_app(server, settings)

    response = await _raw_post(app, settings, {"Authorization": f"Bearer {issued.token}"})
    assert response.status_code == 403
    body = response.json()
    assert body["error"] == "insufficient_scope"
    assert READ_SCOPE in body["error_description"]


async def test_http_streamable_round_trip_with_bearer(settings, api, token_store) -> None:
    """真走 SDK Streamable HTTP 客户端：Bearer 通过 + 调工具 + 审计 actor = token label。"""
    httpx2 = pytest.importorskip("httpx2")
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    issued = token_store.issue(scope=READ_SCOPE, label="dsh")
    server = create_server(settings, api=api, token_store=token_store)
    app = create_http_app(server, settings)

    async with app.router.lifespan_context(app):
        http_client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=settings.http_base_url,
            headers={"Authorization": f"Bearer {issued.token}"},
        )
        try:
            transport = streamable_http_client(settings.http_mcp_url, http_client=http_client)
            async with Client(transport) as client:
                data = await call_tool(client, "collection.tree")
                assert data["ok"] is True
                assert data["count"] == 3
        finally:
            await http_client.aclose()

    records = [
        json.loads(line)
        for line in settings.audit_path.read_text(encoding="utf-8").strip().splitlines()
    ]
    assert records[-1]["tool"] == "collection.tree"
    assert records[-1]["actor"] == "dsh", "审计 actor 应来自 Bearer token 的 label"


async def test_http_revoked_token_rejected(settings, api, token_store) -> None:
    issued = token_store.issue(scope=READ_SCOPE, label="dsh")
    token_store.revoke("dsh")
    server = create_server(settings, api=api, token_store=token_store)
    app = create_http_app(server, settings)

    response = await _raw_post(app, settings, {"Authorization": f"Bearer {issued.token}"})
    assert response.status_code == 401
