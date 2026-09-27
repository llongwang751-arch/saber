"""Validated HTTP request models shared by route modules."""

from typing import Any, Dict, List
from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(..., description="用户输入")
    use_rag: bool = False
    conversation_id: str = Field(default="", max_length=128, pattern=r"^[A-Za-z0-9_-]*$")


class ApprovalDecision(BaseModel):
    approved: bool


class TaskResumeRequest(BaseModel):
    model_config = {"extra": "forbid"}
    conversation_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class MCPParam(BaseModel):
    name: str
    description: str = ""
    required: bool = False


class MCPRegisterRequest(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = ""
    endpoint: str = Field(..., min_length=1)
    params: List[Dict[str, Any]] = Field(default_factory=list)


class MCPServerDiscoverRequest(BaseModel):
    endpoint: str = Field(..., min_length=1, description="MCP 服务器 Streamable HTTP 端点")


class DocsDeleteRequest(BaseModel):
    doc_hash: str = Field(..., min_length=1)


class UploadJSONRequest(BaseModel):
    content: str = Field(..., min_length=1)


class RAGLabRequest(BaseModel):
    document: str = Field(..., min_length=1, max_length=40000, description="用于实验的文档正文")
    query: str = Field(..., min_length=1, max_length=1000, description="用户查询")
    top_k: int = Field(default=5, ge=1, le=10)
