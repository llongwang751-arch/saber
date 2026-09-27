from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_native_run_database(tmp_path: Path, monkeypatch):
    """HTTP tests must never append runs to a developer's persistent ledger."""
    monkeypatch.setenv("AGI_RUN_DB_PATH", str(tmp_path / "native-runs.sqlite3"))
