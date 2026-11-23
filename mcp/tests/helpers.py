# SPDX-License-Identifier: MIT
"""测试小工具。"""

from __future__ import annotations

import json
from typing import Any

from mcp import Client


async def call_tool(client: Client, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """调用工具并解析 JSON 返回；工具返回 is_error 时断言失败。"""
    result = await client.call_tool(name, arguments or {})
    assert not result.is_error, f"工具 {name} 返回错误：{result.content}"
    assert result.content, f"工具 {name} 没有返回内容"
    text = result.content[0].text
    parsed = json.loads(text)
    assert isinstance(parsed, dict), f"工具 {name} 返回的不是 JSON 对象：{text[:200]}"
    return parsed


async def call_tool_raw(client: Client, name: str, arguments: dict[str, Any] | None = None):
    """原样返回 CallToolResult（用于断言参数校验错误等）。"""
    return await client.call_tool(name, arguments or {})


__all__ = ["call_tool", "call_tool_raw"]
