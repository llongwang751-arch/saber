from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from internal.evaluation.service import EvaluationService
from internal.evaluation.store import (
    DatasetVersionRecord,
    EvalRunRecord,
    EvaluationStore,
    ImmutableDatasetVersionError,
)


@pytest.fixture()
def store(tmp_path: Path):
    repository = EvaluationStore(
        f"sqlite+pysqlite:///{(tmp_path / 'evaluation.db').as_posix()}"
    )
    try:
        yield repository
    finally:
        repository.close()


def _cases():
    return [
        {
            "case_id": "intent-001",
            "scenario": "意图理解",
            "turns": [{"role": "user", "content": "帮我查医保报销"}],
            "expected": {"intents": ["insurance_query"], "required_slots": []},
            "risk_tags": ["medical"],
            "metadata": {"source": "expert"},
        },
        {
            "case_id": "tool-001",
            "scenario": "工具调用",
            "turns": [{"role": "user", "content": "查询深圳医保办理入口"}],
            "expected": {
                "tool_calls": [{"name": "policy_search", "arguments": {"city": "深圳"}}]
            },
            "risk_tags": ["policy"],
            "metadata": {},
        },
    ]


def _dataset_version(store: EvaluationStore):
    dataset = store.create_dataset(
        "medical-agent-regression",
        description="医疗 Agent 回归集",
        metadata={"owner": "quality"},
    )
    version = store.import_dataset_version(
        dataset["id"], _cases(), metadata={"source": "reviewed"}
    )
    return dataset, version


def test_schema_and_sqlite_foreign_keys_are_enabled(store: EvaluationStore):
    expected_tables = {
        "datasets",
        "dataset_versions",
        "eval_cases",
        "eval_runs",
        "case_runs",
        "badcases",
        "human_annotations",
    }
    assert expected_tables <= set(inspect(store.engine).get_table_names())

    with store.engine.connect() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1

    with pytest.raises(IntegrityError):
        with store._transaction() as session:
            session.add(
                EvalRunRecord(
                    id="missing-parent-run",
                    dataset_version_id="missing-version",
                    name="",
                    status="pending",
                    config={},
                    summary={},
                    metadata_json={},
                )
            )


def test_in_memory_sqlite_schema_is_shared_across_sessions():
    repository = EvaluationStore("sqlite:///:memory:")
    try:
        created = repository.create_dataset("in-memory")
        assert repository.get_dataset(created["id"])["name"] == "in-memory"
    finally:
        repository.close()


def test_database_url_can_be_overridden_by_environment(tmp_path: Path, monkeypatch):
    database_file = tmp_path / "from-env.db"
    database_url = f"sqlite+pysqlite:///{database_file.as_posix()}"
    monkeypatch.setenv("AGI_EVAL_DATABASE_URL", database_url)

    repository = EvaluationStore()
    try:
        assert repository.database_url == database_url
        assert database_file.exists()
    finally:
        repository.close()


def test_dataset_versions_are_idempotent_ordered_and_immutable(store: EvaluationStore):
    dataset, version = _dataset_version(store)

    assert version["created"] is True
    assert version["version"] == 1
    assert version["case_count"] == 2
    assert len(version["checksum"]) == 64

    duplicate = store.import_dataset_version(dataset["id"], _cases())
    assert duplicate["created"] is False
    assert duplicate["id"] == version["id"]
    assert len(store.list_dataset_versions(dataset["id"])) == 1

    cases = store.read_version_cases(version["id"])
    assert [case["case_id"] for case in cases] == ["intent-001", "tool-001"]
    assert cases[0]["payload"]["expected"]["intents"] == ["insurance_query"]
    assert cases[1]["position"] == 1

    changed_cases = _cases() + [
        {
            "case_id": "fallback-001",
            "scenario": "异常兜底",
            "turns": [],
            "expected": {"require_fallback": True},
        }
    ]
    second = store.import_dataset_version(dataset["id"], changed_cases)
    assert second["created"] is True
    assert second["version"] == 2

    with pytest.raises(ImmutableDatasetVersionError):
        with store._transaction() as session:
            record = session.get(DatasetVersionRecord, version["id"])
            record.checksum = "mutated"


def test_dataset_import_rejects_duplicate_case_ids(store: EvaluationStore):
    dataset = store.create_dataset("duplicate-case-check")

    with pytest.raises(ValueError, match="case_id values must be unique"):
        store.import_dataset_version(
            dataset["id"],
            [{"case_id": "same"}, {"case_id": "same"}],
        )


def test_run_results_and_badcases_are_persisted_as_json(store: EvaluationStore):
    _, version = _dataset_version(store)
    run = store.create_run(
        version["id"],
        name="nightly",
        status="running",
        config={"model": "deepseek-v4", "temperature": 0},
    )

    passed = store.save_case_result(
        run["id"],
        "intent-001",
        status="passed",
        output={"intent": "insurance_query", "content": "请提供所在城市"},
        metrics={"passed": True, "intent_accuracy": 1.0},
        trace=[{"event": "intent_detected", "latency_ms": 12}],
    )
    assert passed["badcase"] is None

    failed = store.save_case_result(
        run["id"],
        "tool-001",
        status="failed",
        output={"tool_calls": [], "content": "不知道"},
        metrics={"passed": False, "tool_precision": 0.0},
        trace=[{"event": "fallback", "reason": "tool timeout"}],
        error={"type": "missing_tool_call"},
        badcase_category="tool_call",
        badcase_severity="high",
        badcase_details={"expected_tool": "policy_search"},
    )
    assert failed["badcase"]["category"] == "tool_call"
    assert failed["badcase"]["severity"] == "high"

    # A terminal per-case observation is release evidence, not an upsert slot.
    with pytest.raises(ValueError, match="immutable"):
        store.save_case_result(
            run["id"],
            "tool-001",
            status="failed",
            output={"tool_calls": []},
            metrics={"passed": False},
            trace=[],
        )

    results = store.list_results(run["id"])
    assert [result["case_id"] for result in results] == ["intent-001", "tool-001"]
    assert results[1]["output"] == {"tool_calls": [], "content": "不知道"}
    assert results[1]["metrics"] == {"passed": False, "tool_precision": 0.0}
    assert len(store.list_badcases(run_id=run["id"])) == 1

    completed = store.update_run(
        run["id"],
        status="completed",
        summary={"total": 2, "passed": 1, "pass_rate": 0.5},
    )
    assert completed["completed_at"] is not None
    assert completed["summary"]["pass_rate"] == 0.5
    assert store.list_runs(status="completed")[0]["id"] == run["id"]


def test_badcase_triage_and_human_annotations(store: EvaluationStore):
    _, version = _dataset_version(store)
    run = store.create_run(version["id"])
    result = store.save_case_result(
        run["id"],
        "intent-001",
        status="error",
        output={},
        metrics={"passed": False},
        trace=[{"event": "exception"}],
    )
    badcase = result["badcase"]

    with pytest.raises(ValueError, match="(?i)verification|resolve|close"):
        store.triage_badcase(badcase["id"], status="resolved")

    triaged = store.triage_badcase(
        badcase["id"],
        category="intent_understanding",
        severity="critical",
        status="investigating",
        owner="quality-owner",
        resolution={"action": "add adversarial cases", "verified": False},
    )
    assert triaged["owner"] == "quality-owner"
    assert triaged["resolved_at"] is None
    assert triaged["resolution"]["verified"] is False

    candidate = store.create_run(version["id"], status="running")
    store.save_case_result(
        candidate["id"],
        "intent-001",
        status="passed",
        passed=True,
        output={"intent": "insurance_query"},
        metrics={"passed": True},
        trace=[],
    )
    store.update_run(candidate["id"], status="completed", summary={"passed": 1})
    verified = EvaluationService(store).verify_badcase(
        badcase["id"],
        candidate_run_id=candidate["id"],
        reviewer="quality-owner",
        note="同一不可变数据集用例已通过候选运行",
    )
    assert verified["badcase"]["status"] == "resolved"
    assert verified["badcase"]["resolved_at"] is not None
    assert verified["badcase"]["resolution"]["passed"] is True

    annotation = store.create_annotation(
        case_run_id=result["id"],
        badcase_id=badcase["id"],
        annotator="reviewer-01",
        annotation={"label": "模型漏识别医保意图", "confidence": 0.95},
    )
    assert annotation["annotation"]["confidence"] == 0.95
    assert store.list_annotations(badcase_id=badcase["id"])[0]["id"] == annotation["id"]
    assert len(store.list_annotations(badcase_id=badcase["id"])) == 2
    assert store.list_badcases(status="resolved", owner="quality-owner")[0]["id"] == badcase["id"]

    with pytest.raises(ValueError, match="must target"):
        store.create_annotation(annotator="reviewer", annotation={"label": "invalid"})


def test_case_result_must_belong_to_run_dataset_version(store: EvaluationStore):
    first_dataset, first_version = _dataset_version(store)
    second_dataset = store.create_dataset("another-dataset")
    second_version = store.import_dataset_version(
        second_dataset["id"], [{"case_id": "foreign-case", "scenario": "other"}]
    )
    foreign_case = store.read_version_cases(second_version["id"])[0]
    run = store.create_run(first_version["id"])

    with pytest.raises(LookupError, match="not found in run dataset version"):
        store.save_case_result(
            run["id"],
            foreign_case["id"],
            status="passed",
            output={},
            metrics={"passed": True},
            trace=[],
        )

    assert first_dataset["id"] != second_dataset["id"]
