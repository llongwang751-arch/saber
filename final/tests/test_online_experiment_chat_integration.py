from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from config.config import APIConfig
from internal.agent.agent import Response
from internal.evaluation.strategy import manifest_sha256
from internal.experimentation.service import ExperimentService
from internal.experimentation.store import ExperimentStore, IdempotencyConflictError
from internal.handler.handler import (
    ChatRequest,
    _begin_online_rag_exposure,
    setup_routes,
)


HMAC_SECRET = b"chat-integration-secret-long-enough-production"
FORBIDDEN_PUBLIC_KEYS = {
    "arm",
    "assignment_id",
    "deployment_id",
    "compiled_checksum",
    "source_manifest_checksum",
    "runtime_strategy_checksum",
    "runtime_overrides",
    "runtime_overrides_applied",
    "candidate_allocation_bps",
    "allocation_bps",
    "enrollment_bucket",
    "variant_bucket",
    "subject_digest",
    "top_k",
    "no_answer_threshold",
    "audience_policy_version",
    "audience_provenance",
    "audience_eligible",
    "audience_account_created_at",
    "audience_attestation",
}


def _all_keys(value):
    if isinstance(value, dict):
        result = set(value)
        for item in value.values():
            result.update(_all_keys(item))
        return result
    if isinstance(value, list):
        result = set()
        for item in value:
            result.update(_all_keys(item))
        return result
    return set()


def _sse_payloads(body: str) -> list[tuple[str, dict]]:
    values = []
    for block in body.split("\n\n"):
        event = ""
        data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
            elif line.startswith("data: ") and line != "data: [DONE]":
                data = json.loads(line.removeprefix("data: "))
        if event and isinstance(data, dict):
            values.append((event, data))
    return values


class _ChatAgent:
    def __init__(self):
        self.rag = SimpleNamespace(
            loaded=True,
            user_id="",
            _hybrid=None,
            _check_existing_chunks=lambda: None,
        )
        self.contexts = []
        self.error = None
        self.interrupted = False
        self.inf = None

    def _response(self, message, execution_context):
        self.contexts.append(execution_context)
        exposure_id = (
            execution_context.experiment_exposure_id if execution_context else ""
        )
        trace = {"decision": "answer", "retrieval": {"top_k": 9}}
        if execution_context:
            trace.update(
                {
                    "experiment_exposure_id": exposure_id,
                    "arm": "candidate",
                    "assignment_id": "assignment-private",
                    "deployment_id": "deployment-private",
                    "runtime_strategy_checksum": "checksum-private",
                    "runtime_overrides_applied": {
                        "rag": {"top_k": 6, "no_answer_threshold": 0.4}
                    },
                }
            )
        return Response(
            query=message,
            answer="测试回答",
            mode="rag",
            error=self.error,
            interrupted=self.interrupted,
            rag_trace=trace,
            experiment_exposure_id=exposure_id,
            runtime_strategy_checksum="checksum-private" if exposure_id else "",
            trace_id=execution_context.trace_id if execution_context else "",
        )

    def process_with_options(self, message, _options, execution_context=None):
        return self._response(message, execution_context)

    def process_stream(self, message, _options, on_event, execution_context=None):
        response = self._response(message, execution_context)
        exposure_id = response.experiment_exposure_id
        on_event(
            {
                "type": "rag_trace",
                "data": {
                    **response.rag_trace,
                    "runtime_overrides": {"rag": {"top_k": 6}},
                },
            }
        )
        on_event({"type": "token", "data": {"content": "测试"}})
        on_event(
            {
                "type": "done",
                "data": {
                    "answer": response.answer,
                    "success": not bool(response.error),
                    "error": response.error,
                    "interrupted": response.interrupted,
                    "experiment_exposure_id": exposure_id,
                    "arm": "candidate",
                    "assignment_id": "assignment-private",
                    "deployment_id": "deployment-private",
                    "runtime_strategy_checksum": "checksum-private",
                    "runtime_overrides_applied": {
                        "rag": {"top_k": 6, "no_answer_threshold": 0.4}
                    },
                },
            }
        )
        return response

    def cancel(self):
        return None


class _Infra:
    def __init__(self):
        self.ready = SimpleNamespace(
            milvus="connected",
            postgresql="connected",
            elasticsearch="connected",
            kafka="disconnected",
        )
        self.repo = SimpleNamespace()


def _approved_evidence(tenant_id: str, proposal_id: str):
    manifest = {
        "runtime_overrides": {
            "rag": {"top_k": 6, "no_answer_threshold": 0.4}
        }
    }
    _canonical, checksum = manifest_sha256(manifest)
    return {
        "tenant_id": tenant_id,
        "proposal": {
            "id": proposal_id,
            "status": "approved",
            "candidate_strategy_version_id": f"strategy-{proposal_id}",
        },
        "strategy": {
            "id": f"strategy-{proposal_id}",
            "source": "offline_eval",
            "manifest": manifest,
            "manifest_checksum": checksum,
        },
    }


def _active_experiment(service: ExperimentService, tenant_id: str) -> dict:
    deployment = service.create_deployment(
        tenant_id,
        "approved-chat",
        actor="chat-admin",
        idempotency_key="deploy-chat",
    )
    experiment = service.create_experiment(
        tenant_id,
        name="chat integration experiment",
        candidate_deployment_id=deployment["id"],
        baseline_rate=0.1,
        minimum_detectable_effect=0.8,
        enrollment_bps=10000,
        candidate_allocation_bps=5000,
        min_duration_hours=1,
        max_duration_hours=24,
        creator="chat-creator",
        idempotency_key="create-chat",
    )
    submitted = service.submit_experiment(
        tenant_id,
        experiment["id"],
        actor="chat-admin",
        expected_generation=experiment["generation"],
        idempotency_key="submit-chat",
    )
    approved = service.review_experiment(
        tenant_id,
        experiment["id"],
        decision="approve",
        actor="chat-approver",
        expected_generation=submitted["generation"],
        idempotency_key="approve-chat",
    )
    return service.start_experiment(
        tenant_id,
        experiment["id"],
        actor="chat-admin",
        expected_generation=approved["generation"],
        idempotency_key="start-chat",
        target_status="running",
    )


@pytest.fixture()
def chat_app(tmp_path: Path, monkeypatch, runtime_identity_factory):
    monkeypatch.setenv(
        "AGI_EVAL_DATABASE_URL",
        f"sqlite+pysqlite:///{(tmp_path / 'application.db').as_posix()}",
    )
    monkeypatch.setenv("AGI_DEFAULT_TENANT_ID", "tenant-chat")
    agent = _ChatAgent()
    infra = _Infra()
    agent.inf = infra
    app = setup_routes(agent, infra, APIConfig())
    tenant_id = app.state.development_user["tenant_id"]
    user_id = app.state.development_user["id"]
    service = ExperimentService(
        ExperimentStore(
            f"sqlite+pysqlite:///{(tmp_path / 'online-chat.db').as_posix()}"
        ),
        hmac_secret=HMAC_SECRET,
        traffic_provenance="production_authenticated",
        strategy_evidence_resolver=_approved_evidence,
        production_evidence_ready=True,
        baseline_runtime_overrides={
            "rag": {"top_k": 3, "no_answer_threshold": 0.3}
        },
        runtime_identity=runtime_identity_factory("chat-primary"),
    )
    experiment = _active_experiment(service, tenant_id)
    updated_user = app.state.application_store.set_user_identity(
        user_id,
        tenant_id=tenant_id,
        roles=["participant"],
        identity_provenance="operator_provisioned",
        experiment_eligible=True,
    )
    # The development middleware closes over the original mapping.
    app.state.development_user.update(updated_user)
    app.state.experiment_service = service
    with TestClient(app) as client:
        yield app, client, agent, service, experiment, tenant_id, user_id
    service.close()


def test_duplicate_request_id_never_executes_agent_twice(chat_app):
    _app, client, agent, service, experiment, tenant_id, _user_id = chat_app
    headers = {"X-Request-ID": "turn-sync-sse-retry"}

    sync = client.post(
        "/api/chat", headers=headers, json={"message": "知识库问题", "use_rag": True}
    )
    assert sync.status_code == 200, sync.text
    sync_body = sync.json()
    assert sync_body["experiment"]["exposure_id"]
    assert sync_body["experiment"]["feedback_eligible"] is True
    assert not (_all_keys(sync_body) & FORBIDDEN_PUBLIC_KEYS)

    sync_retry = client.post(
        "/api/chat", headers=headers, json={"message": "知识库问题", "use_rag": True}
    )
    assert sync_retry.status_code == 409

    with client.stream(
        "POST",
        "/api/chat/stream",
        headers=headers,
        json={"message": "知识库问题", "use_rag": True},
    ) as stream:
        assert stream.status_code == 409

    changed_payload = client.post(
        "/api/chat",
        headers=headers,
        json={"message": "另一个知识库问题", "use_rag": True},
    )
    assert changed_payload.status_code == 409

    evidence = service.store.evidence(tenant_id, experiment["id"])
    assert len(evidence["exposures"]) == 1
    assert evidence["exposures"][0]["status"] == "completed"
    assert [ctx.experiment_exposure_id for ctx in agent.contexts] == [
        sync_body["experiment"]["exposure_id"],
    ]


def test_sse_finishes_exposure_before_releasing_feedback_handle(chat_app):
    _app, client, _agent, service, _experiment, _tenant_id, _user_id = chat_app
    finish_called = threading.Event()
    original_finish = service.finish_exposure

    def recording_finish(*args, **kwargs):
        result = original_finish(*args, **kwargs)
        finish_called.set()
        return result

    service.finish_exposure = recording_finish
    with client.stream(
        "POST",
        "/api/chat/stream",
        headers={"X-Request-ID": "turn-sse-first-attempt"},
        json={"message": "知识库问题", "use_rag": True},
    ) as stream:
        assert stream.status_code == 200
        lines = []
        for line in stream.iter_lines():
            lines.append(line)
            if line == "event: done":
                assert finish_called.is_set(), "SSE done became visible before finish_exposure"
    events = []
    current = ""
    for line in lines:
        if line.startswith("event: "):
            current = line.removeprefix("event: ")
        elif line.startswith("data: ") and line != "data: [DONE]":
            events.append((current, json.loads(line.removeprefix("data: "))))
    done = next(data for event, data in events if event == "done")
    assert done["experiment"]["exposure_id"]
    assert done["experiment"]["feedback_eligible"] is True
    assert not (_all_keys([data for _event, data in events]) & FORBIDDEN_PUBLIC_KEYS)


@pytest.mark.parametrize(
    ("error", "interrupted", "expected_status"),
    [("model failed", False, "error"), (None, True, "cancelled")],
)
def test_error_or_interrupted_chat_is_not_feedback_eligible(
    chat_app, error, interrupted, expected_status
):
    _app, client, agent, service, experiment, tenant_id, _user_id = chat_app
    agent.error = error
    agent.interrupted = interrupted

    response = client.post(
        "/api/chat",
        headers={"X-Request-ID": f"turn-{expected_status}"},
        json={"message": "知识库问题", "use_rag": True},
    )

    assert response.status_code == 200
    assert response.json()["experiment"]["feedback_eligible"] is False
    evidence = service.store.evidence(tenant_id, experiment["id"])
    assert evidence["exposures"][0]["status"] == expected_status


def test_exposure_conflict_returns_409_without_running_default_chat(chat_app, monkeypatch):
    _app, client, agent, service, experiment, tenant_id, _user_id = chat_app

    def conflict(_allocation):
        raise IdempotencyConflictError("turn belongs to another subject")

    monkeypatch.setattr(service, "begin_exposure", conflict)
    response = client.post(
        "/api/chat",
        headers={"X-Request-ID": "attacker-reused-turn"},
        json={"message": "知识库问题", "use_rag": True},
    )

    assert response.status_code == 409
    assert agent.contexts == []
    assert service.store.evidence(tenant_id, experiment["id"])["exposures"] == []


def test_missing_tenant_identity_fails_closed_without_assignment():
    class _MustNotRoute:
        def resolve_assignment(self, *_args, **_kwargs):
            raise AssertionError("identity without tenant must not enter assignment")

    app = SimpleNamespace(state=SimpleNamespace(experiment_service=_MustNotRoute()))
    request = Request({"type": "http", "app": app})
    request.state.user = {"id": "user-without-tenant"}
    request.state.request_id = "missing-tenant-turn"
    agent = SimpleNamespace(rag=SimpleNamespace(loaded=True))

    exposure, context = _begin_online_rag_exposure(
        request,
        ChatRequest(message="知识库问题", use_rag=True),
        agent,
        "missing-tenant-trace",
    )

    assert exposure is None
    assert context is None


def test_persisted_trace_endpoint_redacts_experiment_but_keeps_normal_rag_detail(
    chat_app,
):
    _app, client, agent, _service, _experiment, _tenant_id, user_id = chat_app
    repo = agent.inf.repo.ragtrace
    repo.save(
        "trace-experiment",
        user_id,
        "rag",
        "问题",
        "completed",
        {
            "rag": {"top_k": 6, "no_answer_threshold": 0.4},
            "experiment_exposure_id": "opaque-exposure",
            "arm": "candidate",
            "assignment_id": "private-assignment",
            "deployment_id": "private-deployment",
            "runtime_strategy_checksum": "private-checksum",
            "runtime_overrides_applied": {"rag": {"top_k": 6}},
        },
    )
    repo.save(
        "trace-normal",
        user_id,
        "rag",
        "问题",
        "completed",
        {"rag": {"top_k": 5}, "decision": "answer"},
    )

    experiment_trace = client.get("/api/traces/trace-experiment")
    normal_trace = client.get("/api/traces/trace-normal")

    assert experiment_trace.status_code == 200
    assert not (_all_keys(experiment_trace.json()) & FORBIDDEN_PUBLIC_KEYS)
    assert normal_trace.status_code == 200
    assert normal_trace.json()["trace"]["rag"]["top_k"] == 5
