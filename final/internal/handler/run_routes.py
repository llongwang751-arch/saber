"""Authenticated HTTP boundary for Saber-native background runs."""

from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from internal.application.api import current_agent
from internal.agent.plan_contracts import ResearchPlan, ResearchStep

from internal.agent.run_service import AWAITING_PLAN_REVIEW, RunCapacityExceeded, RunConflict, RunNotFound, TERMINAL

from internal.application.run_runtime import get_run_service as _service


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=20000)
    conversation_id: str = Field(default="", max_length=128, pattern=r"^[A-Za-z0-9_-]*$")
    use_rag: bool = False
    mode: Literal["chat", "research"] = "chat"


class ReviewPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "edit", "reject"]
    version: int = Field(ge=1, strict=True)
    plan: ResearchPlan | None = None
    steps: list[ResearchStep] | None = Field(default=None, min_length=1, max_length=24)


class RecoverRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


def _owner(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not isinstance(user, dict) or not user.get("id"):
        raise HTTPException(status_code=401, detail="Authentication required")
    return str(user["id"])


def _owned_call(call):
    try:
        return call()
    except RunNotFound as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    except RunConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RunCapacityExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def create_native_run_router() -> APIRouter:
    router = APIRouter(prefix="/api/agent-runs", tags=["agent-runs"])

    @router.post("")
    async def create_run(
        body: CreateRunRequest, request: Request, idempotency_key: str = Header(default="", alias="Idempotency-Key")
    ):
        owner = _owner(request)
        agent = current_agent(request)
        return await run_in_threadpool(
            _owned_call,
            lambda: _service(request).create(
                owner,
                agent,
                body.message,
                conversation_id=body.conversation_id,
                use_rag=body.use_rag,
                request_key=idempotency_key,
                mode=body.mode,
            ),
        )

    @router.post("/recover")
    async def recover_run(
        body: RecoverRunRequest, request: Request, idempotency_key: str = Header(default="", alias="Idempotency-Key")
    ):
        owner = _owner(request)
        agent = current_agent(request)
        return await run_in_threadpool(
            _owned_call,
            lambda: _service(request).create_recovery(
                owner,
                agent,
                body.task_id,
                conversation_id=body.conversation_id,
                request_key=idempotency_key,
            ),
        )

    @router.get("")
    async def list_runs(request: Request, limit: int = Query(default=50, ge=1, le=100)):
        return _service(request).list(_owner(request), limit=limit)

    @router.get("/summary")
    async def run_summary(request: Request):
        return _service(request).summary(_owner(request))

    @router.get("/{run_id}")
    async def get_run(run_id: str, request: Request):
        return _owned_call(lambda: _service(request).get(_owner(request), run_id))

    @router.get("/{run_id}/events")
    async def get_events(
        run_id: str,
        request: Request,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=500),
    ):
        return _owned_call(lambda: _service(request).events_since(_owner(request), run_id, after, limit=limit))

    @router.get("/{run_id}/plan")
    async def get_plan(run_id: str, request: Request):
        return _owned_call(lambda: _service(request).get_plan(_owner(request), run_id))

    @router.post("/{run_id}/plan/review")
    async def review_plan(run_id: str, body: ReviewPlanRequest, request: Request):
        owner = _owner(request)
        # Resolve ownership before loading the agent or modifying a plan.
        _owned_call(lambda: _service(request).get(owner, run_id))
        agent = current_agent(request)
        return await run_in_threadpool(
            _owned_call,
            lambda: _service(request).review_plan(
                owner, agent, run_id, action=body.action, version=body.version,
                plan=body.plan.model_dump(mode="json") if body.plan is not None else None,
                steps=[step.model_dump(mode="json") for step in body.steps] if body.steps is not None else None,
            ),
        )

    @router.post("/{run_id}/cancel")
    async def cancel_run(run_id: str, request: Request):
        return _owned_call(lambda: _service(request).cancel(_owner(request), run_id))

    @router.post("/{run_id}/resume")
    async def resume_research(
        run_id: str, request: Request, idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    ):
        owner = _owner(request)
        _owned_call(lambda: _service(request).get(owner, run_id))
        agent = current_agent(request)
        return await run_in_threadpool(
            _owned_call,
            lambda: _service(request).resume_research(owner, agent, run_id, request_key=idempotency_key),
        )

    @router.get("/{run_id}/stream")
    async def stream_run(run_id: str, request: Request, after: int = Query(default=0, ge=0)):
        service = _service(request)
        owner = _owner(request)
        _owned_call(lambda: service.get(owner, run_id))
        last_id = request.headers.get("last-event-id")
        if last_id:
            try:
                last_cursor = int(last_id)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="Invalid Last-Event-ID") from exc
            if last_cursor < 0:
                raise HTTPException(status_code=422, detail="Invalid Last-Event-ID")
            after = max(after, last_cursor)

        async def events():
            cursor = after
            while True:
                batch = await run_in_threadpool(service.events_since, owner, run_id, cursor)
                for event in batch:
                    cursor = event["event_id"]
                    payload = json.dumps(event["data"], ensure_ascii=False)
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {payload}\n\n"
                    if event["type"] == "done":
                        return
                if not batch and service.get(owner, run_id)["status"] in TERMINAL | {AWAITING_PLAN_REVIEW}:
                    return
                if await request.is_disconnected():
                    return
                # Cross-process events cannot notify this process's condition.
                # A short database poll keeps SSE replay responsive on any worker.
                await run_in_threadpool(service.wait_for_change, run_id, cursor, 1.0)
                yield ": heartbeat\n\n"
                await asyncio.sleep(0)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router
