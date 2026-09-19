from __future__ import annotations

from pathlib import Path

import pytest

from internal.evaluation.service import EvaluationServiceRegistry


def test_registry_rejects_shared_template_without_tenant_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(
        "AGI_EVAL_TENANT_DATABASE_URL_TEMPLATE",
        "postgresql+psycopg2://user:pass@db/all-tenants",
    )
    with pytest.raises(ValueError, match="tenant_digest"):
        EvaluationServiceRegistry(root=tmp_path / "registry")


def test_only_non_sqlite_tenant_template_is_production_shared_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(
        "AGI_EVAL_TENANT_DATABASE_URL_TEMPLATE",
        "sqlite+pysqlite:///runtime/eval-{tenant_digest}.db",
    )
    local = EvaluationServiceRegistry(root=tmp_path / "local")
    try:
        assert local.storage_backend == "sqlite"
        assert local.production_shared_ready is False
    finally:
        local.close()

    monkeypatch.setenv(
        "AGI_EVAL_TENANT_DATABASE_URL_TEMPLATE",
        "postgresql+psycopg2://user:pass@db/eval_{tenant_digest}",
    )
    shared = EvaluationServiceRegistry(root=tmp_path / "shared")
    try:
        assert shared.storage_backend == "postgresql"
        assert shared.production_shared_ready is True
    finally:
        shared.close()


def test_registry_rejects_unresolved_template_braces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(
        "AGI_EVAL_TENANT_DATABASE_URL_TEMPLATE",
        "postgresql://db/eval_{tenant_digest}_{unknown}",
    )
    with pytest.raises(ValueError, match="invalid"):
        EvaluationServiceRegistry(root=tmp_path / "registry")
