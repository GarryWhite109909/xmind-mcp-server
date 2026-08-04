"""MCP Server 集成测试：通过 stdio 与启动的 server 进程通信。"""

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def server_process():
    """启动 xmind-mcp server 子进程。"""
    # 直接启动 run_server，避免依赖用户机器上的真实配置
    # （否则配置里写着 cuda 但实际没有 CUDA provider 时，CLI 会拒绝启动）。
    code = (
        "import asyncio;"
        "from xmind_mcp.config import Config;"
        "from xmind_mcp.server import run_server;"
        "asyncio.run(run_server(Config(ocr_engine='rapidocr', device='cpu', initialized=True)))"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    yield proc
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _send(proc: subprocess.Popen, msg: dict) -> None:
    line = json.dumps(msg, ensure_ascii=False) + "\n"
    assert proc.stdin is not None
    proc.stdin.write(line)
    proc.stdin.flush()


def _recv(proc: subprocess.Popen) -> dict:
    assert proc.stdout is not None
    line = proc.stdout.readline()
    assert line, "server 没有返回数据"
    return json.loads(line)


def test_server_initialize_and_list_tools(server_process: subprocess.Popen):
    _send(server_process, {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1.0"},
        },
    })

    init_resp = _recv(server_process)
    assert init_resp["id"] == 1
    assert init_resp["result"]["serverInfo"]["name"] == "xmind-mcp-server"
    assert "tools" in init_resp["result"]["capabilities"]

    _send(server_process, {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    })

    list_resp = _recv(server_process)
    assert list_resp["id"] == 2
    tools = list_resp["result"]["tools"]
    assert len(tools) == 55
    assert any(t["name"] == "read_structure" for t in tools)
    assert any(t["name"] == "find_node" for t in tools)
    assert any(t["name"] == "export_markdown" for t in tools)
    assert any(t["name"] == "set_node_style" for t in tools)
    assert any(t["name"] == "duplicate_node" for t in tools)
    assert any(t["name"] == "add_nodes" for t in tools)
    assert any(t["name"] == "create_file" for t in tools)
    assert any(t["name"] == "import_markdown" for t in tools)
    assert any(t["name"] == "get_node" for t in tools)
    assert any(t["name"] == "apply_to_matching" for t in tools)
    assert any(t["name"] == "analyze_file" for t in tools)
    assert any(t["name"] == "set_sheet_layout" for t in tools)
    assert any(t["name"] == "add_image" for t in tools)
    assert any(t["name"] == "add_summary" for t in tools)
    assert any(t["name"] == "add_boundary" for t in tools)
    assert any(t["name"] == "add_relationship" for t in tools)
    assert any(t["name"] == "apply_style_preset" for t in tools)
