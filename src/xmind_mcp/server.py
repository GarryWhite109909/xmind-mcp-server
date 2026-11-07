"""MCP Server：通过 stdio 与 MCP 客户端（如 Trae、Claude Desktop）通信。

使用 mcp 2.x 的 low-level Server API：
- add_request_handler 注册 tools/list 和 tools/call
- 通过 mcp.server.stdio.stdio_server() 提供 stdio 传输
"""

from __future__ import annotations

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions

from . import __version__
from .config import Config
from .tools import XMindTools
from .tool_schemas import build_tool_list


async def run_server(config: Config) -> None:
    """启动 MCP Server。

    工具层不持有需要显式释放的资源（图片直接读文件，不开常驻模型），
    因此进程退出时不需要 atexit / 信号处理。
    """
    server = Server("xmind-mcp-server")
    tools = XMindTools(config)
    tool_list = build_tool_list(config.toolset)

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
            messages, is_error = await tools.call_tool_status(params.name, params.arguments or {})
            return types.CallToolResult(content=messages, isError=is_error)
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
        server_version=__version__,
        capabilities=types.ServerCapabilities(
            tools=types.ToolsCapability(),
        ),
    )

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)
