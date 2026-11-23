# SPDX-License-Identifier: MIT
"""M5 写侧工具：proposal.* / task.*（stdio 视为已授权）。

覆盖：
1. 工具注册与 schema（proposal.create 必须带证据）；
2. 投递提案 → 列提案 → 取提案；
3. 领任务 → 心跳 → 完成；失败可重排；
4. 任务状态机冲突返回 ``conflict`` 而不是裸异常；
5. 写 token 的 scope 签发（agent-read,agent-propose）。
"""

from __future__ import annotations

import pytest
from fake_api import (
    BLOCK_ATTN,
    FIG_ATTENTION_1,
    PROPOSAL_ID,
    TASK_FAILED,
    TASK_QUEUED,
)
from helpers import call_tool

from litbench_mcp.auth import TokenError, TokenStore


async def test_proposal_tools_registered(mcp) -> None:
    listed = {tool.name: tool for tool in (await mcp.list_tools()).tools}
    create = listed["proposal.create"].input_schema
    assert create["required"] == ["figure_id", "proposed", "evidence_block_ids"]
    assert create["properties"]["evidence_block_ids"]["minItems"] == 1
    assert create["properties"]["confidence"]["maximum"] == 1.0
    assert listed["proposal.create"].annotations.read_only_hint is False

    claim = listed["task.claim"].input_schema
    assert claim["required"] == ["task_id"]
    assert claim["properties"]["lease_seconds"]["minimum"] == 30


async def test_proposal_create_list_get(mcp) -> None:
    created = await call_tool(
        mcp,
        "proposal.create",
        {
            "figure_id": FIG_ATTENTION_1,
            "proposed": {
                "label_ok": False,
                "suggested_label": "Figure 1",
                "caption_issue": "none",
                "crop_issue": "none",
                "confidence": 0.96,
            },
            "evidence_block_ids": [BLOCK_ATTN],
            "confidence": 0.96,
            "note": "自注意力图号",
        },
    )
    assert created["ok"] is True
    proposal = created["proposal"]
    assert proposal["status"] == "pending"
    assert proposal["provider"] == "external"
    assert proposal["evidence"][0]["block_id"] == BLOCK_ATTN

    listed = await call_tool(mcp, "proposal.list", {"status": "pending"})
    assert listed["ok"] is True
    assert listed["count"] >= 1
    assert any(item["proposal_id"] == PROPOSAL_ID for item in listed["items"])

    fetched = await call_tool(mcp, "proposal.get", {"proposal_id": PROPOSAL_ID})
    assert fetched["ok"] is True
    assert fetched["proposal"]["proposal_id"] == PROPOSAL_ID


async def test_proposal_create_requires_evidence(mcp) -> None:
    result = await mcp.call_tool(
        "proposal.create",
        {
            "figure_id": FIG_ATTENTION_1,
            "proposed": {"label_ok": False, "suggested_label": "Figure 1"},
            "evidence_block_ids": [],
        },
    )
    assert result.is_error is True, "空证据必须在 schema 层被拒"


async def test_task_lifecycle(mcp) -> None:
    listed = await call_tool(mcp, "task.list", {"status": "queued"})
    assert listed["ok"] is True
    assert any(item["id"] == TASK_QUEUED for item in listed["items"])

    claimed = await call_tool(mcp, "task.claim", {"task_id": TASK_QUEUED})
    assert claimed["ok"] is True
    task = claimed["task"]
    assert task["status"] == "running"
    assert task["actor"] == "stdio"
    assert task["attempts"] == 1

    heart = await call_tool(mcp, "task.heartbeat", {"task_id": TASK_QUEUED})
    assert heart["ok"] is True
    assert heart["task"]["status"] == "running"

    done = await call_tool(
        mcp,
        "task.complete",
        {"task_id": TASK_QUEUED, "result": {"proposal_id": PROPOSAL_ID}, "cost": 0.0012},
    )
    assert done["ok"] is True
    assert done["task"]["status"] == "done"
    assert done["task"]["result"] == {"proposal_id": PROPOSAL_ID}
    assert done["task"]["cost"] == pytest.approx(0.0012)


async def test_task_conflict_is_readable(mcp) -> None:
    # 直接领一个 failed 任务：服务端 409 → 工具层 conflict（不是裸异常）
    data = await call_tool(mcp, "task.claim", {"task_id": TASK_FAILED})
    assert data["ok"] is False
    assert data["error_code"] == "conflict"


async def test_task_fail_without_retry(mcp) -> None:
    await call_tool(mcp, "task.claim", {"task_id": TASK_QUEUED})
    failed = await call_tool(
        mcp,
        "task.fail",
        {"task_id": TASK_QUEUED, "error": "模型超时", "retry": False},
    )
    assert failed["ok"] is True
    assert failed["task"]["status"] == "failed"
    assert failed["task"]["error"] == "模型超时"


async def test_multi_scope_token_issue(tmp_path) -> None:
    store = TokenStore(tmp_path / "tokens.json")
    issued = store.issue(scope="agent-read,agent-propose", label="writer")
    assert issued.record.scopes == ("agent-read", "agent-propose")
    verified = await store.verify_token(issued.token)
    assert verified is not None
    assert set(verified.scopes) == {"agent-read", "agent-propose"}

    with pytest.raises(TokenError):
        store.issue(scope="agent-read,nope", label="bad")


async def _http_proposal_call(settings, api, token_store, token: str):
    """用真实 Streamable HTTP 客户端调 proposal.create，返回工具 JSON。"""
    httpx2 = pytest.importorskip("httpx2")
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    from litbench_mcp.server import create_http_app, create_server

    server = create_server(settings, api=api, token_store=token_store)
    app = create_http_app(server, settings)
    async with app.router.lifespan_context(app):
        http_client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=settings.http_base_url,
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            transport = streamable_http_client(settings.http_mcp_url, http_client=http_client)
            async with Client(transport) as client:
                result = await client.call_tool(
                    "proposal.create",
                    {
                        "figure_id": FIG_ATTENTION_1,
                        "proposed": {"label_ok": False, "suggested_label": "Figure 1"},
                        "evidence_block_ids": [BLOCK_ATTN],
                        "confidence": 0.96,
                    },
                )
                return result
        finally:
            await http_client.aclose()


async def test_http_write_scope_required(settings, api, token_store) -> None:
    import json

    reader = token_store.issue(scope="agent-read", label="reader")
    result = await _http_proposal_call(settings, api, token_store, reader.token)
    assert result.is_error is False
    data = json.loads(result.content[0].text)
    assert data["ok"] is False
    assert data["error_code"] == "insufficient_scope"

    writer = token_store.issue(scope="agent-read,agent-propose", label="writer")
    result = await _http_proposal_call(settings, api, token_store, writer.token)
    assert result.is_error is False
    data = json.loads(result.content[0].text)
    assert data["ok"] is True
    assert data["proposal"]["status"] == "pending"
