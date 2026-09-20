# handler — HTTP API 路由处理（FastAPI + Pydantic + CORS）
import asyncio
import inspect
import logging
import os
import hashlib
import hmac
import json
import math
import queue
import re
import threading
import time
import uuid
import gc
import sys
import tracemalloc
from types import SimpleNamespace
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from config.config import APIConfig
from internal.fastapi_compat import app_lifespan, register_shutdown
from internal.application.api import current_agent, install_application_features
from internal.agent.agent import ChatOptions, RequestExecutionContext, Response, UnifiedAgent
from internal.document.library import DOCUMENT_SOURCE_UPLOAD, WriteRequest
from internal.document.parser import parse_bytes
from internal.evaluation.api import create_evaluation_router
from internal.evaluation.service import EvaluationService, EvaluationServiceRegistry
from internal.experimentation.public import redact_public_experiment_trace
from internal.experimentation.store import IdempotencyConflictError
from internal.infra.infra import Infrastructure
from internal.rag.lab import run_rag_lab

logger = logging.getLogger(__name__)

# ─── 请求 / 响应 模型 ──────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str = Field(..., description="用户输入")
    use_rag: bool = False
    conversation_id: str = Field(default="", max_length=128, pattern=r"^[A-Za-z0-9_-]*$")


class ApprovalDecision(BaseModel):
    approved: bool


class TaskResumeRequest(BaseModel):
    model_config = {'extra': 'forbid'}
    conversation_id: str = Field(..., min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')


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


# ─── 工具函数 ───────────────────────────────────────────────────────────────

def _response_to_dict(resp: Response) -> Dict[str, Any]:
    exposure_id = str(getattr(resp, "experiment_exposure_id", "") or "")
    return {
        "query": resp.query,
        "answer": resp.answer,
        "mode": resp.mode,
        "intent": resp.intent,
        "slots": resp.slots,
        "fallback": resp.fallback,
        "error": resp.error,
        "steps": [
            {
                "type": s.type,
                "content": s.content,
                "tool": s.tool,
                "params": s.params,
            }
            for s in resp.steps
        ],
        "tool_call": resp.tool_call,
        "tool_calls": resp.tool_calls,
        "search_results": [_rag_result_to_main_contract(r) for r in resp.search_results],
        "rag_trace": redact_public_experiment_trace(
            resp.rag_trace,
            experiment_active=bool(exposure_id),
        ),
        "task": resp.task,
        "extracted_info": resp.extracted_info,
        "short_term_count": resp.short_term_count,
        "long_term_count": resp.long_term_count,
        "preferences": resp.preferences,
        "interrupted": resp.interrupted,
        "trace_id": resp.trace_id,
        # Never reveal the assigned arm or candidate configuration to normal
        # chat clients.  Feedback is linked through this opaque exposure id.
        "experiment": {
            "exposure_id": exposure_id,
            "feedback_eligible": bool(
                exposure_id and not resp.error and not resp.interrupted
            ),
        },
        "success": not bool(resp.error),
    }


def _rag_result_to_main_contract(result: Dict[str, Any]) -> Dict[str, Any]:
    content = result.get("content", "")
    score = result.get("score", result.get("similarity", 0.0))
    try:
        similarity = float(score)
    except Exception:
        similarity = 0.0
    if not math.isfinite(similarity):
        similarity = 0.0
    return {
        **result,
        "content": content,
        "score": similarity,
        "similarity": similarity,
        "chunk": result.get("chunk") or {"content": content},
        "source": result.get("source", "unknown"),
    }


def _tool_to_main_contract(tool: Dict[str, Any]) -> Dict[str, Any]:
    """兼容 Go main 分支前端：工具参数字段叫 params。"""
    params = tool.get("params")
    if params is None:
        params = tool.get("parameters", [])
    return {
        "name": tool.get("name", ""),
        "description": tool.get("description", tool.get("desc", "")),
        "is_mcp": tool.get("is_mcp", False),
        "params": params or [],
    }


def _sse(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _jsonable(value: Any) -> Any:
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return _jsonable(asdict(value))
    return value


def _begin_online_rag_exposure(
    request: Request,
    req: ChatRequest,
    active_agent: Any,
    trace_id: str,
) -> tuple[Any | None, RequestExecutionContext | None]:
    """Resolve and persist a real request exposure, failing closed to control.

    Merely asking for an assignment is not an exposure.  The ledger entry is
    created only after the request is known to enter the loaded, read-only RAG
    path and immediately before Agent execution.  Any control-plane failure
    leaves the request on the stable default configuration.
    """

    service = getattr(request.app.state, "experiment_service", None)
    rag = getattr(active_agent, "rag", None)
    if service is None or not req.use_rag or not bool(getattr(rag, "loaded", False)):
        return None, None
    user = getattr(request.state, "user", None)
    if not isinstance(user, dict):
        return None, None
    # Online allocation is tenant-scoped.  Never silently turn a user id into
    # a tenant id when an upstream identity record is incomplete.
    tenant_id = str(user.get("tenant_id") or "").strip()
    user_id = str(user.get("id") or "").strip()
    turn_id = str(getattr(request.state, "request_id", "") or trace_id).strip()
    if not tenant_id or not user_id:
        return None, None
    try:
        # X-Request-ID is an idempotency key, not merely a correlation label.
        # Bind it to the canonical request semantics so a caller cannot reuse
        # one turn id for different text and inherit somebody else's exposure.
        request_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "surface": "rag_chat",
                    "message": req.message,
                    "use_rag": bool(req.use_rag),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        allocation = service.resolve_assignment(
            tenant_id,
            user_id,
            "rag_chat",
            turn_id,
            trace_id,
            identity_context=user,
        )
        if not allocation:
            return None, None
        allocation = {**dict(allocation), "request_fingerprint": request_fingerprint}
        exposure = service.begin_exposure(allocation)
        if bool((exposure or {}).get("idempotent")):
            # We intentionally do not execute the Agent again.  Returning a
            # conflict is safer than keeping an in-memory response cache: it
            # works across workers and cannot replay private response data to
            # a later request.
            raise HTTPException(
                status_code=409,
                detail="该请求编号已经执行过；请复用原响应或生成新的请求编号",
            )
        merged = {**dict(allocation), **dict(exposure or {})}
        exposure_id = str(
            merged.get("opaque_exposure_id") or merged.get("exposure_id") or ""
        ).strip()
        if not exposure_id:
            raise RuntimeError("experiment exposure ledger returned no exposure id")
        runtime_overrides = merged.get("runtime_overrides") or {}
        if hasattr(runtime_overrides, "runtime_payload"):
            runtime_overrides = runtime_overrides.runtime_payload()
        context = RequestExecutionContext(
            runtime_overrides=dict(runtime_overrides or {}),
            experiment_exposure_id=exposure_id,
            runtime_strategy_checksum=str(
                merged.get("runtime_strategy_checksum")
                or merged.get("compiled_checksum")
                or ""
            ),
            trace_id=trace_id,
        )
        return merged, context
    except HTTPException:
        raise
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail="请求编号与已有请求冲突，请生成新的请求编号",
        ) from exc
    except Exception as exc:
        logger.error(
            "线上实验曝光写入失败，已回退稳定对照配置 request_id=%s: %s",
            turn_id,
            exc,
        )
        return None, None


def _finish_online_rag_exposure(
    request: Request,
    exposure: Any | None,
    *,
    response: Response | None,
    started: float,
    error: Exception | None = None,
) -> None:
    """Best-effort terminal update; the user response never depends on analytics."""

    if not exposure:
        return
    service = getattr(request.app.state, "experiment_service", None)
    if service is None:
        return
    interrupted = bool(getattr(response, "interrupted", False)) if response else False
    status = "cancelled" if interrupted else "error" if error or (response and response.error) else "completed"
    rag_trace = dict(getattr(response, "rag_trace", {}) or {}) if response else {}
    safety_events = [
        dict(item)
        for item in (rag_trace.get("safety_events") or [])
        if isinstance(item, dict)
        and str(item.get("severity") or "").upper() in {"S0", "S1"}
        and item.get("trusted") is True
        and str(item.get("source") or "") in {"server_guardrail", "server_evaluator"}
    ]
    metrics = {
        "completed": status == "completed",
        "error": status == "error",
        "cancelled": status == "cancelled",
        "fallback": bool(getattr(response, "fallback", False)) if response else False,
        "rag_answered": bool(getattr(response, "search_results", None)) if response else False,
        "rag_result_count": len(getattr(response, "search_results", []) or []) if response else 0,
        "rag_decision": str(rag_trace.get("decision") or ""),
    }
    try:
        service.finish_exposure(
            exposure,
            status=status,
            latency_ms=max(0.0, (time.perf_counter() - started) * 1000.0),
            metrics=metrics,
            safety_events=safety_events,
        )
    except Exception as exc:
        # Missing terminal telemetry is surfaced by readiness/analysis and can
        # never be treated as a successful effect claim.
        logger.error("线上实验曝光终态写入失败: %s", exc)


def _sanitize_stream_done(
    data: Any,
    *,
    exposure_id_hint: str = "",
) -> dict[str, Any]:
    """Expose only the opaque feedback handle, never arm/configuration details."""

    payload = dict(data) if isinstance(data, dict) else {}
    exposure_id = str(
        payload.pop("experiment_exposure_id", "") or exposure_id_hint or ""
    )
    payload.pop("runtime_strategy_checksum", None)
    feedback_eligible = bool(
        exposure_id
        and not payload.get("error")
        and not payload.get("interrupted")
        and payload.get("success", True) is not False
    )
    payload = redact_public_experiment_trace(
        payload,
        experiment_active=bool(exposure_id),
    )
    payload["experiment"] = {
        "exposure_id": exposure_id,
        "feedback_eligible": feedback_eligible,
    }
    return payload


def _normalize_ingest_result(result: Any, text: str, document_id: str = "", version_id: str = "", section: str = "") -> Dict[str, Any]:
    doc_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else ""
    if isinstance(result, tuple):
        chunk_count = result[0] if len(result) > 0 else 0
        if len(result) > 1 and result[1]:
            doc_hash = result[1]
    elif isinstance(result, dict):
        out = dict(result)
        out.setdefault("chunk_count", 0)
        out.setdefault("doc_hash", doc_hash)
        if document_id:
            out.setdefault("document_id", document_id)
        if version_id:
            out.setdefault("version_id", version_id)
        if section:
            out.setdefault("section", section)
        return out
    else:
        chunk_count = result or 0
    return {
        "chunk_count": int(chunk_count or 0),
        "parent_count": 0,
        "indexed_count": int(chunk_count or 0),
        "doc_hash": doc_hash,
        "document_id": document_id,
        "version_id": version_id,
        "section": section,
    }


# ─── 路由组装 ───────────────────────────────────────────────────────────────

def setup_routes(
    agent: UnifiedAgent,
    inf: Infrastructure,
    cfg: APIConfig,
    *,
    auth_required: bool = False,
) -> FastAPI:
    app = FastAPI(title="AGI Assistant", version="1.0", lifespan=app_lifespan)

    # 评测平台使用独立 SQLAlchemy/SQLite 存储，不依赖 PG、Milvus 或模型 Key。
    # 因此即使业务基础设施降级，也能离线跑评测集和 Badcase 回归。
    evaluation_service = EvaluationService(local_agent=agent)
    app.state.evaluation_service = evaluation_service
    app.include_router(create_evaluation_router())
    register_shutdown(app, evaluation_service.close)

    # CORS：鉴权走 Authorization 头而非 Cookie，通配符源无需携带凭据。
    # 显式配置 cors_origins 时才允许 credentials，避免 "*"+credentials 的宽松组合。
    origins = list(getattr(cfg, "cors_origins", None) or ["*"])
    allow_credentials = "*" not in origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    install_application_features(
        app,
        agent,
        inf,
        cfg,
        auth_required=auth_required,
    )
    if getattr(cfg, "pprof_enabled", False):
        admin_token = str(getattr(cfg, "pprof_admin_token", "") or "")

        def _debug_authorized(request: Request) -> None:
            if not admin_token or not hmac.compare_digest(
                admin_token.encode("utf-8"), request.headers.get("x-admin-token", "").encode("utf-8")
            ):
                # Mirror Go: an unauthorized caller cannot tell whether debug
                # endpoints are mounted.
                raise HTTPException(status_code=404, detail="Not Found")

        @app.get("/debug/pprof/", include_in_schema=False)
        async def debug_index(request: Request):
            _debug_authorized(request)
            return PlainTextResponse(
                "Types of profiles available: threads, heap, objects, cmdline, trace\n"
            )

        @app.get("/debug/pprof/cmdline", include_in_schema=False)
        async def debug_cmdline(request: Request):
            _debug_authorized(request)
            return PlainTextResponse("\x00".join(sys.argv))

        @app.api_route("/debug/pprof/{profile}", methods=["GET", "POST"], include_in_schema=False)
        async def debug_profile(profile: str, request: Request):
            _debug_authorized(request)
            if profile in {"goroutine", "thread", "threads"}:
                frames = sys._current_frames()
                return JSONResponse({
                    "threads": [
                        {"name": item.name, "ident": item.ident, "frame": bool(frames.get(item.ident))}
                        for item in threading.enumerate()
                    ]
                })
            if profile in {"heap", "allocs", "objects"}:
                if not tracemalloc.is_tracing():
                    tracemalloc.start(10)
                current, peak = tracemalloc.get_traced_memory()
                return JSONResponse({"tracked_bytes": current, "peak_bytes": peak, "gc_objects": len(gc.get_objects())})
            if profile in {"symbol", "trace", "profile"}:
                return JSONResponse({"profile": profile, "runtime": "python", "supported": True})
            raise HTTPException(status_code=404, detail="Not Found")
    if auth_required:
        evaluation_registry = EvaluationServiceRegistry(
            local_agent_factory=app.state.agent_registry.get
        )
        app.state.evaluation_service_registry = evaluation_registry
        experiment_service = getattr(app.state, "experiment_service", None)
        if experiment_service is not None:
            application_store_ready = bool(
                getattr(experiment_service, "production_evidence_ready", False)
            )
            experiment_service.production_evidence_ready = bool(
                application_store_ready
                and evaluation_registry.production_shared_ready
            )
            experiment_service.production_evidence_backends = {
                "application": getattr(
                    experiment_service, "storage_backend", "unknown"
                ),
                "offline_evaluation": evaluation_registry.storage_backend,
                "shared_offline_evaluation": (
                    evaluation_registry.production_shared_ready
                ),
            }
        register_shutdown(app, evaluation_registry.close)

    @app.middleware("http")
    async def production_guardrails(request: Request, call_next):
        request_id = request.headers.get("x-request-id", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", request_id):
            request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        max_body = max(1024, int(os.getenv("AGI_MAX_REQUEST_BYTES", str(25 * 1024 * 1024))))
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > max_body:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": f"请求体超过 {max_body} 字节限制", "request_id": request_id},
                        headers={"X-Request-ID": request_id},
                    )
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Content-Length 无效", "request_id": request_id},
                    headers={"X-Request-ID": request_id},
                )
        timeout_seconds = max(1.0, float(os.getenv("AGI_REQUEST_TIMEOUT_SECONDS", "120")))
        is_ingest_request = request.url.path == "/api/upload" or (
            request.url.path.startswith("/api/documents/") and request.url.path.endswith("/ingest")
        )
        if is_ingest_request:
            timeout_seconds = max(
                timeout_seconds,
                float(os.getenv("AGI_INGEST_TIMEOUT_SECONDS", "900")),
            )
        started = time.perf_counter()
        try:
            response = await asyncio.wait_for(call_next(request), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning("请求超时 request_id=%s path=%s", request_id, request.url.path)
            response = JSONResponse(
                status_code=504,
                content={"detail": "请求处理超时", "request_id": request_id},
            )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path.startswith("/api/"):
            # Streaming endpoints set the Go-compatible ``no-cache`` policy;
            # keep explicit handler policies and default all other APIs to
            # ``no-store``.
            response.headers.setdefault("Cache-Control", "no-store")
        elif request.url.path == "/" or request.url.path.endswith(".html"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        logger.info(
            "HTTP %s %s -> %s %.1fms request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
            request_id,
        )
        return response

    @app.get("/health")
    async def health():
        ready = getattr(inf, "ready", SimpleNamespace())
        return {
            "status": "ok",
            "milvus": getattr(ready, "milvus", "not_configured"),
            "postgresql": getattr(ready, "postgresql", "not_configured"),
            "elasticsearch": getattr(ready, "elasticsearch", "not_configured"),
            "kafka": getattr(ready, "kafka", "not_configured"),
            "local_database": "connected",
        }

    @app.post("/api/chat")
    async def chat(req: ChatRequest, request: Request):
        exposure = None
        response = None
        started = time.perf_counter()
        try:
            active_agent = current_agent(request)
            opts = ChatOptions(use_rag=req.use_rag, conversation_id=req.conversation_id)
            # The validated request id is also the turn/trace id.  A client
            # that falls back from SSE to sync with the same X-Request-ID gets
            # the same idempotent exposure instead of being counted twice.
            trace_id = str(getattr(request.state, "request_id", "") or uuid.uuid4())
            exposure, execution_context = _begin_online_rag_exposure(
                request,
                req,
                active_agent,
                trace_id,
            )
            if execution_context is None:
                response = await run_in_threadpool(
                    active_agent.process_with_options, req.message, opts
                )
            else:
                response = await run_in_threadpool(
                    active_agent.process_with_options,
                    req.message,
                    opts,
                    execution_context,
                )
            _finish_online_rag_exposure(
                request,
                exposure,
                response=response,
                started=started,
            )
            return _response_to_dict(response)
        except HTTPException:
            raise
        except Exception as e:
            _finish_online_rag_exposure(
                request,
                exposure,
                response=response,
                started=started,
                error=e,
            )
            logger.error("聊天接口错误: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/chat/stream")
    async def chat_stream(req: ChatRequest, request: Request):
        """SSE 流式：handler 只负责输出事件，真实 token 由 agent 内部 LLM 流式回调产生。"""

        opts = ChatOptions(use_rag=req.use_rag, conversation_id=req.conversation_id)

        active_agent = current_agent(request)
        # Resolve the experiment exposure before response headers are sent, so
        # duplicate/conflicting idempotency keys produce a real HTTP 409 rather
        # than failing inside an already-open SSE stream.
        trace_id = str(getattr(request.state, "request_id", "") or uuid.uuid4())
        exposure, execution_context = _begin_online_rag_exposure(
            request,
            req,
            active_agent,
            trace_id,
        )
        registry = getattr(active_agent, "_cancel_registry", None)
        if registry is not None:
            token, unregister = registry.register()
        else:
            token = SimpleNamespace(is_cancelled=lambda: False, cancel=lambda: None)
            unregister = lambda: None

        async def _generate():
            stream_closed = threading.Event()
            events = None
            yield _sse("start", {"message": req.message})
            try:
                if hasattr(active_agent, "process_stream"):
                    events = queue.Queue(maxsize=512)
                    sentinel = object()
                    deferred_done: list[str] = []

                    def enqueue(item):
                        while not stream_closed.is_set():
                            try:
                                events.put(item, timeout=0.1)
                                return
                            except queue.Full:
                                continue

                    def _on_event(evt):
                        if not isinstance(evt, dict):
                            evt = _jsonable(evt)
                        event_type = str((evt or {}).get("type", "") or "")
                        data = (evt or {}).get("data") or {}
                        if event_type == "rag_result" and isinstance(data, dict):
                            data = {
                                **data,
                                "search_results": [
                                    _rag_result_to_main_contract(item)
                                    for item in (data.get("search_results") or [])
                                    if isinstance(item, dict)
                                ],
                            }
                        if event_type == "rag_trace":
                            data = redact_public_experiment_trace(
                                data,
                                experiment_active=bool(exposure),
                            )
                        if event_type == "done":
                            exposure_id_hint = ""
                            if isinstance(exposure, dict):
                                exposure_id_hint = str(
                                    exposure.get("opaque_exposure_id")
                                    or exposure.get("exposure_id")
                                    or exposure.get("id")
                                    or ""
                                )
                            data = _sanitize_stream_done(
                                data,
                                exposure_id_hint=exposure_id_hint,
                            )
                        if event_type:
                            rendered = _sse(event_type, data)
                            if event_type == "done":
                                # The opaque feedback handle is useful only
                                # after its exposure has a terminal ledger
                                # state.  Buffer the final event so a fast UI
                                # click cannot race ``finish_exposure``.
                                deferred_done.append(rendered)
                            else:
                                enqueue(rendered)

                    def _run_process_stream():
                        response = None
                        started = time.perf_counter()
                        # 真实 UnifiedAgent 支持复用 HTTP 层取消令牌（断连即取消）；
                        # 测试替身可能是窄签名，不支持时不强传。
                        stream_kwargs = (
                            {"cancel_token": token}
                            if "cancel_token" in getattr(
                                inspect.signature(active_agent.process_stream), "parameters", {}
                            )
                            else {}
                        )
                        try:
                            if execution_context is None:
                                response = active_agent.process_stream(
                                    req.message, opts, _on_event, **stream_kwargs,
                                )
                            else:
                                response = active_agent.process_stream(
                                    req.message,
                                    opts,
                                    _on_event,
                                    execution_context,
                                    **stream_kwargs,
                                )
                            _finish_online_rag_exposure(
                                request,
                                exposure,
                                response=response,
                                started=started,
                            )
                            if deferred_done:
                                for rendered in deferred_done:
                                    enqueue(rendered)
                            elif response is not None:
                                enqueue(_sse("done", _response_to_dict(response)))
                        except Exception as e:
                            deferred_done.clear()
                            _finish_online_rag_exposure(
                                request,
                                exposure,
                                response=response,
                                started=started,
                                error=e,
                            )
                            logger.error("流式聊天 process_stream 失败: %s", e)
                            enqueue(_sse("done", {"answer": f"请求失败: {e}", "interrupted": False, "success": False}))
                        finally:
                            enqueue(sentinel)

                    worker = threading.Thread(target=_run_process_stream, name="chat-stream", daemon=True)
                    worker.start()
                    while True:
                        item = await asyncio.to_thread(events.get)
                        if item is sentinel:
                            break
                        yield item
                    return

                try:
                    started = time.perf_counter()
                    if hasattr(active_agent, "_dispatch") and registry is not None:
                        if execution_context is None:
                            resp = active_agent._dispatch(req.message, opts, token)
                        else:
                            resp = active_agent._dispatch(
                                req.message,
                                opts,
                                token,
                                execution_context=execution_context,
                            )
                    else:
                        if execution_context is None:
                            resp = active_agent.process_with_options(req.message, opts)
                        else:
                            resp = active_agent.process_with_options(
                                req.message,
                                opts,
                                execution_context,
                            )
                    _finish_online_rag_exposure(
                        request,
                        exposure,
                        response=resp,
                        started=started,
                    )
                except Exception as e:
                    _finish_online_rag_exposure(
                        request,
                        exposure,
                        response=None,
                        started=started,
                        error=e,
                    )
                    logger.error("流式聊天 _dispatch 失败: %s", e)
                    yield _sse("done", {"answer": f"请求失败: {e}", "interrupted": False, "success": False})
                    return

                data = _response_to_dict(resp)
                yield _sse("route", {"mode": resp.mode})
                if resp.extracted_info:
                    yield _sse("memory", {"extracted_info": resp.extracted_info})
                for step in resp.steps:
                    yield _sse("step", {
                        "type": step.type,
                        "content": step.content,
                        "tool": step.tool,
                        "params": step.params,
                    })
                if resp.tool_call:
                    yield _sse("tool_call", resp.tool_call)
                if resp.search_results:
                    yield _sse("rag_result", {"search_results": data["search_results"]})

                answer_text = resp.answer or ""
                interrupted = bool(resp.interrupted)

                if answer_text and not interrupted:
                    # 逐 token（按字符）yield，体感为真流式。
                    # 注：当前 _dispatch 已生成完整 answer，本路由不再二次调
                    # llm.chat_stream_context 以避免与 stm/记忆写入重复。真流式
                    # LLM 接口 chat_stream_context 有独立单测覆盖，并按 queue+
                    # thread 范式接入，待 Task 25 多任务取消落地后切到本路由。
                    for ch in answer_text:
                        if token.is_cancelled():
                            break
                        yield _sse("token", {"content": ch})

                if token.is_cancelled():
                    data["interrupted"] = True

                yield _sse("done", data)
            finally:
                # 客户端断连时生成器在这里退出：先取消该请求的执行链
                # （LLM 流/图执行/工具循环都会检查该令牌），再关闭事件队列。
                try:
                    token.cancel()
                except Exception:
                    pass
                stream_closed.set()
                if events is not None:
                    # Wake any cancelled asyncio.to_thread(events.get) waiter.
                    try:
                        while True:
                            events.get_nowait()
                    except queue.Empty:
                        pass
                    events.put_nowait(sentinel)
                try:
                    unregister()
                except Exception:
                    pass

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    @app.post("/api/conversations")
    async def create_conversation(request: Request):
        current_agent(request)  # Apply the same authentication boundary as chat.
        return {"conversation_id": str(uuid.uuid4())}

    @app.get("/api/tool-approvals")
    async def tool_approvals(request: Request):
        from dataclasses import asdict
        agent = current_agent(request)
        return [asdict(item) for item in agent.human_approval.requests_for(agent.user_id)
                if item.status.value == "pending" and item.expires_at > time.time()]

    @app.post("/api/tool-approvals/{approval_id}/decision")
    async def decide_tool_approval(approval_id: str, decision: ApprovalDecision, request: Request):
        from internal.agent.tool_execution import guarded_tool_attempt
        from internal.agent.conversations import ConversationBusy
        agent = current_agent(request)
        owned = {item.request_id: item for item in agent.human_approval.requests_for(agent.user_id)}
        item = owned.get(approval_id)
        if item is None:
            raise HTTPException(404, "审批不存在")
        def run(scoped):
            tool = scoped.tool_executor.snapshot().get(item.tool_name)
            if tool is None:
                raise HTTPException(409, "工具已不可用，请重新发起请求")
            agent.human_approval.decide(approval_id, approved=decision.approved, operator=agent.user_id)
            if not decision.approved:
                return {"status": "rejected"}
            result = guarded_tool_attempt(scoped, tool, item.tool_name, dict(item.params), None,
                max(1.0, scoped.cfg.step_timeout_ms / 1000), item.invocation_id)
            # Result is added to the same conversation; no model-generated substitute action.
            scoped.stm.add("assistant", result.payload or str(result.error or ""))
            scoped._save_chat_history("assistant", result.payload or str(result.error or ""))
            return {"status": "completed" if result.success else "failed", "result": result.to_dict()}
        def execute():
            if item.session_id != "default":
                with agent._conversations.lease(item.session_id) as scoped:
                    return run(scoped)
            with agent._turn_lock:
                return run(agent)
        try:
            return await run_in_threadpool(execute)
        except (ValueError, ConversationBusy) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/chat/cancel")
    async def chat_cancel(request: Request, conversation_id: str = ""):
        try:
            agent = current_agent(request)
            if conversation_id:
                agent._conversations.cancel(conversation_id)
            else:
                agent.cancel()
            return {"ok": True, "message": "已发送取消信号"}
        except Exception as e:
            logger.error("取消失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/rag/lab/run")
    async def rag_lab_run(req: RAGLabRequest, request: Request):
        """运行隔离的 RAG 教学流水线，不向正式知识库写入任何数据。"""
        try:
            active_agent = current_agent(request)
            return await run_in_threadpool(
                run_rag_lab,
                active_agent,
                req.document,
                req.query,
                req.top_k,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error("RAG 实验台运行失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/rag/reindex")
    async def rag_reindex(request: Request):
        """Rebuild idempotent search projections from the primary chunk store."""

        rag = getattr(current_agent(request), "rag", None)
        if rag is None or not hasattr(rag, "rebuild_indexes"):
            raise HTTPException(status_code=503, detail="RAG 不可用")
        try:
            return await run_in_threadpool(rag.rebuild_indexes)
        except Exception as exc:
            logger.error("RAG 索引重建失败: %s", exc)
            raise HTTPException(status_code=500, detail="RAG 索引重建失败")

    @app.post("/api/docs/delete")
    async def docs_delete(req: DocsDeleteRequest, request: Request):
        try:
            rag = getattr(current_agent(request), "rag", None)
            if rag is None or not hasattr(rag, "delete"):
                raise HTTPException(status_code=503, detail="RAG 服务不可用，无法删除文档")
            await run_in_threadpool(rag.delete, req.doc_hash)
            return {"ok": True, "doc_hash": req.doc_hash}
        except HTTPException:
            raise
        except Exception as e:
            logger.error("删除文档失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    # 上传白名单：与 document parser 实际支持的格式对齐（PDF / Markdown / 纯文本）。
    # 二进制垃圾（exe/zip 等）在解析层只会产出乱码文本污染知识库，直接拒绝。
    upload_allowed_extensions = {".md", ".markdown", ".txt", ".pdf"}

    def _upload_max_bytes() -> int:
        return max(1024, int(os.getenv("AGI_UPLOAD_MAX_BYTES", str(25 * 1024 * 1024))))

    def _validate_upload_filename(filename: str) -> None:
        suffix = os.path.splitext(str(filename).replace("\\", "/").split("/")[-1])[1].lower()
        if suffix not in upload_allowed_extensions:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件类型 {suffix or '(无扩展名)'}，仅允许 md/markdown/txt/pdf",
            )

    @app.post("/api/upload")
    async def upload(request: Request):
        try:
            active_agent = current_agent(request)
            content_type = request.headers.get("content-type", "")
            filename = "upload.txt"
            upload_content_type = "text/plain"
            if "application/json" in content_type:
                payload = await request.json()
                raw_text = str((payload or {}).get("content", ""))
                if len(raw_text.encode("utf-8")) > _upload_max_bytes():
                    raise HTTPException(status_code=413, detail="文档内容超过大小限制")
                parsed = parse_bytes(filename, upload_content_type, raw_text.encode("utf-8"))
            else:
                form = await request.form()
                file = form.get("file")
                if file is None:
                    raise HTTPException(status_code=400, detail="缺少 file 或 content")
                filename = getattr(file, "filename", None) or filename
                _validate_upload_filename(filename)
                upload_content_type = getattr(file, "content_type", None) or upload_content_type
                # 分块读取并累计计量：既避免整包读入内存，也让 chunked 编码
                # （无 Content-Length，绕过全局 body 中间件）同样受大小上限约束。
                parts: List[bytes] = []
                total = 0
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _upload_max_bytes():
                        raise HTTPException(status_code=413, detail="文件超过大小限制")
                    parts.append(chunk)
                parsed = parse_bytes(filename, upload_content_type, b"".join(parts))

            text = parsed.content
            if parsed.needs_ocr:
                return {
                    "filename": parsed.filename,
                    "content_type": parsed.content_type,
                    "parser": parsed.parser,
                    "pages": parsed.pages,
                    "text_chars": parsed.text_chars,
                    "needs_ocr": True,
                    "chunk_count": 0,
                    "parent_count": 0,
                    "indexed_count": 0,
                    "doc_hash": "",
                    "chunks": None,
                    "message": "PDF 文本抽取结果过少，可能是扫描件，需要 OCR 后再入库",
                }
            if not text.strip():
                return {"chunk_count": 0, "doc_hash": "", "success": False, "message": "文件内容为空"}
            doc_result = None
            ingest_result = None
            if hasattr(active_agent, "write_document"):
                write_request = WriteRequest(
                    title=filename,
                    doc_type="upload",
                    source=DOCUMENT_SOURCE_UPLOAD,
                    created_by="user",
                    content_md=text,
                    metadata={
                        "filename": parsed.filename,
                        "content_type": parsed.content_type,
                        "parser": parsed.parser,
                        "pages": parsed.pages,
                        "text_chars": parsed.text_chars,
                    },
                )
                # Document parsing, embedding and index writes are synchronous today.
                # Keep them off the asyncio event loop so a slow embedding provider
                # does not freeze health checks, chat streams and other users.
                doc_result = await run_in_threadpool(
                    active_agent.write_document,
                    write_request,
                    True,
                )
                ingest_result = (doc_result or {}).get("ingest")
            else:
                ingest_result = await run_in_threadpool(active_agent.rag_ingest, text)
            doc_json = _jsonable((doc_result or {}).get("document")) if isinstance(doc_result, dict) else None
            ver_json = _jsonable((doc_result or {}).get("version")) if isinstance(doc_result, dict) else None
            ingest = _normalize_ingest_result(
                ingest_result,
                text,
                document_id=(doc_json or {}).get("id", "") if isinstance(doc_json, dict) else "",
                version_id=(ver_json or {}).get("id", "") if isinstance(ver_json, dict) else "",
                section="upload",
            )
            return {
                "filename": parsed.filename,
                "content_type": parsed.content_type,
                "parser": parsed.parser,
                "pages": parsed.pages,
                "text_chars": parsed.text_chars,
                "needs_ocr": parsed.needs_ocr,
                "chunk_count": ingest.get("chunk_count", 0),
                "parent_count": ingest.get("parent_count", 0),
                "indexed_count": ingest.get("indexed_count", ingest.get("chunk_count", 0)),
                "chunk_preview": ingest.get("chunk_preview"),
                "doc_hash": ingest.get("doc_hash", ""),
                "chunks": ingest.get("chunks"),
                "document": doc_json,
                "version": ver_json,
                "success": True,
            }
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error("上传接口错误: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/documents/", include_in_schema=False)
    @app.get("/api/documents")
    async def documents_list(request: Request):
        try:
            active_agent = current_agent(request)
            docs = _jsonable(active_agent.list_documents())
            if hasattr(active_agent, "get_document"):
                enriched = []
                for doc in docs or []:
                    latest_metadata = {}
                    latest_content_chars = 0
                    latest_parser = ""
                    try:
                        document_id = str((doc or {}).get("id", "") or "")
                        if document_id:
                            latest = _jsonable(active_agent.get_document(document_id))
                            ver = (latest or {}).get("version") if isinstance(latest, dict) else None
                            if isinstance(ver, dict):
                                latest_metadata = ver.get("metadata") or {}
                                latest_content_chars = len(str(ver.get("content_md", "") or ""))
                                latest_parser = str((latest_metadata or {}).get("parser", "") or "")
                    except Exception:
                        latest_metadata = {}
                    item = dict(doc or {})
                    item["latest_metadata"] = latest_metadata
                    item["latest_content_chars"] = latest_content_chars
                    item["latest_parser"] = latest_parser
                    item["rag_chunk_count"] = 0
                    rag = getattr(active_agent, "rag", None)
                    if rag is not None and hasattr(rag, "document_chunk_count"):
                        try:
                            item["rag_chunk_count"] = int(rag.document_chunk_count(document_id) or 0)
                        except Exception:
                            item["rag_chunk_count"] = 0
                    enriched.append(item)
                docs = enriched
            return {"documents": docs}
        except Exception as e:
            logger.error("文档列表失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/documents/", include_in_schema=False)
    @app.post("/api/documents")
    async def documents_write(request: Request):
        try:
            active_agent = current_agent(request)
            payload = await request.json()
            res = active_agent.write_document(
                WriteRequest(
                    document_id=str((payload or {}).get("document_id", "") or ""),
                    title=str((payload or {}).get("title", "") or ""),
                    doc_type=str((payload or {}).get("doc_type", "") or ""),
                    source=str((payload or {}).get("source", "") or ""),
                    created_by=str((payload or {}).get("created_by", "") or ""),
                    content_md=str((payload or {}).get("content_md", "") or ""),
                    summary=str((payload or {}).get("summary", "") or ""),
                    metadata=(payload or {}).get("metadata") or {},
                ),
                bool((payload or {}).get("ingest_to_rag")),
            )
            out = _jsonable(res)
            if isinstance(out, dict) and "ingest" in out and not isinstance(out.get("ingest"), dict):
                version = out.get("version") or {}
                document = out.get("document") or {}
                out["ingest"] = _normalize_ingest_result(
                    out.get("ingest"),
                    str(version.get("content_md", "")) if isinstance(version, dict) else "",
                    document_id=str(document.get("id", "")) if isinstance(document, dict) else "",
                    version_id=str(version.get("id", "")) if isinstance(version, dict) else "",
                    section=str(document.get("doc_type", "")) if isinstance(document, dict) else "",
                )
            return out
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error("文档写入失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/documents/{document_id}")
    async def documents_get(
        document_id: str,
        request: Request,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50000, ge=1000, le=100000),
    ):
        try:
            out = _jsonable(current_agent(request).get_document(document_id))
            version = out.get("version") if isinstance(out, dict) else None
            if isinstance(version, dict):
                content = str(version.get("content_md", "") or "")
                total_chars = len(content)
                safe_offset = min(offset, total_chars)
                end = min(total_chars, safe_offset + limit)
                version["content_md"] = content[safe_offset:end]
                out["content_page"] = {
                    "offset": safe_offset,
                    "limit": limit,
                    "returned_chars": end - safe_offset,
                    "total_chars": total_chars,
                    "has_previous": safe_offset > 0,
                    "has_more": end < total_chars,
                }
            return out
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            logger.error("读取文档失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.delete("/api/documents/{document_id}")
    async def documents_delete(document_id: str, request: Request):
        try:
            current_agent(request).delete_document(document_id)
            return {"ok": True, "document_id": document_id}
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            logger.error("删除本地文档失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/documents/{document_id}/ingest")
    async def documents_ingest(document_id: str, request: Request):
        try:
            active_agent = current_agent(request)
            payload = {}
            try:
                payload = await request.json()
            except Exception:
                payload = {}
            version_id = str((payload or {}).get("version_id", "") or "")
            # Re-ingesting a long document performs parsing, batch embedding and
            # index writes. Keep that synchronous pipeline off the event loop so
            # health checks, refresh and chat remain responsive while it runs.
            ingest_result = await run_in_threadpool(
                active_agent.ingest_document,
                document_id,
                version_id,
            )
            res = _jsonable(ingest_result)
            if not isinstance(res, dict):
                res = _normalize_ingest_result(
                    res,
                    "",
                    document_id=document_id,
                    version_id=version_id,
                    section="document",
                )
            return res
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error("文档入库失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/tools/mcp")
    async def register_mcp_tool(req: MCPRegisterRequest, request: Request):
        try:
            active_agent = current_agent(request)
            name = req.name.strip()
            description = req.description.strip()
            endpoint = req.endpoint.strip()
            if not name or not endpoint:
                raise HTTPException(status_code=400, detail="缺少 name 或 endpoint 参数")

            active_agent.register_mcp_tool(name, description, req.params, endpoint=endpoint)
            return {"ok": True, "name": name}
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error("注册 MCP 工具失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/tools/mcp/discover")
    async def discover_mcp_server(req: MCPServerDiscoverRequest, request: Request):
        """握手并发现 MCP 服务器的工具清单，批量注册进当前用户的工具箱。

        与 /api/tools/mcp 的单工具裸 HTTP 注册互补：本端点走 MCP 协议
        （initialize → tools/list），远端工具以 mcp_ 前缀注册，执行走 tools/call。
        """
        from internal.tools.mcp_client import McpProtocolError

        try:
            active_agent = current_agent(request)
            endpoint = req.endpoint.strip()
            if not endpoint:
                raise HTTPException(status_code=400, detail="缺少 endpoint 参数")
            return await run_in_threadpool(active_agent.register_mcp_server, endpoint)
        except HTTPException:
            raise
        except ValueError as e:
            # SSRF 校验失败等注册期校验错误。
            raise HTTPException(status_code=400, detail=str(e))
        except McpProtocolError as e:
            raise HTTPException(status_code=502, detail=str(e))
        except Exception as e:
            logger.error("发现 MCP 服务器失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/status")
    async def status(request: Request):
        return current_agent(request).status()

    @app.get("/api/tools")
    async def tools(request: Request):
        return [_tool_to_main_contract(t) for t in current_agent(request).get_tools()]

    @app.get("/api/memory/", include_in_schema=False)
    @app.get("/api/memory")
    async def memory(request: Request):
        active_agent = current_agent(request)
        return {
            "long_term": _jsonable(getattr(active_agent.ltm, "items", []) or []),
            "short_term": _jsonable(active_agent.stm.get()),
            "preference": active_agent.preference.get_all(),
        }

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return PlainTextResponse("ok")

    @app.get("/readyz", include_in_schema=False)
    async def readyz():
        # Match the Go probe exactly.  Detailed dependency/readiness evidence
        # remains available from the authenticated evaluation/status APIs.
        return PlainTextResponse("ok")

    @app.get("/api/snapshots")
    async def snapshots(request: Request):
        try:
            active_agent = current_agent(request)
            items = active_agent.snapshot_list()
            return [
                {
                    "index": index,
                    "timestamp": str((item or {}).get("timestamp", "")),
                    "steps": len(((item or {}).get("state") or {}).get("steps") or []),
                }
                for index, item in enumerate(items)
            ]
        except Exception as e:
            logger.error("加载快照失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.get('/api/tasks')
    async def persisted_tasks(request: Request):
        agent = current_agent(request)
        return {'items': await run_in_threadpool(agent.inf.repo.snapshot.list, limit=50, user_id=agent.user_id)}

    @app.post('/api/tasks/{task_id}/resume')
    async def resume_persisted_task(task_id: str, body: TaskResumeRequest, request: Request):
        from internal.agent.recovery import resume_task, RecoveryConflict
        from internal.agent.conversations import ConversationBusy
        if len(task_id) > 128:
            raise HTTPException(400, '任务 ID 过长')
        try:
            return await run_in_threadpool(resume_task, current_agent(request), task_id, body.conversation_id)
        except KeyError:
            raise HTTPException(404, '任务不存在')
        except (RecoveryConflict, ConversationBusy) as exc:
            raise HTTPException(409, str(exc)) from exc

    # 静态前端：仅在目录存在时挂载，避免容器内缺失目录直接崩
    frontend_dir = os.environ.get("FRONTEND_DIR", "frontend")
    if os.path.isdir(frontend_dir):
        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
    else:
        logger.warning("⚠️  frontend 目录不存在: %s（跳过静态挂载）", frontend_dir)

    return app
