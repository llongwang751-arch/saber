"""Runtime routes registered by the application composition root."""

import logging
import uuid
from types import SimpleNamespace

from fastapi import HTTPException, Request
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from internal.application.api import current_agent
from internal.application.readiness import install_readiness

from .models import TaskResumeRequest
from .http_contracts import _tool_to_main_contract, _jsonable

logger = logging.getLogger(__name__)


def register_runtime_routes(app, agent, inf, cfg):
    readiness = install_readiness(app, agent, inf, cfg)
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

    @app.post("/api/conversations")
    async def create_conversation(request: Request):
        current_agent(request)  # Apply the same authentication boundary as chat.
        return {"conversation_id": str(uuid.uuid4())}

    @app.get("/api/status")
    async def status(request: Request):
        from config.research import feature_status

        result = dict(current_agent(request).status())
        result["features"] = feature_status(cfg)
        result["research"] = {"plan_review_required": True, "runtime": "native"}
        return result

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
        if readiness.checks:
            report = await run_in_threadpool(readiness.snapshot)
            if not report["ready"]:
                return PlainTextResponse("not ready", status_code=503)
        return PlainTextResponse("ok")

    @app.get("/api/ops/readiness")
    async def readiness_report(request: Request):
        if not getattr(request.state, "user", None):
            raise HTTPException(401, "Authentication required")
        return await run_in_threadpool(readiness.snapshot)

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

    @app.get("/api/tasks")
    async def persisted_tasks(request: Request):
        agent = current_agent(request)
        return {"items": await run_in_threadpool(agent.inf.repo.snapshot.list, limit=50, user_id=agent.user_id)}

    @app.post("/api/tasks/{task_id}/resume")
    async def resume_persisted_task(task_id: str, body: TaskResumeRequest, request: Request):
        from internal.agent.recovery import resume_task, RecoveryConflict
        from internal.agent.conversations import ConversationBusy

        if len(task_id) > 128:
            raise HTTPException(400, "任务 ID 过长")
        try:
            return await run_in_threadpool(resume_task, current_agent(request), task_id, body.conversation_id)
        except KeyError:
            raise HTTPException(404, "任务不存在")
        except (RecoveryConflict, ConversationBusy) as exc:
            raise HTTPException(409, str(exc)) from exc
