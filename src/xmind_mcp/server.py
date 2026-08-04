"""MCP Server：通过 stdio 与 MCP 客户端（如 Trae、Claude Desktop）通信。

使用 mcp 2.x 的 low-level Server API：
- add_request_handler 注册 tools/list 和 tools/call
- 通过 mcp.server.stdio.stdio_server() 提供 stdio 传输
"""

from __future__ import annotations

import asyncio
import atexit
import signal
import sys

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions

from .config import Config
from .tools import XMindTools, build_tool_list
from . import __version__


def _log(msg: str) -> None:
    """输出到 stderr，避免污染 MCP 的 stdio 通信。"""
    print(f"[xmind-mcp] {msg}", file=sys.stderr, flush=True)


def _safe_unload(tools: XMindTools, label: str) -> None:
    """同步释放资源，所有清理路径（finally/atexit/signal）共用。

    幂等：重复调用安全。绝不能抛异常 —— 清理路径上抛异常会
    阻断后续清理或导致进程无法退出。
    """
    try:
        _log(f"触发卸载 ({label})")
        tools.close()
        _log(f"卸载完成 ({label})")
    except Exception as e:
        _log(f"卸载失败 ({label}): {type(e).__name__}: {e}")


def _install_signal_handlers(tools: XMindTools) -> None:
    """注册信号处理，覆盖常见强杀场景。

    - SIGINT:  Ctrl+C
    - SIGTERM: Unix 软终止（部分 MCP 客户端用这个）
    - SIGBREAK: Windows Ctrl+Break（taskkill 不带 /F 会发这个）

    注意：taskkill /F 或 kill -9 会直接终止进程，Python 抓不到
    任何信号，此时由 OS / 显卡驱动自动回收资源（进程死亡必然释放）。
    """
    def _handler(signum, frame):
        sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        _safe_unload(tools, f"signal:{sig_name}")
        # 退出码 0 表示正常退出，避免 MCP 客户端把退出当作崩溃重试
        sys.exit(0)

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # 非主线程或平台不支持时跳过
            pass


async def run_server(config: Config) -> None:
    """启动 MCP Server。"""
    server = Server("xmind-mcp-server")
    tools = XMindTools(config)
    tool_list = build_tool_list()

    # 注册兜底清理：进程正常退出（含 sys.exit）时触发
    atexit.register(_safe_unload, tools, "atexit")

    # 注册信号处理：覆盖 SIGINT/SIGTERM/SIGBREAK
    _install_signal_handlers(tools)

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
        server_version=__version__,
        capabilities=types.ServerCapabilities(
            tools=types.ToolsCapability(),
        ),
    )

    try:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, init_options)
    finally:
        # 优雅关闭：MCP 客户端断开 stdin 或 server.run 正常返回
        _safe_unload(tools, "finally")
        # atexit 已注册过，但 finally 已经清理完，注销避免重复执行
        atexit.unregister(_safe_unload)
