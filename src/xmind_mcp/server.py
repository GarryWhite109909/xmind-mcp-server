"""MCP Server：通过 stdio 与 MCP 客户端（如 Trae、Claude Desktop）通信。"""

from __future__ import annotations

import asyncio
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server

from .config import Config
from .tools import XMindTools, build_tool_list


async def run_server(config: Config) -> None:
    """启动 MCP Server。"""
    server = Server("xmind-mcp-server")
    tools = XMindTools(config)

    @server.list_tools()
    async def handle_list_tools():
        return build_tool_list()

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict):
        return await tools.call_tool(name, arguments or {})

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
