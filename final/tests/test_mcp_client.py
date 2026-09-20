"""MCP 协议客户端（mcp_client.py）与 register_mcp_server 的行为测试。

用线程内假 MCP 服务器覆盖：initialize 握手、tools/list 发现、tools/call
调用、isError/协议错误语义、SSRF 校验和重名跳过。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from internal.agent.agent import UnifiedAgent
from internal.tools.mcp_client import (
    McpProtocolError,
    call_remote_tool,
    initialize_session,
    list_remote_tools,
)
from internal.tools.tools import Tool, ToolCallContext, ToolExecutor


class FakeMCPHandler(BaseHTTPRequestHandler):
    """按 method 分发的最小 MCP Streamable HTTP 服务器。"""

    server_version = "FakeMCP/0.1"

    def log_message(self, *args):  # 静默访问日志
        return

    def _send_json(self, status: int, payload: dict | None, extra_headers: dict | None = None):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            message = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send_json(400, {"error": "bad json"})
        method = message.get("method")
        request_id = message.get("id")

        # 模拟 FastMCP 的强制会话校验：initialize 之外的请求必须回传 session。
        if method == "initialize":
            return self._send_json(200, {
                "jsonrpc": "2.0", "id": request_id,
                "result": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-mcp", "version": "0.1"},
                },
            }, extra_headers={"Mcp-Session-Id": "fake-session-123"})
        if method != "notifications/initialized" and self.headers.get("Mcp-Session-Id") != "fake-session-123":
            return self._send_json(400, {
                "jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32001, "message": "missing or invalid session"},
            })
        if method == "notifications/initialized":
            return self._send_json(202, None)
        if method == "tools/list":
            return self._send_json(200, {
                "jsonrpc": "2.0", "id": request_id,
                "result": {"tools": [{
                    "name": "echo",
                    "description": "回显输入文本",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string", "description": "要回显的内容"}},
                        "required": ["text"],
                    },
                }]},
            })
        if method == "tools/call":
            arguments = (message.get("params") or {}).get("arguments") or {}
            text = str(arguments.get("text") or "")
            if text == "boom":
                return self._send_json(200, {
                    "jsonrpc": "2.0", "id": request_id,
                    "result": {"content": [{"type": "text", "text": "模拟失败"}], "isError": True},
                })
            if text == "rpcerror":
                return self._send_json(200, {
                    "jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32000, "message": "远端内部错误"},
                })
            return self._send_json(200, {
                "jsonrpc": "2.0", "id": request_id,
                "result": {"content": [{"type": "text", "text": f"echo: {text}"}], "isError": False},
            })
        return self._send_json(200, {
            "jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32601, "message": f"unknown method {method}"},
        })


@pytest.fixture()
def mcp_server(monkeypatch):
    """起本地假 MCP 服务器，并放开 loopback SSRF 限制（仅测试进程内）。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMCPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("AGI_ALLOW_PRIVATE_MCP_ENDPOINTS", "1")
    yield f"http://127.0.0.1:{server.server_address[1]}/mcp"
    server.shutdown()
    server.server_close()


def make_agent() -> SimpleNamespace:
    executor = ToolExecutor()
    return SimpleNamespace(tool_executor=executor, add_tool=executor.add_tool)


def test_initialize_and_list(mcp_server):
    server_info = initialize_session(mcp_server)
    assert server_info["name"] == "fake-mcp"
    tools = list_remote_tools(mcp_server)
    assert [t["name"] for t in tools] == ["echo"]
    assert tools[0]["inputSchema"]["required"] == ["text"]


def test_call_remote_tool_success(mcp_server):
    initialize_session(mcp_server)  # 真实顺序：先握手建立会话，再调用
    text, is_error = call_remote_tool(mcp_server, "echo", {"text": "你好"}, 5)
    assert text == "echo: 你好"
    assert is_error is False


def test_call_remote_tool_is_error(mcp_server):
    initialize_session(mcp_server)
    text, is_error = call_remote_tool(mcp_server, "echo", {"text": "boom"}, 5)
    assert is_error is True
    assert text == "模拟失败"


def test_call_remote_tool_rpc_error(mcp_server):
    initialize_session(mcp_server)
    with pytest.raises(McpProtocolError):
        call_remote_tool(mcp_server, "echo", {"text": "rpcerror"}, 5)


def test_register_mcp_server_discovers_and_registers(mcp_server):
    fake = make_agent()
    result = UnifiedAgent.register_mcp_server(fake, mcp_server)
    assert result["registered"] == ["mcp_echo"]
    assert result["skipped"] == []
    tool = fake.tool_executor.snapshot()["mcp_echo"]
    assert tool.is_mcp is True
    assert tool.params[0]["name"] == "text"
    assert tool.params[0]["required"] == "true"


def test_registered_tool_executes_through_executor(mcp_server):
    fake = make_agent()
    UnifiedAgent.register_mcp_server(fake, mcp_server)
    tool = fake.tool_executor.snapshot()["mcp_echo"]
    result = tool.execute_structured(ToolCallContext(), {"text": "hello"})
    assert result.success is True
    assert result.payload == "echo: hello"
    assert result.metadata["backend"] == "mcp"


def test_registered_tool_maps_is_error_to_tool_error(mcp_server):
    fake = make_agent()
    UnifiedAgent.register_mcp_server(fake, mcp_server)
    tool = fake.tool_executor.snapshot()["mcp_echo"]
    result = tool.execute_structured(ToolCallContext(), {"text": "boom"})
    assert result.success is False
    assert result.error.code == "remote"
    assert result.error.retryable is False


def test_duplicate_names_are_skipped(mcp_server):
    fake = make_agent()
    fake.add_tool(Tool(name="mcp_echo", description="占位", params=[], func=lambda _: "x"))
    result = UnifiedAgent.register_mcp_server(fake, mcp_server)
    assert result["registered"] == []
    assert result["skipped"] == ["mcp_echo"]
    assert fake.tool_executor.snapshot()["mcp_echo"].func({"text": "hi"}) == "x"


def test_ssrf_still_blocks_private_endpoints(monkeypatch):
    # 不放开 AGI_ALLOW_PRIVATE_MCP_ENDPOINTS：回环地址必须在注册期被拒绝。
    monkeypatch.delenv("AGI_ALLOW_PRIVATE_MCP_ENDPOINTS", raising=False)
    fake = make_agent()
    with pytest.raises(ValueError):
        UnifiedAgent.register_mcp_server(fake, "http://127.0.0.1:9/mcp")
