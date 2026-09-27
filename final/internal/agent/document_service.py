"""Document service for an explicitly supplied conversation runtime.

The caller owns mutable state and lifecycle; services do not retain a user or conversation.
"""

import inspect
import logging
from typing import Any, Dict, List

from internal.document.library import DOCUMENT_SOURCE_AGENT, WriteRequest
from internal.tools.tools import Tool, new_mcp_tool


from .serialization import _param_string, _param_string_default, _param_bool, _json_string, _to_jsonable

logger = logging.getLogger(__name__)


def register_document_tools(agent) -> None:
    for tool in [
        agent._write_document_tool(),
        agent._list_documents_tool(),
        agent._read_document_tool(),
        agent._ingest_document_tool(),
    ]:
        agent.tool_executor.add_tool(tool)


def write_document_tool(agent) -> Tool:
    return Tool(
        name="write_document",
        description="将 Markdown 文档写入本地文档库，可选择同步入库 RAG。适合保存报告、总结、研究结果。",
        params=[
            {"name": "title", "type": "string", "description": "文档标题"},
            {"name": "content_md", "type": "string", "description": "Markdown 正文"},
            {"name": "doc_type", "type": "string", "description": "文档类型，如 report/note/summary"},
            {"name": "source", "type": "string", "description": "来源，如 agent_generated"},
            {"name": "summary", "type": "string", "description": "简短摘要"},
            {"name": "ingest_to_rag", "type": "boolean", "description": "是否写入后立即进入 RAG 索引"},
        ],
        func=lambda params: _json_string(
            agent.write_document(
                WriteRequest(
                    title=_param_string(params, "title"),
                    doc_type=_param_string_default(params, "doc_type", "report"),
                    source=_param_string_default(params, "source", DOCUMENT_SOURCE_AGENT),
                    created_by="agent",
                    content_md=_param_string(params, "content_md") or _param_string(params, "content"),
                    summary=_param_string(params, "summary"),
                    metadata={"tool": "write_document"},
                ),
                _param_bool(params, "ingest_to_rag"),
            )
        ),
    )


def list_documents_tool(agent) -> Tool:
    return Tool(
        name="list_documents",
        description="列出本地文档库中的文档。",
        params=[],
        func=lambda params: _json_string({"documents": agent.list_documents()}),
    )


def read_document_tool(agent) -> Tool:
    return Tool(
        name="read_document",
        description="读取本地文档库中的指定文档最新版本。",
        params=[{"name": "document_id", "type": "string", "description": "文档 ID"}],
        func=lambda params: _json_string(agent.get_document(_param_string(params, "document_id"))),
    )


def ingest_document_tool(agent) -> Tool:
    return Tool(
        name="ingest_document",
        description="将本地文档库中的文档版本切分并写入 RAG 索引。",
        params=[
            {"name": "document_id", "type": "string", "description": "文档 ID"},
            {"name": "version_id", "type": "string", "description": "版本 ID，不填则使用最新版本"},
        ],
        func=lambda params: _json_string(
            agent.ingest_document(
                _param_string(params, "document_id"),
                _param_string(params, "version_id"),
            )
        ),
    )


def document_store(agent):
    store = getattr(getattr(getattr(agent, "inf", None), "repo", None), "documents", None)
    if store is None:
        raise RuntimeError("document library not configured")
    return store


def document_store_call(agent, method_name: str, *args):
    """Apply tenant scoping when the repository supports ``user_id``."""
    method = getattr(agent._document_store(), method_name)
    try:
        supports_user = "user_id" in inspect.signature(method).parameters
    except (TypeError, ValueError):
        supports_user = False
    if supports_user:
        return method(*args, user_id=getattr(agent, "user_id", "default_user"))
    return method(*args)


def write_document(agent, req: WriteRequest, ingest_to_rag: bool = False) -> Dict[str, Any]:
    req.created_by = getattr(agent, "user_id", "default_user")
    wr = agent._document_store_call("write", req)
    out = _to_jsonable(wr)
    if ingest_to_rag:
        out["ingest"] = agent._ingest_content(
            wr.version.content_md,
            document_id=wr.document.id,
            version_id=wr.version.id,
            section=wr.document.doc_type,
        )
    return out


def list_documents(agent) -> List[Any]:
    return agent._document_store_call("list")


def get_document(agent, document_id: str) -> Dict[str, Any]:
    doc, ver = agent._document_store_call("get", document_id)
    return {"document": doc, "version": ver}


def delete_document(agent, document_id: str) -> None:
    if agent.rag is not None and hasattr(agent.rag, "delete_document"):
        agent.rag.delete_document(document_id)
    agent._document_store_call("delete", document_id)


def ingest_document(agent, document_id: str, version_id: str = "") -> Dict[str, Any]:
    if version_id:
        ver = agent._document_store_call("get_version", version_id)
    else:
        _, ver = agent._document_store_call("get", document_id)
    doc_id = document_id or ver.document_id
    return agent._ingest_content(
        ver.content_md,
        document_id=doc_id,
        version_id=ver.id,
        section="document",
    )


def ingest_content(agent, content: str, document_id: str, version_id: str, section: str) -> Dict[str, Any]:
    if agent.rag is None:
        raise RuntimeError("RAG 引擎未初始化")
    try:
        chunk_count = agent.rag.ingest(
            content,
            document_id=document_id,
            version_id=version_id,
            section=section,
        )
    except TypeError:
        chunk_count = agent.rag.ingest(content)
    return {
        "chunk_count": int(chunk_count or 0),
        "document_id": document_id,
        "version_id": version_id,
        "section": section,
    }


def register_mcp_tool(agent, name: str, description: str, params: List[Dict[str, str]], func=None, endpoint: str = ""):
    agent.add_tool(new_mcp_tool(name, description, params, func=func, endpoint=endpoint))


def register_mcp_server(agent, endpoint: str) -> Dict[str, Any]:
    """按 MCP 协议握手并批量注册远端服务器提供的工具。

    与 register_mcp_tool 的单工具裸 HTTP 模式互补：这里完成 initialize
    握手与 tools/list 自动发现，远端每个工具以 mcp_ 前缀注册为本地 Tool，
    执行走 tools/call。与既有工具重名的一律跳过（不静默覆盖内置能力）。
    SSRF 校验沿用 validate_mcp_endpoint：默认拒绝内网/回环端点。
    """
    from internal.tools.mcp_client import initialize_session, list_remote_tools
    from internal.tools.tools import build_mcp_remote_tool, validate_mcp_endpoint

    validate_mcp_endpoint(endpoint)
    server_info = initialize_session(endpoint)
    specs = list_remote_tools(endpoint)
    existing = set(agent.tool_executor.snapshot().keys())
    registered: List[str] = []
    skipped: List[str] = []
    for spec in specs:
        remote_name = str(spec.get("name") or "").strip()
        if not remote_name:
            continue
        local_name = f"mcp_{remote_name}"
        if local_name in existing:
            skipped.append(local_name)
            continue
        agent.add_tool(build_mcp_remote_tool(endpoint, spec))
        existing.add(local_name)
        registered.append(local_name)
    return {"server": server_info, "registered": registered, "skipped": skipped}
