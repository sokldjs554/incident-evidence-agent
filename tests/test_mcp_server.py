"""MCP 서버가 도구를 올바르게 노출하는지 검증한다 (stdio 실행 없이)."""

import asyncio
import json

import pytest

pytest.importorskip("mcp")

from fivewhys.mcp_server.server import create_server


def test_server_exposes_expected_tools():
    server = create_server("db_pool_exhaustion", seed=42)
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert {
        "get_error_timeline",
        "get_anomalous_templates",
        "search_logs",
        "get_log_context",
        "list_change_events",
        "search_past_incidents",
        "get_incident_info",
    } <= names


def test_tool_call_returns_json():
    server = create_server("db_pool_exhaustion", seed=42)
    result = asyncio.run(server.call_tool("get_incident_info", {}))
    # mcp 2.x: CallToolResult(content=[...]) / mcp 1.x FastMCP: (blocks, raw)
    if hasattr(result, "content"):
        blocks = result.content
    elif isinstance(result, tuple):
        blocks = result[0]
    else:
        blocks = result
    data = json.loads(blocks[0].text)
    assert data["services"]
    assert "incident" in data
