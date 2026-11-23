# SPDX-License-Identifier: MIT
"""MCP 服务端契约：8 个只读 + 8 个写侧工具、schema、untrusted 声明。"""

from __future__ import annotations

from helpers import call_tool

READ_TOOLS = {
    "library.search",
    "evidence.get",
    "figure.get",
    "table.get",
    "cite.resolve",
    "collection.tree",
    "summary.get",
    "dataset.golden",
}
WRITE_TOOLS = {
    "proposal.create",
    "proposal.list",
    "proposal.get",
    "task.list",
    "task.claim",
    "task.heartbeat",
    "task.complete",
    "task.fail",
}
EXPECTED_TOOLS = READ_TOOLS | WRITE_TOOLS
# 写侧里两个「只看不改」工具仍标 readOnly（但调用它们需要 agent-propose scope）
READ_ONLY_WRITE_TOOLS = {"proposal.list", "proposal.get", "task.list"}
MUTATING_TOOLS = WRITE_TOOLS - READ_ONLY_WRITE_TOOLS


async def test_all_tools_registered(mcp) -> None:
    listed = await mcp.list_tools()
    names = {tool.name for tool in listed.tools}
    assert names == EXPECTED_TOOLS
    assert len(listed.tools) == 16
    for tool in listed.tools:
        assert tool.annotations is not None, f"{tool.name} 缺少 annotations"
        if tool.name in MUTATING_TOOLS:
            assert tool.annotations.read_only_hint is False, f"{tool.name} 不该标 readOnly"
        else:
            assert tool.annotations.read_only_hint is True, f"{tool.name} 不是 readOnly"
        assert tool.annotations.destructive_hint is False
        assert tool.description, f"{tool.name} 缺少 description"


async def test_tool_descriptions_warn_about_untrusted_content(mcp) -> None:
    listed = await mcp.list_tools()
    for tool in listed.tools:
        description = tool.description or ""
        assert "不可信" in description or "untrusted" in description.lower(), tool.name


async def test_server_instructions_declare_untrusted_and_read_only(mcp) -> None:
    instructions = mcp.instructions or ""
    assert "不可信" in instructions
    assert "只读" in instructions
    assert "cite_key" in instructions


async def test_input_schemas(mcp) -> None:
    listed = {tool.name: tool for tool in (await mcp.list_tools()).tools}
    library = listed["library.search"].input_schema
    assert library["required"] == ["query"]
    assert library["properties"]["limit"]["default"] == 20
    assert library["properties"]["limit"]["maximum"] == 100

    evidence = listed["evidence.get"].input_schema
    assert evidence["required"] == ["ref"]
    assert evidence["properties"]["limit"]["default"] == 8

    figure = listed["figure.get"].input_schema
    assert not figure.get("required")
    assert set(figure["properties"]) == {"ref", "paper", "label"}

    table = listed["table.get"].input_schema
    assert table["required"] == ["paper"]

    cite = listed["cite.resolve"].input_schema
    assert cite["required"] == ["claim_text", "candidate_papers"]
    assert cite["properties"]["candidate_papers"]["minItems"] == 1
    assert cite["properties"]["candidate_papers"]["maxItems"] == 20

    summary = listed["summary.get"].input_schema
    assert summary["required"] == ["paper"]
    assert "section_key" in summary["properties"]

    golden = listed["dataset.golden"].input_schema
    assert golden["properties"]["include_pending"]["default"] is False


async def test_unknown_tool_is_not_silently_ignored(mcp) -> None:
    result = await mcp.call_tool("library.write", {"query": "x"})
    assert result.is_error is True
    assert "Unknown tool" in (result.content[0].text or "")


async def test_collection_tree_call_via_mcp(mcp) -> None:
    data = await call_tool(mcp, "collection.tree")
    assert data["ok"] is True
    assert data["count"] == 3
