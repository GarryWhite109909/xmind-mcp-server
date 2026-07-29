"""MCP Server 集成测试：通过 stdio 与启动的 server 进程通信。"""

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def server_process():
    """启动 xmind-mcp server 子进程。"""
    proc = subprocess.Popen(
        [sys.executable, "-m", "xmind_mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
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
    assert len(tools) == 10
    assert any(t["name"] == "read_structure" for t in tools)
