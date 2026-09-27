# handler — HTTP API 路由处理（FastAPI + Pydantic + CORS）
import asyncio
import logging
import os
import hmac
import re
import threading
import time
import uuid
import gc
import sys
import tracemalloc

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from config.config import APIConfig
from internal.fastapi_compat import app_lifespan, register_shutdown
from internal.application.api import install_application_features
from internal.application.run_runtime import close_run_service
from internal.agent.agent import UnifiedAgent
from .run_routes import create_native_run_router
from internal.evaluation.api import create_evaluation_router
from internal.evaluation.service import EvaluationService, EvaluationServiceRegistry
from internal.infra.infra import Infrastructure


from .models import (
    ChatRequest as ChatRequest,
    ApprovalDecision as ApprovalDecision,
    TaskResumeRequest as TaskResumeRequest,
    MCPParam as MCPParam,
    MCPRegisterRequest as MCPRegisterRequest,
    MCPServerDiscoverRequest as MCPServerDiscoverRequest,
    DocsDeleteRequest as DocsDeleteRequest,
    UploadJSONRequest as UploadJSONRequest,
    RAGLabRequest as RAGLabRequest,
)
from .http_contracts import (
    _response_to_dict as _response_to_dict,
    _rag_result_to_main_contract as _rag_result_to_main_contract,
    _tool_to_main_contract as _tool_to_main_contract,
    _sse as _sse,
    _jsonable as _jsonable,
    _sanitize_stream_done as _sanitize_stream_done,
    _normalize_ingest_result as _normalize_ingest_result,
)
from .chat_experiments import (
    _begin_online_rag_exposure as _begin_online_rag_exposure,
    _finish_online_rag_exposure as _finish_online_rag_exposure,
)
from .chat_routes import register_chat_routes
from .document_routes import register_document_routes
from .tool_routes import register_tool_routes
from .runtime_routes import register_runtime_routes

logger = logging.getLogger(__name__)

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
    app.include_router(create_native_run_router())
    register_shutdown(app, evaluation_service.close)
    register_shutdown(app, lambda: close_run_service(app))

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
            return PlainTextResponse("Types of profiles available: threads, heap, objects, cmdline, trace\n")

        @app.get("/debug/pprof/cmdline", include_in_schema=False)
        async def debug_cmdline(request: Request):
            _debug_authorized(request)
            return PlainTextResponse("\x00".join(sys.argv))

        @app.api_route("/debug/pprof/{profile}", methods=["GET", "POST"], include_in_schema=False)
        async def debug_profile(profile: str, request: Request):
            _debug_authorized(request)
            if profile in {"goroutine", "thread", "threads"}:
                frames = sys._current_frames()
                return JSONResponse(
                    {
                        "threads": [
                            {"name": item.name, "ident": item.ident, "frame": bool(frames.get(item.ident))}
                            for item in threading.enumerate()
                        ]
                    }
                )
            if profile in {"heap", "allocs", "objects"}:
                if not tracemalloc.is_tracing():
                    tracemalloc.start(10)
                current, peak = tracemalloc.get_traced_memory()
                return JSONResponse({"tracked_bytes": current, "peak_bytes": peak, "gc_objects": len(gc.get_objects())})
            if profile in {"symbol", "trace", "profile"}:
                return JSONResponse({"profile": profile, "runtime": "python", "supported": True})
            raise HTTPException(status_code=404, detail="Not Found")

    if auth_required:
        evaluation_registry = EvaluationServiceRegistry(local_agent_factory=app.state.agent_registry.get)
        app.state.evaluation_service_registry = evaluation_registry
        experiment_service = getattr(app.state, "experiment_service", None)
        if experiment_service is not None:
            application_store_ready = bool(getattr(experiment_service, "production_evidence_ready", False))
            experiment_service.production_evidence_ready = bool(
                application_store_ready and evaluation_registry.production_shared_ready
            )
            experiment_service.production_evidence_backends = {
                "application": getattr(experiment_service, "storage_backend", "unknown"),
                "offline_evaluation": evaluation_registry.storage_backend,
                "shared_offline_evaluation": (evaluation_registry.production_shared_ready),
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

    register_runtime_routes(app, agent, inf, cfg)

    register_chat_routes(app, agent, inf, cfg)

    register_tool_routes(app, agent, inf, cfg)

    register_document_routes(app, agent, inf, cfg)

    # 静态前端：仅在目录存在时挂载，避免容器内缺失目录直接崩
    frontend_dir = os.environ.get("FRONTEND_DIR", "web/dist")
    if os.path.isdir(frontend_dir):
        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
    else:
        logger.warning("⚠️  frontend 目录不存在: %s（跳过静态挂载）", frontend_dir)

    return app
