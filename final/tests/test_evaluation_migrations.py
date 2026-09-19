from __future__ import annotations

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from internal.evaluation.store import Base, EvaluationStore
from internal.application import models as _application_models  # noqa: F401


FINAL_ROOT = Path(__file__).resolve().parents[1]
BUSINESS_TABLES = {
    "datasets",
    "dataset_versions",
    "eval_cases",
    "eval_runs",
    "case_runs",
    "badcases",
    "human_annotations",
    "users",
    "installed_skills",
    "farm_production_records",
    "farm_reports",
    "agent_preferences",
    "agent_chat_history",
    "agent_task_snapshots",
    "agent_long_term_memory",
    "agent_documents",
    "agent_document_versions",
    "memory_outbox",
    "agent_rag_chunks",
    "agent_traces",
    "rag_projection_outbox",
    "strategy_versions",
    "promotion_proposals",
    "strategy_pointers",
    "strategy_audit_events",
    "online_strategy_deployments",
    "online_experiments",
    "experiment_assignments",
    "experiment_exposures",
    "experiment_outcomes",
    "experiment_monitor_snapshots",
    "experiment_audit_events",
    "experiment_safety_outbox",
    "evolution_suggestions",
    "evolution_audit_events",
}


def _database_url(database_file: Path) -> str:
    return f"sqlite+pysqlite:///{database_file.as_posix()}"


def _alembic_config(database_file: Path) -> Config:
    config = Config(str(FINAL_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", _database_url(database_file))
    return config


@pytest.fixture(autouse=True)
def _isolate_database_environment(monkeypatch):
    monkeypatch.delenv("AGI_EVAL_DATABASE_URL", raising=False)


def test_eighteen_revision_chain_creates_tables_incrementally(tmp_path: Path):
    database_file = tmp_path / "stepwise.db"
    config = _alembic_config(database_file)
    script = ScriptDirectory.from_config(config)

    revisions = [revision.revision for revision in script.walk_revisions()]
    assert revisions == [
        "0018_conversation_history",
        "0017_memory_supersession_provenance",
        "0016_verified_runtime_identity",
        "0015_experiment_lifecycle_integrity",
        "0014_experiment_audience_identity",
        "0013_controlled_evolution",
        "0012_online_experiments",
        "0011_offline_strategy_review",
        "0010_rag_projection_outbox",
        "0009_agent_traces",
        "0008_memory_consistency",
        "0007_local_agent_state",
        "0006_smart_farm",
        "0005_application_identity_skills",
        "0004_evaluation_indexes",
        "0003_evaluation_badcases",
        "0002_evaluation_runs",
        "0001_evaluation_datasets",
    ]

    command.upgrade(config, "0001_evaluation_datasets")
    engine = create_engine(_database_url(database_file))
    try:
        assert {"datasets", "dataset_versions", "eval_cases"} <= set(
            inspect(engine).get_table_names()
        )
        assert "eval_runs" not in inspect(engine).get_table_names()

        command.upgrade(config, "0002_evaluation_runs")
        assert {"eval_runs", "case_runs"} <= set(inspect(engine).get_table_names())
        assert "badcases" not in inspect(engine).get_table_names()

        command.upgrade(config, "0003_evaluation_badcases")
        assert {"badcases", "human_annotations"} <= set(
            inspect(engine).get_table_names()
        )

        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0018_conversation_history"
            )
    finally:
        engine.dispose()


def test_head_schema_matches_store_and_supports_crud(tmp_path: Path):
    database_file = tmp_path / "compatible.db"
    config = _alembic_config(database_file)
    command.upgrade(config, "head")

    database_url = _database_url(database_file)
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert BUSINESS_TABLES <= set(inspector.get_table_names())

        for table in Base.metadata.sorted_tables:
            migrated_columns = {column["name"] for column in inspector.get_columns(table.name)}
            assert migrated_columns == set(table.columns.keys()), table.name
            migrated_indexes = {index["name"] for index in inspector.get_indexes(table.name)}
            model_indexes = {index.name for index in table.indexes}
            assert model_indexes <= migrated_indexes, table.name

        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("dataset_versions")
        } >= {
            "uq_dataset_versions_dataset_checksum",
            "uq_dataset_versions_dataset_version",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints("human_annotations")
        } == {"ck_human_annotations_has_target"}
    finally:
        engine.dispose()

    store = EvaluationStore(database_url, create_schema=False)
    try:
        dataset = store.create_dataset("migration-compatible")
        version = store.import_dataset_version(
            dataset["id"],
            [
                {
                    "case_id": "intent-001",
                    "scenario": "intent",
                    "turns": [{"role": "user", "content": "我要查医保"}],
                    "expected": {"intents": ["insurance_query"]},
                }
            ],
        )
        run = store.create_run(version["id"], status="running")
        result = store.save_case_result(
            run["id"],
            "intent-001",
            status="failed",
            output={"intent": "unknown"},
            metrics={"passed": False, "overall_score": 0.0},
            trace=[{"event": "intent_detected"}],
        )
        annotation = store.create_annotation(
            case_run_id=result["id"],
            badcase_id=result["badcase"]["id"],
            annotator="migration-test",
            annotation={"label": "intent mismatch"},
        )

        assert store.list_case_results(run["id"])[0]["output"]["intent"] == "unknown"
        assert store.list_badcases(run_id=run["id"])[0]["status"] == "open"
        assert store.list_annotations(case_run_id=result["id"])[0]["id"] == annotation["id"]
    finally:
        store.close()


def test_upgrade_head_then_downgrade_base_is_reversible(tmp_path: Path):
    database_file = tmp_path / "reversible.db"
    config = _alembic_config(database_file)

    command.upgrade(config, "head")
    engine = create_engine(_database_url(database_file))
    try:
        assert BUSINESS_TABLES <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    command.downgrade(config, "base")
    engine = create_engine(_database_url(database_file))
    try:
        tables_after_downgrade = set(inspect(engine).get_table_names())
        assert not (BUSINESS_TABLES & tables_after_downgrade)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT COUNT(*) FROM alembic_version")) == 0
    finally:
        engine.dispose()


def test_migrations_honor_database_url_environment_override(
    tmp_path: Path, monkeypatch
):
    configured_file = tmp_path / "configured.db"
    override_file = tmp_path / "override.db"
    config = _alembic_config(configured_file)
    monkeypatch.setenv("AGI_EVAL_DATABASE_URL", _database_url(override_file))

    command.upgrade(config, "head")

    assert override_file.exists()
    assert not configured_file.exists()
    engine = create_engine(_database_url(override_file))
    try:
        assert BUSINESS_TABLES <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_store_auto_upgrades_fresh_database_to_head(tmp_path: Path):
    database_file = tmp_path / "auto-upgrade.db"
    store = EvaluationStore(_database_url(database_file))
    try:
        with store.engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0018_conversation_history"
            )
    finally:
        store.close()


def test_audience_upgrade_never_grandfathers_legacy_accounts(tmp_path: Path):
    database_file = tmp_path / "legacy-audience.db"
    config = _alembic_config(database_file)
    command.upgrade(config, "0013_controlled_evolution")
    engine = create_engine(_database_url(database_file))
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, username, password_hash, tenant_id, roles, created_at) "
                    "VALUES (:id, :username, :password_hash, :tenant_id, :roles, "
                    "CURRENT_TIMESTAMP)"
                ),
                {
                    "id": "legacy-user",
                    "username": "legacy_user",
                    "password_hash": "unused",
                    "tenant_id": "legacy-tenant",
                    "roles": '["participant"]',
                },
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(_database_url(database_file))
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT identity_provenance, experiment_eligible "
                    "FROM users WHERE id = 'legacy-user'"
                )
            ).one()
        assert row.identity_provenance == "legacy_unverified"
        assert bool(row.experiment_eligible) is False
    finally:
        engine.dispose()


def test_runtime_identity_upgrade_never_invents_legacy_component_evidence(
    tmp_path: Path,
):
    database_file = tmp_path / "legacy-runtime-identity.db"
    config = _alembic_config(database_file)
    command.upgrade(config, "0015_experiment_lifecycle_integrity")
    engine = create_engine(_database_url(database_file))
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO online_strategy_deployments "
                    "(id, tenant_id, source_proposal_id, source_strategy_version_id, "
                    "source_manifest_checksum, compiled_overrides, compiler_version, "
                    "compiled_checksum, record_checksum, created_by, created_at) "
                    "VALUES ('deployment-legacy', 'tenant-legacy', 'proposal-legacy', "
                    "'strategy-legacy', :checksum, :overrides, 'rag-runtime-v1', "
                    ":checksum, :checksum, 'legacy-admin', CURRENT_TIMESTAMP)"
                ),
                {"checksum": "a" * 64, "overrides": '{"rag":{"top_k":3}}'},
            )
            connection.execute(
                text(
                    "INSERT INTO online_experiments "
                    "(id, tenant_id, name, hypothesis, surface, status, "
                    "candidate_deployment_id, control_overrides, "
                    "baseline_runtime_overrides, runtime_environment_fingerprint, "
                    "candidate_allocation_bps, initial_enrollment_bps, enrollment_bps, "
                    "primary_metric, aggregation_rule, baseline_rate, "
                    "minimum_detectable_effect, alpha, power, required_sample_per_arm, "
                    "min_duration_hours, max_duration_hours, attribution_window_hours, "
                    "traffic_provenance, hmac_key_id, preregistration_checksum, "
                    "generation, created_by, reviewed_by, review_note, started_by, "
                    "paused_by, pause_reason, paused_from, completed_by, created_at, "
                    "updated_at, audience_policy_version) "
                    "VALUES ('experiment-legacy', 'tenant-legacy', '旧实验', '', "
                    "'rag_chat', 'draft', 'deployment-legacy', '{}', :baseline, "
                    ":fingerprint, 5000, 1000, 1000, 'positive_feedback', "
                    "'first_feedback_per_exposed_user_v1', 0.1, 0.05, 0.05, 0.8, "
                    "100, 1, 24, 168, 'production_authenticated', 'legacy-key', '', "
                    "0, 'legacy-admin', '', '', '', '', '', '', '', CURRENT_TIMESTAMP, "
                    "CURRENT_TIMESTAMP, 'production_authenticated_v1')"
                ),
                {
                    "baseline": '{"rag":{"top_k":3,"no_answer_threshold":0.3}}',
                    "fingerprint": "b" * 64,
                },
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(_database_url(database_file))
    try:
        inspector = inspect(engine)
        identity_column = next(
            column
            for column in inspector.get_columns("online_experiments")
            if column["name"] == "runtime_identity_evidence"
        )
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT runtime_identity_evidence, preregistration_checksum "
                    "FROM online_experiments WHERE id='experiment-legacy'"
                )
            ).one()
        assert identity_column["nullable"] is False
        assert json.loads(row.runtime_identity_evidence) == {}
        assert row.preregistration_checksum == ""
    finally:
        engine.dispose()


def test_store_adopts_complete_pre_alembic_schema(tmp_path: Path):
    database_file = tmp_path / "legacy-create-all.db"
    engine = create_engine(_database_url(database_file))
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()

    store = EvaluationStore(_database_url(database_file))
    try:
        with store.engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0018_conversation_history"
            )
    finally:
        store.close()
