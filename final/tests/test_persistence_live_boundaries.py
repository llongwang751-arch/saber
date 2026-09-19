from types import SimpleNamespace

import pytest

from internal.platform.postgres import PostgresClient
from internal.repo.preference import PGRepo


def test_pool_connection_bootstraps_without_a_checked_out_connection(monkeypatch):
    calls = []
    def connect(self):
        self._pool = object()
        self._conn = None
    monkeypatch.setattr(PostgresClient, "_connect", connect)
    monkeypatch.setattr(PostgresClient, "bootstrap_schema", lambda self: calls.append(True))
    PostgresClient(SimpleNamespace())
    assert calls == [True]


@pytest.mark.parametrize("affected", [-1, 0, None])
def test_preference_cannot_acknowledge_failed_write(affected):
    repo = PGRepo(SimpleNamespace(is_real=lambda: True, exec=lambda *_: affected))
    with pytest.raises(RuntimeError, match="did not commit"):
        repo.save("tenant", "language", "zh")


def test_preference_accepts_confirmed_commit():
    repo = PGRepo(SimpleNamespace(is_real=lambda: True, exec=lambda *_: 1))
    repo.save("tenant", "language", "zh")


def test_snapshot_conflict_cannot_silently_claim_success():
    from internal.repo.snapshot import PGRepo as Snapshots
    repo = Snapshots(SimpleNamespace(is_real=lambda: True, exec=lambda *_: 0))
    with pytest.raises(RuntimeError, match="belongs to another user"):
        repo.save("task", "{}", user_id="other")


def test_llm_usage_observer_gets_only_valid_counts_and_cannot_break_response(monkeypatch):
    from config.config import APIConfig
    from internal.llm.llm import Client, Message
    response = SimpleNamespace(status_code=200, json=lambda: {
        "choices": [{"message": {"content": "answer"}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15,
                  "unrelated_private_field": "not forwarded"}})
    monkeypatch.setattr("internal.llm.llm.requests.post", lambda *a, **k: response)
    records = []
    client = Client(APIConfig(), usage_callback=records.append)
    assert client._call_chat("system", [Message("user", "question")]) == "answer"
    assert records == [{"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}]
    client._usage_callback = lambda _: (_ for _ in ()).throw(RuntimeError("observer failed"))
    assert client._call_chat("system", []) == "answer"
