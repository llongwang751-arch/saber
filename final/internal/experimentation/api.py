"""Authenticated FastAPI control plane for online RAG experiments."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from .schemas import (
    DeploymentCreate,
    ExperimentCreate,
    ExperimentDecision,
    ExperimentMutation,
    ExperimentResume,
    ExperimentStart,
    FeedbackCreate,
    FeedbackEvent,
    RampRequest,
)
from .service import ExperimentService
from .store import ExperimentConflictError


ADMIN_ROLE = "experiment_admin"
APPROVER_ROLE = "experiment_approver"


def create_experiment_router() -> APIRouter:
    router = APIRouter(prefix="/api/online-experiments", tags=["online-experiments"])

    @router.get("/readiness")
    def readiness(request: Request):
        user = _user(request)
        value = _service(request).readiness()
        return {
            **value,
            "rbac": {
                "roles": sorted(_roles(user)),
                "can_manage": ADMIN_ROLE in _roles(user),
                "can_approve": APPROVER_ROLE in _roles(user),
                "can_submit_feedback": True,
            },
        }

    @router.post("/deployments", status_code=201)
    def create_deployment(body: DeploymentCreate, request: Request):
        user = _require_role(request, ADMIN_ROLE)
        return _guard(lambda: _service(request).create_deployment(
            _tenant(user),
            body.proposal_id,
            actor=_actor(user),
            idempotency_key=body.idempotency_key,
        ))

    @router.get("/deployments")
    def list_deployments(
        request: Request,
        limit: int = Query(default=200, ge=1, le=500),
    ):
        user = _require_management_reader(request)
        return {"items": _guard(lambda: _service(request).list_deployments(
            _tenant(user), limit=limit
        ))}

    @router.get("/deployments/{deployment_id}")
    def get_deployment(deployment_id: str, request: Request):
        user = _require_management_reader(request)
        return _guard(lambda: _service(request).get_deployment(
            _tenant(user), deployment_id
        ))

    @router.post("/experiments", status_code=201)
    def create_experiment(body: ExperimentCreate, request: Request):
        user = _require_role(request, ADMIN_ROLE)
        values = body.model_dump(
            exclude={"idempotency_key", "surface"}, exclude_none=True
        )
        return _guard(lambda: _service(request).create_experiment(
            _tenant(user),
            **values,
            creator=_actor(user),
            idempotency_key=body.idempotency_key,
        ))

    @router.get("/experiments")
    def list_experiments(
        request: Request,
        status: str | None = Query(default=None, max_length=32),
        limit: int = Query(default=200, ge=1, le=500),
    ):
        user = _require_management_reader(request)
        return {"items": _guard(lambda: _service(request).list_experiments(
            _tenant(user), status=status, limit=limit
        ))}

    @router.get("/experiments/{experiment_id}")
    def get_experiment(experiment_id: str, request: Request):
        user = _require_management_reader(request)
        return _guard(lambda: _service(request).get_experiment(
            _tenant(user), experiment_id
        ))

    @router.post("/experiments/{experiment_id}/submit")
    def submit_experiment(
        experiment_id: str, body: ExperimentMutation, request: Request
    ):
        user = _require_role(request, ADMIN_ROLE)
        return _mutation(request, user, experiment_id, body, "submit_experiment")

    @router.post("/experiments/{experiment_id}/decision")
    def decide_experiment(
        experiment_id: str, body: ExperimentDecision, request: Request
    ):
        user = _require_role(request, APPROVER_ROLE)
        return _guard(lambda: _service(request).review_experiment(
            _tenant(user),
            experiment_id,
            decision=body.decision,
            actor=_actor(user),
            expected_generation=body.expected_generation,
            idempotency_key=body.idempotency_key,
            reason=body.reason,
        ))

    @router.post("/experiments/{experiment_id}/start")
    def start_experiment(
        experiment_id: str, body: ExperimentStart, request: Request
    ):
        user = _require_role(request, ADMIN_ROLE)
        return _guard(lambda: _service(request).start_experiment(
            _tenant(user),
            experiment_id,
            actor=_actor(user),
            expected_generation=body.expected_generation,
            idempotency_key=body.idempotency_key,
            reason=body.reason,
            target_status=body.target_status,
        ))

    @router.post("/experiments/{experiment_id}/ramp")
    def ramp_experiment(
        experiment_id: str, body: RampRequest, request: Request
    ):
        user = _require_role(request, ADMIN_ROLE)
        return _guard(lambda: _service(request).ramp_experiment(
            _tenant(user),
            experiment_id,
            target_enrollment_bps=body.target_enrollment_bps,
            actor=_actor(user),
            expected_generation=body.expected_generation,
            idempotency_key=body.idempotency_key,
            reason=body.reason,
        ))

    @router.post("/experiments/{experiment_id}/pause")
    def pause_experiment(
        experiment_id: str, body: ExperimentMutation, request: Request
    ):
        user = _require_role(request, ADMIN_ROLE)
        return _mutation(request, user, experiment_id, body, "pause_experiment")

    @router.post("/experiments/{experiment_id}/resume")
    def resume_experiment(
        experiment_id: str, body: ExperimentResume, request: Request
    ):
        user = _user(request)
        current = _guard(lambda: _service(request).get_experiment(
            _tenant(user), experiment_id
        ))
        safety_resume = current["status"] == "safety_paused"
        required = APPROVER_ROLE if safety_resume else ADMIN_ROLE
        _assert_role(user, required)
        return _guard(lambda: _service(request).resume_experiment(
            _tenant(user),
            experiment_id,
            actor=_actor(user),
            expected_generation=body.expected_generation,
            idempotency_key=body.idempotency_key,
            reason=body.reason,
            target_status=body.target_status,
            safety_approved=safety_resume,
        ))

    @router.post("/experiments/{experiment_id}/complete")
    def complete_experiment(
        experiment_id: str, body: ExperimentMutation, request: Request
    ):
        user = _require_role(request, ADMIN_ROLE)
        return _mutation(request, user, experiment_id, body, "complete_experiment")

    @router.post("/experiments/{experiment_id}/rollback")
    def rollback_experiment(
        experiment_id: str, body: ExperimentMutation, request: Request
    ):
        user = _require_role(request, ADMIN_ROLE)
        return _mutation(request, user, experiment_id, body, "rollback_experiment")

    @router.get("/experiments/{experiment_id}/analysis")
    def analyze_experiment(experiment_id: str, request: Request):
        user = _require_management_reader(request)
        return _guard(lambda: _service(request).analyze_experiment(
            _tenant(user), experiment_id
        ))

    @router.get("/audit-events")
    def audit_events(
        request: Request,
        experiment_id: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=1000),
    ):
        user = _require_management_reader(request)
        return {"items": _guard(lambda: _service(request).list_audit_events(
            _tenant(user), experiment_id=experiment_id, limit=limit
        ))}

    @router.get("/experiments/{experiment_id}/audit")
    def experiment_audit(
        experiment_id: str,
        request: Request,
        limit: int = Query(default=200, ge=1, le=1000),
    ):
        user = _require_management_reader(request)
        # Verify tenant ownership even if the audit log is currently empty.
        _guard(lambda: _service(request).get_experiment(_tenant(user), experiment_id))
        return {"items": _guard(lambda: _service(request).list_audit_events(
            _tenant(user), experiment_id=experiment_id, limit=limit
        ))}

    @router.put("/exposures/{exposure_id}/feedback")
    def put_feedback(exposure_id: str, body: FeedbackEvent, request: Request):
        user = _user(request)
        return _guard(lambda: _service(request).record_feedback(
            _tenant(user), _actor(user), exposure_id, body.event_id, body.rating
        ))

    @router.post("/feedback")
    def post_feedback(body: FeedbackCreate, request: Request):
        """Compatibility route; path-addressed PUT is the canonical API."""

        user = _user(request)
        return _guard(lambda: _service(request).record_feedback(
            _tenant(user), _actor(user), body.exposure_id, body.event_id, body.rating
        ))

    return router


def _mutation(
    request: Request,
    user: dict[str, Any],
    experiment_id: str,
    body: ExperimentMutation,
    method: str,
) -> Any:
    operation = getattr(_service(request), method)
    return _guard(lambda: operation(
        _tenant(user),
        experiment_id,
        actor=_actor(user),
        expected_generation=body.expected_generation,
        idempotency_key=body.idempotency_key,
        reason=body.reason,
    ))


def _service(request: Request) -> ExperimentService:
    service = getattr(request.app.state, "experiment_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="在线实验服务不可用")
    return service


def _user(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    if not isinstance(user, dict) or not str(user.get("id") or "").strip():
        raise HTTPException(status_code=401, detail="未认证")
    return user


def _roles(user: dict[str, Any]) -> set[str]:
    value = user.get("roles") or []
    return {str(item) for item in value} if isinstance(value, list) else set()


def _assert_role(user: dict[str, Any], role: str) -> None:
    if role not in _roles(user):
        raise HTTPException(status_code=403, detail="没有执行该在线实验操作的权限")


def _require_role(request: Request, role: str) -> dict[str, Any]:
    user = _user(request)
    _assert_role(user, role)
    return user


def _require_management_reader(request: Request) -> dict[str, Any]:
    user = _user(request)
    if not (_roles(user) & {ADMIN_ROLE, APPROVER_ROLE}):
        raise HTTPException(status_code=403, detail="没有查看在线实验控制面的权限")
    return user


def _tenant(user: dict[str, Any]) -> str:
    tenant_id = str(user.get("tenant_id") or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=401, detail="认证身份缺少 tenant_id")
    return tenant_id


def _actor(user: dict[str, Any]) -> str:
    return str(user["id"])


def _guard(operation: Callable[[], Any]) -> Any:
    try:
        return operation()
    except HTTPException:
        raise
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExperimentConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


__all__ = ["create_experiment_router"]
