from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from internal.evaluation.api import create_evaluation_router
from internal.evaluation.service import EvaluationService
from internal.evaluation.store import EvaluationStore


def _case(case_id: str) -> dict:
    return {
        "case_id": case_id,
        "scenario": f"发布完整性检查 {case_id}",
        "turns": [{"role": "user", "content": f"执行 {case_id}"}],
        "expected": {
            "intents": ["target"],
            "required_content": ["正确"],
        },
        "metadata": {
            "synthetic": True,
            "data_policy": "synthetic-only",
            "outputs": {
                "baseline": {
                    "intent": "wrong",
                    "content": "错误",
                    "trace": [
                        {"sequence": 0, "event_type": "intent_predicted"},
                        {"sequence": 1, "event_type": "final_response"},
                    ],
                },
                "candidate": {
                    "intent": "target",
                    "content": "正确",
                    "trace": [
                        {"sequence": 0, "event_type": "intent_predicted"},
                        {"sequence": 1, "event_type": "final_response"},
                    ],
                },
            },
        },
    }


@pytest.fixture()
def service(tmp_path: Path):
    repository = EvaluationStore(
        f"sqlite+pysqlite:///{(tmp_path / 'release-integrity.db').as_posix()}"
    )
    value = EvaluationService(repository)
    try:
        yield value
    finally:
        value.close()


def _version(service: EvaluationService, name: str, case_ids: tuple[str, ...]):
    dataset = service.create_dataset(
        name,
        metadata={"data_policy": "synthetic-only"},
    )
    version = service.import_cases(
        dataset["id"],
        [_case(case_id) for case_id in case_ids],
        metadata={"data_policy": "synthetic-only"},
    )
    return dataset, version


def _pending_run(
    service: EvaluationService,
    version_id: str,
    name: str,
    *,
    release_gate: dict | None = None,
):
    return service.create_run(
        version_id,
        name=name,
        adapter={"type": "replay", "profile": "candidate"},
        release_gate=release_gate,
        metadata={"data_source": "offline_eval", "online_ab": False},
    )


@pytest.mark.parametrize(
    "release_gate",
    [
        {"minimum_pass_rate": -0.01},
        {"minimum_pass_rate": 1.01},
        {"maximum_error_rate": -0.01},
        {"maximum_error_rate": 1.01},
        {"maximum_p95_latency_ms": -1},
        {"minimum_sample_count": -1},
        {"pass_rate": 0.95},
        {"unknown_gate": True},
    ],
)
def test_release_gate_rejects_out_of_range_or_unknown_fields(service, release_gate):
    _, version = _version(service, "typed-release-gate", ("gate-1",))

    with pytest.raises((TypeError, ValueError)):
        _pending_run(
            service,
            version["id"],
            "invalid release gate",
            release_gate=release_gate,
        )


def test_compare_runs_rejects_different_dataset_versions(service):
    _, version_a = _version(service, "compare-version-a", ("shared-case",))
    _, version_b = _version(service, "compare-version-b", ("shared-case",))
    baseline = _pending_run(service, version_a["id"], "baseline")
    candidate = _pending_run(service, version_b["id"], "candidate")

    with pytest.raises(ValueError, match="(?i)dataset|version|数据集|版本"):
        service.compare_runs(baseline["id"], candidate["id"])


def test_compare_runs_requires_two_completed_runs(service):
    _, version = _version(service, "compare-completed-runs", ("case-a",))
    baseline = _pending_run(service, version["id"], "baseline")
    candidate = _pending_run(service, version["id"], "candidate")

    with pytest.raises(ValueError, match="(?i)completed|complete|完成"):
        service.compare_runs(baseline["id"], candidate["id"])


def test_compare_runs_rejects_candidate_with_missing_case_result(service):
    _, version = _version(service, "compare-complete-case-set", ("case-a", "case-b"))
    baseline = _pending_run(service, version["id"], "baseline")
    candidate = _pending_run(service, version["id"], "candidate")

    for case_id in ("case-a", "case-b"):
        service.store.save_case_result(
            baseline["id"],
            case_id,
            status="passed",
            passed=True,
        )
    service.store.save_case_result(
        candidate["id"],
        "case-a",
        status="passed",
        passed=True,
    )
    service.store.update_run(
        baseline["id"],
        status="completed",
        summary={"release_gate_passed": True, "pass_rate": 1.0},
    )
    service.store.update_run(
        candidate["id"],
        status="completed",
        summary={"release_gate_passed": True, "pass_rate": 1.0},
    )

    with pytest.raises(ValueError, match="(?i)case|complete|missing|用例|完整|缺失"):
        service.compare_runs(baseline["id"], candidate["id"])


def test_badcase_cannot_be_closed_by_triage_but_verify_can_resolve(service):
    _, version = _version(service, "badcase-state-machine", ("unsafe-case",))
    baseline = service.execute_run(
        service.create_run(
            version["id"],
            name="baseline",
            adapter={"type": "replay", "profile": "baseline"},
            metadata={"data_source": "offline_eval", "online_ab": False},
        )["id"]
    )
    candidate = service.execute_run(
        service.create_run(
            version["id"],
            name="candidate",
            adapter={"type": "replay", "profile": "candidate"},
            metadata={"data_source": "offline_eval", "online_ab": False},
        )["id"]
    )
    badcase = service.store.list_badcases(run_id=baseline["id"])[0]

    app = FastAPI()
    app.include_router(create_evaluation_router())
    app.state.evaluation_service = service
    with TestClient(app) as client:
        for forbidden_status in ("resolved", "closed"):
            response = client.post(
                f"/api/eval/badcases/{badcase['id']}/triage",
                json={
                    "status": forbidden_status,
                    "owner": "unverified-reviewer",
                    "resolution": {"claim": "fixed without evidence"},
                },
            )
            assert response.status_code in {400, 409, 422}
            assert service.store.get_badcase(badcase["id"])["status"] == "open"

        triaged = client.post(
            f"/api/eval/badcases/{badcase['id']}/triage",
            json={"status": "triaged", "owner": "reviewer-a"},
        )
        assert triaged.status_code == 200
        assert triaged.json()["status"] == "triaged"

        verified = client.post(
            f"/api/eval/badcases/{badcase['id']}/verify",
            json={
                "candidate_run_id": candidate["id"],
                "reviewer": "reviewer-b",
                "note": "同一不可变用例已通过候选策略回归",
            },
        )
        assert verified.status_code == 200, verified.text
        assert verified.json()["badcase"]["status"] == "resolved"
        assert verified.json()["candidate_result"]["status"] == "passed"
        assert verified.json()["annotation"]["annotator"] == "reviewer-b"
