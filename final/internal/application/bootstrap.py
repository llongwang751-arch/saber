"""Composition root and resource ownership for CLI and ASGI-factory startup."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from config.config import default_config
from internal.agent.agent import UnifiedAgent
from internal.fastapi_compat import register_shutdown
from internal.infra.infra import Infrastructure


@dataclass
class Deps:
    cfg: object
    inf: object
    agent: object
    app: object
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.agent.cancel()
        finally:
            try:
                self.agent.close()
            finally:
                self.inf.close()


def build_deps(cfg=None, *, infrastructure_factory=None, agent_factory=None, routes_factory=None):
    from .auth import KNOWN_DEVELOPMENT_SECRETS
    from internal.handler.handler import setup_routes

    cfg = cfg if cfg is not None else default_config()
    auth_required = os.getenv("AGI_AUTH_REQUIRED", "1").strip().lower() not in {"0", "false", "no", "off"}
    secret = str(cfg.auth_jwt_secret or "")
    if auth_required and (len(secret.encode("utf-8")) < 32 or secret in KNOWN_DEVELOPMENT_SECRETS):
        raise RuntimeError("Set AGI_JWT_SECRET to a private random value of at least 32 bytes")
    if cfg.pprof_enabled and not str(cfg.pprof_admin_token or "").strip():
        raise RuntimeError("PPROF_ADMIN_TOKEN is required when diagnostics are enabled")
    project = Path(__file__).resolve().parents[2]
    frontend = project / "web" / "dist"
    os.environ.setdefault("FRONTEND_DIR", str(frontend if frontend.is_dir() else project / "frontend"))
    inf = (infrastructure_factory or Infrastructure)(cfg)
    agent = None
    try:
        agent = (agent_factory or UnifiedAgent)(cfg, inf)
        app = (routes_factory or setup_routes)(agent, inf, cfg, auth_required=auth_required)
    except BaseException:
        try:
            if agent is not None:
                agent.close()
        finally:
            inf.close()
        raise
    deps = Deps(cfg, inf, agent, app)
    app.state.dependencies = deps
    register_shutdown(app, deps.close)
    return deps


def create_app():
    """ASGI factory: uvicorn internal.application.bootstrap:create_app --factory."""
    return build_deps().app
