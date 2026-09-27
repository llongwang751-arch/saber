"""Request-scoped experiment enrollment and terminal accounting."""

import logging
import hashlib
import json
import time
from typing import Any

from fastapi import HTTPException, Request

from internal.agent.agent import RequestExecutionContext, Response
from internal.experimentation.store import IdempotencyConflictError

from .models import ChatRequest

logger = logging.getLogger(__name__)


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
        exposure_id = str(merged.get("opaque_exposure_id") or merged.get("exposure_id") or "").strip()
        if not exposure_id:
            raise RuntimeError("experiment exposure ledger returned no exposure id")
        runtime_overrides = merged.get("runtime_overrides") or {}
        if hasattr(runtime_overrides, "runtime_payload"):
            runtime_overrides = runtime_overrides.runtime_payload()
        context = RequestExecutionContext(
            runtime_overrides=dict(runtime_overrides or {}),
            experiment_exposure_id=exposure_id,
            runtime_strategy_checksum=str(
                merged.get("runtime_strategy_checksum") or merged.get("compiled_checksum") or ""
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
