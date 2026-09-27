"""FastAPI delivery layer for auth, skills and memory governance APIs."""

from __future__ import annotations

import os
from typing import Any

import bcrypt
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, ConfigDict, Field

from internal.agent.agent import UnifiedAgent
from internal.agent.registry import AgentRegistry
from internal.fastapi_compat import iter_all_routes, register_shutdown

from .auth import AuthService, AuthenticationError, ValidationError
from .local_repos import install_local_repositories
from .skills import SkillService
from .store import ApplicationStore, ConflictError, NotFoundError


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credentials(RequestModel):
    # Go's json.Decoder ignores unknown keys and turns missing fields into the
    # string zero value; the domain validator then returns ``invalid_input``.
    # Keep that public behavior while other Python request models stay strict.
    model_config = ConfigDict(extra="ignore")
    username: str = ""
    password: str = ""


class SkillID(RequestModel):
    skill_id: str


class SkillToggle(SkillID):
    enabled: bool


class MemoryQuarantine(RequestModel):
    # ``id`` is the Go wire contract. ``ids`` keeps the Python batch extension.
    id: int | None = None
    ids: list[int] = Field(default_factory=list)
    reason: str = Field(default="manual", max_length=500)


class MemoryUnquarantine(RequestModel):
    id: int | None = None
    ids: list[int] = Field(default_factory=list)


PUBLIC_PATHS = {
    "/api/auth/register",
    "/api/auth/login",
    "/health",
    "/healthz",
    "/readyz",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
}


def _request_id(request: Request) -> str:
    return str(
        getattr(request.state, "request_id", "")
        or request.headers.get("x-request-id", "")
    )


def _auth_error_response(
    request: Request, *, status_code: int, code: str, error: str
) -> JSONResponse:
    """Return the stable Go authentication error envelope."""

    return JSONResponse(
        status_code=status_code,
        content={
            "error": error,
            "code": code,
            "request_id": _request_id(request),
        },
        headers={"WWW-Authenticate": "Bearer"} if status_code == 401 else None,
    )


def install_application_features(
    app: FastAPI,
    seed_agent,
    inf,
    cfg,
    *,
    auth_required: bool,
) -> None:
    store = ApplicationStore()
    auth = AuthService(
        store,
        secret=getattr(cfg, "auth_jwt_secret", "") or None,
        ttl_hours=getattr(cfg, "auth_jwt_ttl_hours", 7 * 24),
        issuer=getattr(cfg, "auth_jwt_issuer", "agi-assistant"),
    )
    skills = SkillService(store, cfg)
    outbox_worker = install_local_repositories(
        inf,
        store,
        trace_retention_days=getattr(cfg, "trace_retention_days", 30),
    )

    def make_agent(user_id: str):
        agent = UnifiedAgent(cfg, inf, user_id=user_id)
        skills.sync_agent(user_id, agent)
        return agent

    seed_user = _ensure_development_user(store, auth) if not auth_required else None
    registry = AgentRegistry(
        make_agent,
        seed_user_id=seed_user["id"] if seed_user is not None else "",
        seed_agent=seed_agent if not auth_required else None,
    )
    if seed_user is not None:
        seed_agent.user_id = seed_user["id"]
        if hasattr(seed_agent, "human_approval"):
            seed_agent.human_approval.journal = getattr(inf.repo, "action_journal", None)
        if hasattr(seed_agent, "ltm"):
            seed_agent.ltm.user_id = seed_user["id"]
        if hasattr(seed_agent, "preference"):
            seed_agent.preference.user_id = seed_user["id"]
            seed_agent.preference.load_from_storage()
        if hasattr(seed_agent, "rag") and seed_agent.rag is not None:
            seed_agent.rag.user_id = seed_user["id"]
            if getattr(seed_agent.rag, "_hybrid", None) is not None:
                seed_agent.rag._hybrid.user_id = seed_user["id"]
            seed_agent.rag._check_existing_chunks()
        seed_agent.chat_repo = getattr(inf.repo, "chat_history", None)

    app.state.application_store = store
    app.state.auth_service = auth
    app.state.skill_service = skills
    app.state.agent_registry = registry
    app.state.auth_required = bool(auth_required)
    app.state.development_user = seed_user
    app.state.memory_outbox_worker = outbox_worker

    @app.exception_handler(RequestValidationError)
    async def go_auth_request_validation_error(
        request: Request, exc: RequestValidationError
    ):
        # Malformed JSON or a JSON value with the wrong wire type never reaches
        # Go's auth service and is reported as invalid_body.  Missing credential
        # fields do reach our endpoint because Credentials supplies zero values.
        if request.url.path in {"/api/auth/login", "/api/auth/register"}:
            return _auth_error_response(
                request,
                status_code=400,
                code="invalid_body",
                error="请求体格式错误",
            )
        return await request_validation_exception_handler(request, exc)

    @app.middleware("http")
    async def authentication_middleware(request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or path in PUBLIC_PATHS or not path.startswith("/api/"):
            return await call_next(request)
        header = request.headers.get("authorization", "")
        bearer = header[7:].strip() if header[:7].lower() == "bearer " else ""
        if bearer:
            try:
                user = auth.verify(bearer)
            except AuthenticationError as exc:
                return _auth_error_response(
                    request,
                    status_code=401,
                    code=getattr(exc, "code", "invalid_token"),
                    error=str(exc),
                )
        elif auth_required:
            return _auth_error_response(
                request,
                status_code=401,
                code="missing_token",
                error="缺少 Authorization 头",
            )
        else:
            if seed_user is None:
                return JSONResponse(status_code=503, content={"detail": "开发用户未初始化"})
            user = seed_user
        request.state.user = user
        return await call_next(request)

    app.include_router(_router(cfg))
    _install_openapi_security(app)
    register_shutdown(app, registry.close)
    if outbox_worker is not None:
        register_shutdown(app, outbox_worker.close)
    register_shutdown(app, store.close)


def _install_openapi_security(app: FastAPI) -> None:
    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            routes=list(iter_all_routes(app.routes)),
        )
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
        }
        for path, operations in schema.get("paths", {}).items():
            if not path.startswith("/api/") or path in {"/api/auth/login", "/api/auth/register"}:
                continue
            for operation in operations.values():
                if isinstance(operation, dict) and "responses" in operation:
                    operation.setdefault("security", [{"BearerAuth": []}])
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi


def current_user(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未认证")
    return user


def current_agent(request: Request):
    agent = getattr(request.state, "agent", None)
    if agent is None:
        user = current_user(request)
        agent = request.app.state.agent_registry.get(user["id"])
        request.state.agent = agent
    return agent


def _ensure_development_user(
    store: ApplicationStore, auth: AuthService
) -> dict[str, Any]:
    username = os.getenv("AGI_DEV_USERNAME", "dev_user")
    try:
        user = store.find_user_by_username(username)
        return store.set_user_identity(
            user["id"],
            tenant_id=auth.default_tenant_id,
            roles=auth.configured_roles(username),
            identity_provenance="development_seed",
            experiment_eligible=False,
        )
    except NotFoundError:
        password = os.getenv("AGI_DEV_PASSWORD", "change-me-now")
        # The development-only bypass account is not a login recommendation;
        # keep its bootstrap cheap while real registrations remain cost 12.
        digest = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=4)).decode("ascii")
        try:
            return store.create_user(
                username,
                digest,
                tenant_id=auth.default_tenant_id,
                roles=auth.configured_roles(username),
                identity_provenance="development_seed",
                experiment_eligible=False,
            )
        except ConflictError:
            user = store.find_user_by_username(username)
            return store.set_user_identity(
                user["id"],
                tenant_id=auth.default_tenant_id,
                roles=auth.configured_roles(username),
                identity_provenance="development_seed",
                experiment_eligible=False,
            )


def _router(cfg=None) -> APIRouter:
    router = APIRouter(tags=["application"])

    @router.post("/api/auth/register")
    def register(body: Credentials, request: Request):
        try:
            return request.app.state.auth_service.register(body.username, body.password)
        except ValidationError as exc:
            return _auth_error_response(
                request, status_code=400, code="invalid_input", error=str(exc)
            )
        except ConflictError as exc:
            return _auth_error_response(
                request, status_code=409, code="user_exists", error=str(exc)
            )

    @router.post("/api/auth/login")
    def login(body: Credentials, request: Request):
        try:
            return request.app.state.auth_service.login(body.username, body.password)
        except ValidationError as exc:
            return _auth_error_response(
                request, status_code=400, code="invalid_input", error=str(exc)
            )
        except AuthenticationError:
            return _auth_error_response(
                request,
                status_code=401,
                code="invalid_credentials",
                error="用户名或密码错误",
            )

    @router.get("/api/auth/me")
    def me(request: Request):
        return current_user(request)

    @router.get("/api/traces")
    def traces_list(request: Request, limit: int = Query(default=50, ge=1, le=200)):
        user = current_user(request)
        repo = getattr(getattr(current_agent(request).inf, "repo", None), "ragtrace", None)
        if repo is None:
            return {"traces": [], "count": 0}
        traces = repo.list(user["id"], limit)
        return {"traces": traces, "count": len(traces)}

    @router.get("/api/traces/{trace_id}")
    def traces_get(trace_id: str, request: Request):
        user = current_user(request)
        repo = getattr(getattr(current_agent(request).inf, "repo", None), "ragtrace", None)
        trace = repo.get(user["id"], trace_id) if repo is not None else None
        if trace is None:
            raise HTTPException(status_code=404, detail="Trace 不存在")
        return trace

    @router.delete("/api/traces/{trace_id}")
    def traces_delete(trace_id: str, request: Request):
        user = current_user(request)
        repo = getattr(getattr(current_agent(request).inf, "repo", None), "ragtrace", None)
        if repo is None or not repo.delete(user["id"], trace_id):
            raise HTTPException(status_code=404, detail="Trace 不存在")
        return {"ok": True, "trace_id": trace_id}

    @router.post("/api/traces/purge")
    def traces_purge(request: Request):
        user = current_user(request)
        repo = getattr(getattr(current_agent(request).inf, "repo", None), "ragtrace", None)
        if repo is None or not hasattr(repo, "purge_expired"):
            return {"ok": True, "deleted": 0}
        return {"ok": True, "deleted": repo.purge_expired(user["id"])}

    @router.get("/api/rag/projections/status")
    def rag_projection_status(request: Request):
        user = current_user(request)
        worker = getattr(getattr(current_agent(request).inf, "repo", None), "rag_projection_outbox", None)
        if worker is None:
            return {"available": False, "pending": 0, "retrying": 0, "dead": 0, "processed": 0}
        return {"available": True, **worker.status(user["id"])}

    @router.post("/api/rag/projections/retry")
    def rag_projection_retry(request: Request):
        user = current_user(request)
        worker = getattr(getattr(current_agent(request).inf, "repo", None), "rag_projection_outbox", None)
        if worker is None:
            return {"ok": True, "retried": 0}
        retried = worker.retry_dead(user["id"])
        processed = worker.process_once(user_id=user["id"])
        return {"ok": True, "retried": retried, "processed": processed}

    @router.get("/api/skills/marketplace")
    def skills_marketplace(request: Request):
        current_user(request)
        return request.app.state.skill_service.marketplace_groups()

    @router.get("/api/skills/installed")
    def skills_installed(request: Request):
        user = current_user(request)
        return {"skills": request.app.state.skill_service.installed(user["id"])}

    @router.post("/api/skills/install")
    def skills_install(body: SkillID, request: Request):
        user = current_user(request)
        try:
            request.app.state.skill_service.install(user["id"], body.skill_id)
            request.app.state.skill_service.sync_agent(user["id"], current_agent(request))
            return {"ok": True, "skill_id": body.skill_id}
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @router.post("/api/skills/uninstall")
    def skills_uninstall(body: SkillID, request: Request):
        user = current_user(request)
        try:
            request.app.state.skill_service.uninstall(user["id"], body.skill_id, current_agent(request))
            return {"ok": True, "skill_id": body.skill_id}
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @router.post("/api/skills/toggle")
    def skills_toggle(body: SkillToggle, request: Request):
        user = current_user(request)
        try:
            result = request.app.state.skill_service.toggle(user["id"], body.skill_id, body.enabled, current_agent(request))
            return {"ok": True, "skill_id": body.skill_id, "enabled": bool(result.get("enabled", body.enabled))}
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @router.get("/api/memory/quarantined")
    def memory_quarantined(request: Request):
        agent = current_agent(request)
        return {"items": [_memory_item(item) for item in agent.ltm.snapshot() if item.status == "quarantined"]}

    @router.post("/api/memory/quarantine")
    def memory_quarantine(body: MemoryQuarantine, request: Request):
        agent = current_agent(request)
        single = body.id is not None
        ids = [body.id] if single else list(body.ids)
        if not ids or any(value is None or int(value) <= 0 for value in ids):
            return Response(
                content="id required\n", status_code=400, media_type="text/plain"
            )
        reason = body.reason or "manual"
        changed = agent.ltm.set_status_committed(
            [int(value) for value in ids], "quarantined", reason=reason
        )
        if single:
            return {"ok": int(body.id) in set(changed), "id": int(body.id)}
        return {"ok": True, "changed": changed}

    @router.post("/api/memory/unquarantine")
    def memory_unquarantine(body: MemoryUnquarantine, request: Request):
        agent = current_agent(request)
        single = body.id is not None
        ids = [body.id] if single else list(body.ids)
        if not ids or any(value is None or int(value) <= 0 for value in ids):
            return Response(
                content="id required\n", status_code=400, media_type="text/plain"
            )
        changed = agent.ltm.set_status_committed(
            [int(value) for value in ids], "active", reason=""
        )
        if single:
            return {"ok": int(body.id) in set(changed), "id": int(body.id)}
        return {"ok": True, "changed": changed}

    @router.get("/api/memory/superseded")
    def memory_superseded(request: Request):
        agent = current_agent(request)
        return {"items": [_memory_item(item) for item in agent.ltm.snapshot() if item.status == "superseded"]}

    return router


def _memory_item(item) -> dict[str, Any]:
    return {
        "id": item.id,
        "content": item.content,
        "importance": item.importance,
        "category": item.category,
        "tags": item.tags,
        "slot_hint": item.slot_hint,
        "score": item.score,
        "status": item.status,
        "superseded_by": item.superseded_by,
        "quarantine_reason": item.quarantine_reason,
        "created_at": item.created_at,
        "last_accessed": item.last_accessed,
    }
