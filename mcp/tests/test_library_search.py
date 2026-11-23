# SPDX-License-Identifier: MIT
"""library.search：标题/cite_key 检索、集合/收藏夹过滤、图注降级兜底。"""

from __future__ import annotations

from fake_api import PAPER_ATTENTION, PAPER_COAL, PAPER_FALLBACK
from helpers import call_tool


async def test_search_by_title(mcp) -> None:
    data = await call_tool(mcp, "library.search", {"query": "pyrolysis"})
    assert data["ok"] is True
    assert data["degraded"] is False
    assert data["count"] == 1
    item = data["items"][0]
    assert item["paper_id"] == PAPER_COAL
    assert item["why"] == "title match"
    assert item["cite_key"] == "wang2019coal"
    assert item["has_version"] is True
    assert {"paper_id", "title", "year", "venue", "figure_count", "table_count"} <= set(item)


async def test_search_by_cite_key(mcp) -> None:
    data = await call_tool(mcp, "library.search", {"query": "vaswani2017attention"})
    assert [item["paper_id"] for item in data["items"]] == [PAPER_ATTENTION]
    assert data["items"][0]["why"] == "citekey match"
    assert data["items"][0]["figure_count"] == 2
    assert data["items"][0]["table_count"] == 1


async def test_search_collection_and_favorite_filters(mcp) -> None:
    by_collection = await call_tool(
        mcp, "library.search", {"query": "coal", "collection_id": "c3" + "c3" * 15}
    )
    assert [item["paper_id"] for item in by_collection["items"]] == [PAPER_COAL]

    # 集合里没有 attention 这篇 → 主查询空 + 图注兜底也空
    intersection = await call_tool(
        mcp, "library.search", {"query": "attention", "collection_id": "c3" + "c3" * 15}
    )
    assert intersection["items"] == []
    assert intersection["degraded"] is True


async def test_search_respects_limit(mcp) -> None:
    data = await call_tool(mcp, "library.search", {"query": "", "limit": 2})
    assert data["count"] == 2
    assert len(data["items"]) == 2


async def test_search_splits_natural_language_query_into_tokens(mcp) -> None:
    """整句 LIKE 搜不到时按关键词拆分检索（M4 的 query 是自然语言，不是标题片段）。

    服务端 ``GET /api/papers?q=`` 只做子串匹配；agent 侧必须能把长 query 拆开，
    否则「查询证据」这个入口在真实库里几乎永远 0 命中。
    """
    data = await call_tool(
        mcp, "library.search", {"query": "coal pyrolysis kinetics experiment details"}
    )
    assert data["ok"] is True
    assert data["degraded"] is False
    assert [item["paper_id"] for item in data["items"]] == [PAPER_COAL]
    assert data["items"][0]["why"].startswith("token match:")
    assert "关键词" in (data["hint"] or "")


async def test_search_falls_back_to_figure_captions(mcp) -> None:
    """标题/cite_key 搜不到时，用图注/标题的降级检索补文献并标 degraded。"""
    data = await call_tool(mcp, "library.search", {"query": "softmax scaling"})
    assert data["degraded"] is True
    assert [item["paper_id"] for item in data["items"]] == [PAPER_FALLBACK]
    assert data["items"][0]["why"] == "figure caption match"
    assert data["items"][0]["title"] == "Numerical stability notes"


async def test_search_no_hits_is_empty_not_error(mcp) -> None:
    data = await call_tool(mcp, "library.search", {"query": "zzzz-not-in-library"})
    assert data["ok"] is True
    assert data["items"] == []
    assert data["count"] == 0
    assert data["degraded"] is True
    assert "没有命中" in (data["hint"] or "")


async def test_search_upstream_500_returns_error_code(mcp, fake) -> None:
    fake.failures[("GET", "/api/papers")] = 500
    data = await call_tool(mcp, "library.search", {"query": "coal"})
    assert data["ok"] is False
    assert data["error_code"] == "upstream_error"
    assert "500" in data["error"]


async def test_search_connection_error_returns_upstream_error(settings, tmp_path) -> None:
    """连不上 API 时也给可读错误，而不是抛异常。"""
    from mcp import Client

    from litbench_mcp.api import LitBenchAPI
    from litbench_mcp.server import create_server

    broken = LitBenchAPI("http://127.0.0.1:1", timeout_s=0.5)
    try:
        server = create_server(settings, api=broken)
        async with Client(server) as client:
            data = await call_tool(client, "library.search", {"query": "coal"})
        assert data["ok"] is False
        assert data["error_code"] == "upstream_error"
        assert "连不上" in data["error"]
    finally:
        await broken.aclose()


async def test_invalid_limit_is_schema_error(mcp) -> None:
    result = await mcp.call_tool("library.search", {"query": "coal", "limit": 500})
    assert result.is_error is True
