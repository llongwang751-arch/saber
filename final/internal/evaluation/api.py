"""FastAPI surface for datasets, runs, Trace analysis and Badcase closure."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .schemas import EvalCase, ReleaseGate
from .service import EvaluationService


EXPERIMENT_ADMIN_ROLE = "experiment_admin"
EXPERIMENT_APPROVER_ROLE = "experiment_approver"


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DatasetCreateRequest(_RequestModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    domain: str = "agent-quality"
    metadata: dict[str, Any] = Field(default_factory=dict)


class DatasetVersionImportRequest(_RequestModel):
    cases: list[EvalCase] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunCreateRequest(_RequestModel):
    dataset_version_id: str = Field(min_length=1)
    strategy_version_id: str | None = Field(default=None, min_length=1)
    name: str = ""
    adapter: dict[str, Any] = Field(
        default_factory=lambda: {"type": "replay", "profile": "baseline"}
    )
    release_gate: ReleaseGate = Field(default_factory=ReleaseGate)
    metadata: dict[str, Any] = Field(default_factory=dict)
    execute: bool = False


class RunCompareRequest(_RequestModel):
    baseline_run_id: str = Field(min_length=1)
    candidate_run_id: str = Field(min_length=1)


class BadcaseTriageRequest(_RequestModel):
    category: str | None = None
    severity: Literal["low", "medium", "high", "critical"] | None = None
    # Terminal states are verification outcomes, never free-form triage input.
    status: Literal["open", "triaged", "investigating"] | None = None
    owner: str | None = None
    resolution: dict[str, Any] | None = None


class AnnotationCreateRequest(_RequestModel):
    annotator: str = Field(min_length=1)
    annotation: dict[str, Any]
    case_run_id: str | None = None
    badcase_id: str | None = None
    eval_case_id: str | None = None


class DemoBootstrapRequest(_RequestModel):
    execute: bool = True


class BadcaseVerifyRequest(_RequestModel):
    candidate_run_id: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    note: str = ""


class StrategyCreateRequest(_RequestModel):
    name: str = Field(min_length=1, max_length=200)
    manifest: dict[str, Any] = Field(min_length=1)


class PromotionCreateRequest(_RequestModel):
    baseline_run_id: str = Field(min_length=1)
    candidate_run_id: str = Field(min_length=1)


class PromotionDecisionRequest(_RequestModel):
    decision: Literal["approve", "reject"]
    note: str = Field(default="", max_length=5000)


class PromotionActivateRequest(_RequestModel):
    note: str = Field(default="", max_length=5000)


class StrategyRollbackRequest(_RequestModel):
    idempotency_key: str = Field(min_length=1, max_length=200)
    reason: str = Field(default="", max_length=5000)
    expected_current_strategy_version_id: str | None = Field(default=None, min_length=1)


class EvolutionSuggestionCreateRequest(_RequestModel):
    source_run_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=200)


class EvolutionSuggestionDecisionRequest(_RequestModel):
    decision: Literal["accept", "reject"]
    note: str = Field(default="", max_length=5000)
    expected_generation: StrictInt = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=200)


class EvolutionSuggestionMaterializeRequest(_RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    expected_generation: StrictInt = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=200)


def create_evaluation_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/eval",
        tags=["Agent Evaluation"],
        dependencies=[Depends(_require_production_reader)],
    )

    @router.get("/readiness")
    def evaluation_readiness(request: Request):
        """Expose Replay and Local execution truth without probing providers."""

        return _guard(lambda: _service(request).readiness())

    @router.post("/demo/bootstrap", status_code=201)
    def bootstrap_demo(body: DemoBootstrapRequest, request: Request):
        _require_production_role(request, EXPERIMENT_ADMIN_ROLE)
        return _guard(lambda: _service(request).bootstrap_demo(execute=body.execute))

    @router.post("/demo/live", status_code=201)
    def bootstrap_live_demo(request: Request):
        _require_production_role(request, EXPERIMENT_ADMIN_ROLE)
        service = _service(request)
        readiness = _guard(service.readiness)
        if not readiness["live"]["ready"]:
            reasons = readiness["live"].get("reasons") or ["Local Agent 不可用"]
            raise HTTPException(status_code=503, detail=str(reasons[0]))
        return _guard(service.bootstrap_live_demo)

    @router.post("/datasets", status_code=201)
    def create_dataset(body: DatasetCreateRequest, request: Request):
        _require_production_role(request, EXPERIMENT_ADMIN_ROLE)
        return _guard(
            lambda: _service(request).create_dataset(
                body.name,
                description=body.description,
                domain=body.domain,
                metadata=body.metadata,
            )
        )

    @router.get("/datasets")
    def list_datasets(request: Request):
        return {"datasets": _guard(lambda: _service(request).store.list_datasets())}

    @router.get("/metrics/prometheus", include_in_schema=True)
    def prometheus_metrics(request: Request):
        return Response(
            content=_guard(lambda: _service(request).prometheus_metrics()),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @router.get("/metrics/summary")
    def metrics_summary(request: Request):
        return _guard(lambda: _service(request).metrics_summary())

    @router.post("/evolution-suggestions", status_code=201)
    def create_evolution_suggestion(
        body: EvolutionSuggestionCreateRequest,
        request: Request,
    ):
        actor = _require_control_role(request, EXPERIMENT_ADMIN_ROLE)
        return _guard(
            lambda: _service(request).create_evolution_suggestion(
                body.source_run_id,
                actor=actor,
                idempotency_key=body.idempotency_key,
            )
        )

    @router.get("/evolution-suggestions")
    def list_evolution_suggestions(
        request: Request,
        status: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
    ):
        _actor(request)
        return {
            "suggestions": _guard(
                lambda: _service(request).list_evolution_suggestions(
                    status=status,
                    limit=limit,
                )
            ),
            "scope": "offline_evaluation",
            "auto_apply": False,
        }

    # Static evolution paths must precede /evolution-suggestions/{id}.
    @router.get("/evolution-suggestions/audit-events")
    def list_evolution_audit_events(
        request: Request,
        suggestion_id: str | None = None,
        limit: int = Query(default=500, ge=1, le=5000),
    ):
        _actor(request)
        return {
            "events": _guard(
                lambda: _service(request).list_evolution_audit_events(
                    suggestion_id=suggestion_id,
                    limit=limit,
                )
            ),
            "scope": "offline_evaluation",
        }

    @router.get("/evolution-suggestions/{suggestion_id}")
    def get_evolution_suggestion(suggestion_id: str, request: Request):
        _actor(request)
        return _guard(
            lambda: _service(request).get_evolution_suggestion(suggestion_id)
        )

    @router.post("/evolution-suggestions/{suggestion_id}/decision")
    def decide_evolution_suggestion(
        suggestion_id: str,
        body: EvolutionSuggestionDecisionRequest,
        request: Request,
    ):
        actor = _require_control_role(request, EXPERIMENT_APPROVER_ROLE)
        return _guard(
            lambda: _service(request).review_evolution_suggestion(
                suggestion_id,
                decision=body.decision,
                reviewer=actor,
                note=body.note,
                expected_generation=body.expected_generation,
                idempotency_key=body.idempotency_key,
            )
        )

    @router.post("/evolution-suggestions/{suggestion_id}/materialize")
    def materialize_evolution_suggestion(
        suggestion_id: str,
        body: EvolutionSuggestionMaterializeRequest,
        request: Request,
    ):
        actor = _require_control_role(request, EXPERIMENT_ADMIN_ROLE)
        return _guard(
            lambda: _service(request).materialize_evolution_suggestion(
                suggestion_id,
                name=body.name,
                actor=actor,
                expected_generation=body.expected_generation,
                idempotency_key=body.idempotency_key,
            )
        )

    @router.post("/strategies", status_code=201)
    def create_strategy(body: StrategyCreateRequest, request: Request):
        actor = _require_control_role(request, EXPERIMENT_ADMIN_ROLE)
        return _guard(
            lambda: _service(request).create_strategy_version(
                body.name,
                body.manifest,
                creator=actor,
            )
        )

    @router.get("/strategies")
    def list_strategies(
        request: Request,
        limit: int = Query(default=200, ge=1, le=1000),
    ):
        return {
            "strategies": _guard(
                lambda: _service(request).list_strategy_versions(limit=limit)
            ),
            "scope": "offline_evaluation",
        }

    # Static strategy paths must precede /strategies/{strategy_version_id}.
    @router.get("/strategies/current")
    def current_strategy(request: Request):
        return _guard(lambda: _service(request).get_current_strategy())

    @router.post("/strategies/rollback")
    def rollback_strategy(body: StrategyRollbackRequest, request: Request):
        actor = _require_control_role(request, EXPERIMENT_ADMIN_ROLE)
        return _guard(
            lambda: _service(request).rollback_strategy(
                actor=actor,
                idempotency_key=body.idempotency_key,
                reason=body.reason,
                expected_current_strategy_version_id=(
                    body.expected_current_strategy_version_id
                ),
            )
        )

    @router.get("/strategies/{strategy_version_id}")
    def get_strategy(strategy_version_id: str, request: Request):
        return _guard(
            lambda: _service(request).get_strategy_version(strategy_version_id)
        )

    @router.post("/promotion-proposals", status_code=201)
    def create_promotion_proposal(body: PromotionCreateRequest, request: Request):
        actor = _require_control_role(request, EXPERIMENT_ADMIN_ROLE)
        return _guard(
            lambda: _service(request).create_promotion_proposal(
                body.baseline_run_id,
                body.candidate_run_id,
                creator=actor,
            )
        )

    @router.get("/promotion-proposals")
    def list_promotion_proposals(
        request: Request,
        status: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
    ):
        return {
            "proposals": _guard(
                lambda: _service(request).list_promotion_proposals(
                    status=status,
                    limit=limit,
                )
            ),
            "scope": "offline_evaluation",
        }

    @router.get("/promotion-proposals/{proposal_id}")
    def get_promotion_proposal(proposal_id: str, request: Request):
        return _guard(
            lambda: _service(request).get_promotion_proposal(proposal_id)
        )

    @router.post("/promotion-proposals/{proposal_id}/decision")
    def decide_promotion_proposal(
        proposal_id: str,
        body: PromotionDecisionRequest,
        request: Request,
    ):
        actor = _require_control_role(request, EXPERIMENT_APPROVER_ROLE)
        return _guard(
            lambda: _service(request).review_promotion_proposal(
                proposal_id,
                decision=body.decision,
                reviewer=actor,
                note=body.note,
            )
        )

    @router.post("/promotion-proposals/{proposal_id}/activate")
    def activate_promotion_proposal(
        proposal_id: str,
        body: PromotionActivateRequest,
        request: Request,
    ):
        actor = _require_control_role(request, EXPERIMENT_ADMIN_ROLE)
        return _guard(
            lambda: _service(request).activate_promotion_proposal(
                proposal_id,
                actor=actor,
                note=body.note,
            )
        )

    @router.get("/strategy-audit-events")
    def list_strategy_audit_events(
        request: Request,
        proposal_id: str | None = None,
        limit: int = Query(default=500, ge=1, le=5000),
    ):
        return {
            "events": _guard(
                lambda: _service(request).store.list_strategy_audit_events(
                    proposal_id=proposal_id,
                    limit=limit,
                )
            ),
            "scope": "offline_evaluation",
        }

    @router.get("/datasets/{dataset_id}")
    def get_dataset(dataset_id: str, request: Request):
        return _guard(lambda: _service(request).store.get_dataset(dataset_id))

    @router.get("/datasets/{dataset_id}/runs")
    def list_dataset_runs(
        dataset_id: str,
        request: Request,
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        return {
            "runs": _guard(
                lambda: _service(request).store.list_runs(
                    dataset_id=dataset_id,
                    limit=limit,
                )
            )
        }

    @router.get("/datasets/{dataset_id}/versions")
    def list_versions(dataset_id: str, request: Request):
        return {
            "versions": _guard(
                lambda: _service(request).store.list_dataset_versions(dataset_id)
            )
        }

    @router.post("/datasets/{dataset_id}/versions/import", status_code=201)
    def import_version(
        dataset_id: str,
        body: DatasetVersionImportRequest,
        request: Request,
    ):
        _require_production_role(request, EXPERIMENT_ADMIN_ROLE)
        return _guard(
            lambda: _service(request).import_cases(
                dataset_id,
                body.cases,
                metadata=body.metadata,
            )
        )

    @router.get("/dataset-versions/{version_id}/cases")
    def list_cases(version_id: str, request: Request):
        return {
            "cases": _guard(
                lambda: _service(request).store.get_version_cases(version_id)
            )
        }

    @router.get("/dataset-versions/{version_id}")
    def get_dataset_version(version_id: str, request: Request):
        return _guard(lambda: _service(request).store.get_dataset_version(version_id))

    @router.post("/runs", status_code=201)
    def create_run(body: RunCreateRequest, request: Request):
        _require_production_role(request, EXPERIMENT_ADMIN_ROLE)
        service = _service(request)
        run = _guard(
            lambda: service.create_run(
                body.dataset_version_id,
                name=body.name,
                adapter=body.adapter,
                release_gate=body.release_gate,
                metadata=body.metadata,
                strategy_version_id=body.strategy_version_id,
            )
        )
        if body.execute:
            run = _guard(lambda: service.start_run(run["id"]))
        return run

    @router.get("/runs")
    def list_runs(
        request: Request,
        dataset_version_id: str | None = None,
        strategy_version_id: str | None = None,
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        return {
            "runs": _guard(
                lambda: _service(request).store.list_runs(
                    dataset_version_id=dataset_version_id,
                    strategy_version_id=strategy_version_id,
                    status=status,
                    limit=limit,
                )
            )
        }

    # Static path must be registered before /runs/{run_id}.
    @router.post("/runs/compare")
    def compare_runs(body: RunCompareRequest, request: Request):
        return _guard(
            lambda: _service(request).compare_runs(
                body.baseline_run_id,
                body.candidate_run_id,
            )
        )

    @router.get("/runs/{run_id}")
    def get_run(run_id: str, request: Request):
        return _guard(lambda: _service(request).store.get_run(run_id))

    @router.post("/runs/{run_id}/execute", status_code=202)
    def execute_run(run_id: str, request: Request):
        _require_production_role(request, EXPERIMENT_ADMIN_ROLE)
        return _guard(lambda: _service(request).start_run(run_id))

    @router.post("/runs/{run_id}/cancel")
    def cancel_run(run_id: str, request: Request):
        _require_production_role(request, EXPERIMENT_ADMIN_ROLE)
        return _guard(lambda: _service(request).cancel_run(run_id))

    @router.get("/runs/{run_id}/progress")
    def run_progress(run_id: str, request: Request):
        return _guard(lambda: _service(request).progress(run_id))

    @router.get("/runs/{run_id}/summary")
    def run_summary(run_id: str, request: Request):
        return _guard(lambda: _service(request).summary(run_id))

    @router.get("/runs/{run_id}/gate")
    def run_release_gate(run_id: str, request: Request):
        run = _guard(lambda: _service(request).store.get_run(run_id))
        summary = run.get("summary") or {}
        return {
            "run_id": run_id,
            "status": run["status"],
            "passed": summary.get("release_gate_passed"),
            "thresholds": summary.get("release_gate") or {},
            "pass_rate": summary.get("pass_rate"),
            "error_rate": summary.get("error_rate"),
            "p95_latency_ms": summary.get("p95_latency_ms"),
            "hard_gate_failures": summary.get("hard_gate_failures"),
        }

    @router.get("/runs/{run_id}/results")
    def run_results(run_id: str, request: Request):
        return {
            "results": _guard(
                lambda: _service(request).store.list_case_results(run_id)
            )
        }

    @router.get("/runs/{run_id}/report")
    def run_report(
        run_id: str,
        request: Request,
        format: Literal["markdown", "csv"] = "markdown",
    ):
        service = _service(request)
        if format == "csv":
            content = _guard(lambda: service.csv_report(run_id))
            return Response(
                content=content,
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="eval-{run_id}.csv"'},
            )
        content = _guard(lambda: service.markdown_report(run_id))
        return Response(
            content=content,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'inline; filename="eval-{run_id}.md"'},
        )

    @router.get("/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request):
        service = _service(request)
        _guard(lambda: service.store.get_run(run_id))

        async def generate():
            last_payload = None
            while True:
                if await request.is_disconnected():
                    return
                payload = _guard(lambda: service.progress(run_id))
                encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                if encoded != last_payload:
                    event = "done" if payload["status"] in {"completed", "failed", "cancelled"} else "progress"
                    yield f"event: {event}\ndata: {encoded}\n\n"
                    last_payload = encoded
                if payload["status"] in {"completed", "failed", "cancelled"}:
                    return
                await asyncio.sleep(0.25)

        return StreamingResponse(generate(), media_type="text/event-stream")

    @router.get("/case-runs/{case_run_id}")
    def get_case_run(case_run_id: str, request: Request):
        return _guard(lambda: _service(request).store.get_case_result(case_run_id))

    @router.get("/case-runs/{case_run_id}/trace")
    def get_case_trace(case_run_id: str, request: Request):
        result = _guard(lambda: _service(request).store.get_case_result(case_run_id))
        return {
            "case_run_id": case_run_id,
            "case_id": result["case_id"],
            "trace": result.get("trace") or [],
        }

    @router.get("/badcases")
    def list_badcases(
        request: Request,
        run_id: str | None = None,
        status: str | None = None,
        category: str | None = None,
        severity: str | None = None,
        owner: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
    ):
        return {
            "badcases": _guard(
                lambda: _service(request).store.list_badcases(
                    run_id=run_id,
                    status=status,
                    category=category,
                    severity=severity,
                    owner=owner,
                    limit=limit,
                )
            )
        }

    @router.get("/runs/{run_id}/badcases")
    def list_run_badcases(
        run_id: str,
        request: Request,
        limit: int = Query(default=200, ge=1, le=1000),
    ):
        return {
            "badcases": _guard(
                lambda: _service(request).store.list_badcases(run_id=run_id, limit=limit)
            )
        }

    @router.get("/badcases/{badcase_id}")
    def get_badcase(badcase_id: str, request: Request):
        return _guard(lambda: _service(request).store.get_badcase(badcase_id))

    @router.post("/badcases/{badcase_id}/triage")
    def triage_badcase(
        badcase_id: str,
        body: BadcaseTriageRequest,
        request: Request,
    ):
        _require_production_role(request, EXPERIMENT_ADMIN_ROLE)
        return _guard(
            lambda: _service(request).store.triage_badcase(
                badcase_id,
                category=body.category,
                severity=body.severity,
                status=body.status,
                owner=body.owner,
                resolution=body.resolution,
            )
        )

    @router.post("/badcases/{badcase_id}/verify")
    def verify_badcase(
        badcase_id: str,
        body: BadcaseVerifyRequest,
        request: Request,
    ):
        authenticated_reviewer = _require_production_role(
            request, EXPERIMENT_APPROVER_ROLE
        )
        return _guard(
            lambda: _service(request).verify_badcase(
                badcase_id,
                candidate_run_id=body.candidate_run_id,
                reviewer=authenticated_reviewer or body.reviewer,
                note=body.note,
            )
        )

    @router.post("/annotations", status_code=201)
    def create_annotation(body: AnnotationCreateRequest, request: Request):
        authenticated_annotator = _production_actor(request)
        return _guard(
            lambda: _service(request).store.create_annotation(
                annotator=authenticated_annotator or body.annotator,
                annotation=body.annotation,
                case_run_id=body.case_run_id,
                badcase_id=body.badcase_id,
                eval_case_id=body.eval_case_id,
            )
        )

    @router.get("/annotations")
    def list_annotations(
        request: Request,
        case_run_id: str | None = None,
        badcase_id: str | None = None,
        eval_case_id: str | None = None,
        annotator: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
    ):
        return {
            "annotations": _guard(
                lambda: _service(request).store.list_annotations(
                    case_run_id=case_run_id,
                    badcase_id=badcase_id,
                    eval_case_id=eval_case_id,
                    annotator=annotator,
                    limit=limit,
                )
            )
        }

    return router


def _actor(request: Request) -> str:
    """Use the authenticated principal for immutable review audit fields."""

    user = getattr(request.state, "user", None)
    if not isinstance(user, dict):
        raise HTTPException(status_code=401, detail="authenticated reviewer is required")
    actor = str(user.get("id") or user.get("username") or "").strip()
    if not actor:
        raise HTTPException(status_code=401, detail="authenticated reviewer is required")
    return actor


def _require_control_role(request: Request, role: str) -> str:
    """Enforce strategy/evolution RBAC in authenticated deployments.

    Standalone router tests and the explicitly unauthenticated local demo keep
    their existing developer workflow.  Once ``auth_required`` is enabled,
    the server-issued role list is authoritative and caller supplied actor
    fields are never accepted.
    """

    actor = _actor(request)
    if not bool(getattr(request.app.state, "auth_required", False)):
        return actor
    user = getattr(request.state, "user", {})
    roles = {
        str(item).strip()
        for item in (user.get("roles") or [])
        if str(item).strip()
    }
    if role not in roles:
        raise HTTPException(status_code=403, detail=f"需要权限：{role}")
    return actor


def _require_production_reader(request: Request) -> str | None:
    """Protect every evaluation asset in authenticated deployments."""

    if not bool(getattr(request.app.state, "auth_required", False)):
        return None
    actor = _actor(request)
    user = getattr(request.state, "user", {})
    roles = {
        str(item).strip()
        for item in (user.get("roles") or [])
        if str(item).strip()
    }
    if not roles.intersection({EXPERIMENT_ADMIN_ROLE, EXPERIMENT_APPROVER_ROLE}):
        raise HTTPException(
            status_code=403,
            detail="需要评测管理或审批权限",
        )
    return actor


def _require_production_role(request: Request, role: str) -> str | None:
    """Apply a mutation role only in production-authenticated mode."""

    if not bool(getattr(request.app.state, "auth_required", False)):
        return None
    return _require_control_role(request, role)


def _production_actor(request: Request) -> str | None:
    """Never trust an audit actor supplied in a production request body."""

    if not bool(getattr(request.app.state, "auth_required", False)):
        return None
    return _actor(request)


def _service(request: Request) -> EvaluationService:
    registry = getattr(request.app.state, "evaluation_service_registry", None)
    user = getattr(request.state, "user", None)
    if registry is not None and user:
        tenant_id = str(user.get("tenant_id") or "").strip()
        actor_id = str(user.get("id") or "").strip()
        if not tenant_id or not actor_id:
            raise HTTPException(
                status_code=401,
                detail="authenticated tenant and user are required",
            )
        return registry.get(tenant_id, execution_user_id=actor_id)
    service = getattr(request.app.state, "evaluation_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Agent evaluation service is unavailable")
    return service


def _guard(operation):
    try:
        return operation()
    except HTTPException:
        raise
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"evaluation operation failed: {exc}") from exc
