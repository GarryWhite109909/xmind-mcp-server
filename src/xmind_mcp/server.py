"""MCP Server：通过 stdio 与 MCP 客户端（如 Trae、Claude Desktop）通信。

使用 mcp 2.x 的 low-level Server API：
- add_request_handler 注册 tools/list 和 tools/call
- 通过 mcp.server.stdio.stdio_server() 提供 stdio 传输
"""

from __future__ import annotations

import asyncio

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions

from .config import Config
from .tools import XMindTools, build_tool_list


async def run_server(config: Config) -> None:
    """启动 MCP Server。"""
    server = Server("xmind-mcp-server")
    tools = XMindTools(config)
    tool_list = build_tool_list()

    async def handle_list_tools(
        ctx,
        params: types.PaginatedRequestParams,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tool_list)

    async def handle_call_tool(
        ctx,
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        try:
            messages = await tools.call_tool(params.name, params.arguments or {})
            return types.CallToolResult(content=messages)
        except Exception as exc:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=f"执行失败: {type(exc).__name__}: {exc}",
                    )
                ],
                isError=True,
            )

    server.add_request_handler("tools/list", types.PaginatedRequestParams, handle_list_tools)
    server.add_request_handler("tools/call", types.CallToolRequestParams, handle_call_tool)

    init_options = InitializationOptions(
        server_name="xmind-mcp-server",
        server_version="0.1.0",
        capabilities=types.ServerCapabilities(
            tools=types.ToolsCapability(),
        ),
    )

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)
