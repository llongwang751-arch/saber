# mcp_client — 最小 MCP（Model Context Protocol）HTTP 客户端。
#
# 在 Streamable HTTP 传输上实现三步核心会话：
#   initialize 握手 → tools/list 工具发现 → tools/call 工具调用
# 响应兼容 application/json 与 text/event-stream（SSE 帧）两种形态。
# 仅支持 http(s) 端点；SSRF 校验由 tools.validate_mcp_endpoint 在注册期负责，
# 本模块不做网络策略。
import itertools
import json
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# 客户端声明的协议版本；宽容模式：不拒绝服务器协商回的旧版本，仅记录。
MCP_PROTOCOL_VERSION = "2025-03-26"
_CLIENT_INFO = {"name": "agi-assistant", "version": "1.0"}
_DISCOVERY_TIMEOUT_SECONDS = 10.0

_ids = itertools.count(1)

# Streamable HTTP 会话表：服务器可在 initialize 响应下发 Mcp-Session-Id，
# 规范要求后续请求必须回传（FastMCP 等实现强制校验）。按 endpoint 记录。
_sessions: Dict[str, str] = {}
_session_lock = threading.Lock()


def _session_for(endpoint: str) -> Optional[str]:
    with _session_lock:
        return _sessions.get(endpoint)


def _remember_session(endpoint: str, session_id: str) -> None:
    with _session_lock:
        _sessions.setdefault(endpoint, session_id)


class McpProtocolError(RuntimeError):
    """JSON-RPC / 传输层错误（远端 error 对象、非协议响应或网络失败）。"""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = bool(retryable)


def _next_id() -> int:
    return next(_ids)


def _rpc_message(method: str, params: Optional[dict], request_id: Optional[int]) -> dict:
    payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if request_id is not None:
        payload["id"] = request_id
    return payload


def _extract_rpc_result(data: Any, endpoint: str) -> Any:
    if not isinstance(data, dict):
        raise McpProtocolError(f"MCP 响应不是 JSON 对象 [{endpoint}]")
    error = data.get("error")
    if error is not None:
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise McpProtocolError(f"MCP 远端返回错误 [{endpoint}]: {message}")
    if "result" not in data:
        raise McpProtocolError(f"MCP 响应缺少 result [{endpoint}]")
    return data["result"]


def _post_rpc(endpoint: str, payload: dict, timeout: float) -> Any:
    """POST 一条 JSON-RPC 消息，兼容 JSON 与 SSE 两种响应形态，返回 result。"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    session_id = _session_for(endpoint)
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    try:
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=timeout)
    except requests.Timeout as exc:
        raise McpProtocolError(f"MCP 请求超时 [{endpoint}]", retryable=True) from exc
    except requests.RequestException as exc:
        raise McpProtocolError(f"MCP 请求失败 [{endpoint}]: {exc}", retryable=True) from exc
    if resp.status_code != 200:
        raise McpProtocolError(
            f"MCP HTTP {resp.status_code} [{endpoint}]", retryable=resp.status_code >= 500
        )
    remote_session = resp.headers.get("Mcp-Session-Id") or ""
    if remote_session:
        _remember_session(endpoint, remote_session)
    content_type = (resp.headers.get("Content-Type") or "").lower()
    if "text/event-stream" in content_type:
        for raw in resp.iter_lines(decode_unicode=True):
            line = (raw or "").strip()
            if not line.startswith("data:"):
                continue
            body = line[len("data:"):].strip()
            if not body or body == "[DONE]":
                continue
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                continue
            # 跳过无 id 的 notification，只取与本请求配对的响应。
            if isinstance(data, dict) and "id" in data:
                return _extract_rpc_result(data, endpoint)
        raise McpProtocolError(f"MCP SSE 流中未收到配对响应 [{endpoint}]")
    try:
        return _extract_rpc_result(resp.json(), endpoint)
    except ValueError as exc:
        raise McpProtocolError(f"MCP 响应不是合法 JSON [{endpoint}]") from exc


def initialize_session(endpoint: str, timeout: float = _DISCOVERY_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """initialize 握手 + initialized 通知。返回 serverInfo（缺失时为空 dict）。"""
    result = _post_rpc(
        endpoint,
        _rpc_message(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
            _next_id(),
        ),
        timeout,
    )
    server_info: Dict[str, Any] = {}
    if isinstance(result, dict) and isinstance(result.get("serverInfo"), dict):
        server_info = dict(result["serverInfo"])
    # notification 无响应体；Streamable HTTP 允许服务器返回空/202。
    # 规范要求 notification 同样回传 Mcp-Session-Id（若有）。
    try:
        notify_headers = {"Content-Type": "application/json"}
        session_id = _session_for(endpoint)
        if session_id:
            notify_headers["Mcp-Session-Id"] = session_id
        requests.post(
            endpoint,
            json=_rpc_message("notifications/initialized", None, None),
            headers=notify_headers,
            timeout=timeout,
        )
    except requests.RequestException:
        pass
    return server_info


def list_remote_tools(endpoint: str, timeout: float = _DISCOVERY_TIMEOUT_SECONDS) -> List[Dict[str, Any]]:
    """tools/list 工具发现：返回 [{name, description, inputSchema}, ...]。"""
    result = _post_rpc(endpoint, _rpc_message("tools/list", {}, _next_id()), timeout)
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        raise McpProtocolError(f"MCP tools/list 响应缺少 tools 数组 [{endpoint}]")
    return [item for item in tools if isinstance(item, dict) and item.get("name")]


def call_remote_tool(
    endpoint: str, name: str, arguments: Optional[Dict[str, Any]], timeout: float
) -> Tuple[str, bool]:
    """tools/call 调用远端工具。返回 (拼接的文本内容, isError)。"""
    result = _post_rpc(
        endpoint,
        _rpc_message("tools/call", {"name": name, "arguments": dict(arguments or {})}, _next_id()),
        timeout,
    )
    if not isinstance(result, dict):
        raise McpProtocolError(f"MCP tools/call 响应格式异常 [{endpoint}]")
    content = result.get("content") or []
    parts: List[str] = []
    for item in content if isinstance(content, list) else []:
        if isinstance(item, dict) and item.get("type") == "text":
            text = str(item.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts), bool(result.get("isError"))
