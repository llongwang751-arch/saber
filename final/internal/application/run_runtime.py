"""Application-owned run runtime: configuration, database location and lazy lifecycle."""

import os
import threading
from pathlib import Path

from internal.agent.run_service import NativeRunService

_init_lock = threading.Lock()


def bounded_env_int(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not 1 <= value <= maximum:
        raise RuntimeError(f"{name} must be between 1 and {maximum}")
    return value


def _database_path(app):
    configured = getattr(app.state, "native_run_db_path", None) or os.getenv("AGI_RUN_DB_PATH")
    if configured:
        return configured
    # An explicitly isolated application database must also isolate its runs.
    # Keep the existing default path to preserve prior local run history.
    if os.getenv("AGI_EVAL_DATABASE_URL"):
        from sqlalchemy.engine import make_url

        url = make_url(os.environ["AGI_EVAL_DATABASE_URL"])
        if url.get_backend_name() == "sqlite":
            if not url.database or url.database == ":memory:":
                return ":memory:"
            database = Path(url.database)
            return database.with_name(database.stem + ".agent-runs.sqlite3")
    return Path(__file__).resolve().parents[2] / "runtime" / "agent_runs.sqlite3"


def get_run_service(request) -> NativeRunService:
    app = request.app
    service = getattr(app.state, "native_run_service", None)
    if service is None:
        with _init_lock:
            service = getattr(app.state, "native_run_service", None)
            if service is None:
                service = NativeRunService(
                    _database_path(app),
                    max_workers=bounded_env_int("AGI_RUN_MAX_WORKERS", 4, 16),
                    max_active=bounded_env_int("AGI_RUN_MAX_ACTIVE", 64, 1024),
                )
                app.state.native_run_service = service
    return service


def close_run_service(app) -> None:
    service = getattr(app.state, "native_run_service", None)
    if service is not None:
        service.close()
