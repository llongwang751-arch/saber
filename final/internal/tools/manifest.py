"""Declarative, operator-owned tool registration; no executable YAML hooks."""

from pathlib import Path

import yaml

from .tools import validate_mcp_endpoint


def load_manifest(path):
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8-sig")) or {}
    if not isinstance(data, dict) or set(data) - {"builtins", "mcp_servers"}:
        raise ValueError("tool manifest accepts only builtins and mcp_servers")
    builtins = data.get("builtins", {})
    servers = data.get("mcp_servers", [])
    if not isinstance(builtins, dict) or any(not isinstance(k, str) or not isinstance(v, bool) for k, v in builtins.items()):
        raise ValueError("builtins must map tool names to booleans")
    if set(builtins) - {"search_web", "rag_search", "exec_command"}:
        raise ValueError("unknown builtin tool in manifest")
    if not isinstance(servers, list):
        raise ValueError("mcp_servers must be a list")
    names = set()
    for server in servers:
        if not isinstance(server, dict) or set(server) - {"name", "endpoint", "enabled"}:
            raise ValueError("MCP server accepts name, endpoint, enabled")
        name, endpoint = server.get("name"), server.get("endpoint")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("MCP server names must be unique and nonempty")
        names.add(name)
        if not isinstance(server.get("enabled", True), bool):
            raise ValueError("MCP server enabled must be boolean")
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("MCP endpoint must be a nonempty string")
        if server.get("enabled", True):
            validate_mcp_endpoint(endpoint)
    return {"builtins": builtins, "mcp_servers": servers}


def apply_tool_manifest(agent):
    path = getattr(agent.cfg, "tools_manifest", "")
    if not path:
        return
    manifest = load_manifest(path)
    for name, enabled in manifest["builtins"].items():
        if not enabled:
            agent.tool_executor.remove_tool(name)
    for server in manifest["mcp_servers"]:
        if server.get("enabled", True):
            agent.register_mcp_server(server["endpoint"])
    agent.tool_manifest = manifest
