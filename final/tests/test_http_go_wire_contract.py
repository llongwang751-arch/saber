from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from fastapi.testclient import TestClient

from config.config import APIConfig
from internal.agent.agent import Response
from internal.handler.handler import setup_routes


class _Agent:
    pass


class _Infra:
    pass


class _Memory:
    def __init__(self):
        self.items = [
            SimpleNamespace(
                id=7,
                content="待复核记忆",
                importance=0.8,
                category="general",
                tags=[],
                slot_hint="",
                score=0.0,
                status="active",
                superseded_by=None,
                quarantine_reason="",
                version=1,
                content_hash="",
            )
        ]
        self.calls = []

    def set_status_committed(self, ids, status, *, reason="", superseded_by=None):
        self.calls.append((list(ids), status, reason, superseded_by))
        changed = []
        for item in self.items:
            if item.id in ids:
                item.status = status
                item.quarantine_reason = reason
                changed.append(item.id)
        return changed


class _MemoryAgent:
    def __init__(self):
        self.ltm = _Memory()
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _StreamAgent:
    def process_stream(self, message, _opts, on_event):
        response = Response(
            query=message,
            answer="真流",
            mode="chat",
            extracted_info="已记住：语言 = 中文",
        )
        on_event(
            {
                "type": "memory",
                "data": {"extracted_info": response.extracted_info},
            }
        )
        on_event({"type": "route", "data": {"mode": "chat"}})
        on_event(
            {
                "type": "step",
                "data": {"type": "Thought", "content": "准备回答"},
            }
        )
        on_event({"type": "token", "data": {"content": "真流"}})
        on_event({"type": "done", "data": response})
        return response


@pytest.fixture()
def wire_client(tmp_path, monkeypatch):
    database = tmp_path / "http-wire.db"
    monkeypatch.setenv(
        "AGI_EVAL_DATABASE_URL", f"sqlite+pysqlite:///{database.as_posix()}"
    )
    app = setup_routes(_Agent(), _Infra(), APIConfig(), auth_required=True)
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def memory_client(tmp_path, monkeypatch):
    database = tmp_path / "http-memory-wire.db"
    monkeypatch.setenv(
        "AGI_EVAL_DATABASE_URL", f"sqlite+pysqlite:///{database.as_posix()}"
    )
    agent = _MemoryAgent()
    app = setup_routes(agent, _Infra(), APIConfig(), auth_required=False)
    with TestClient(app) as client:
        yield client, agent


@pytest.fixture()
def stream_client(tmp_path, monkeypatch):
    database = tmp_path / "http-stream-wire.db"
    monkeypatch.setenv(
        "AGI_EVAL_DATABASE_URL", f"sqlite+pysqlite:///{database.as_posix()}"
    )
    app = setup_routes(_StreamAgent(), _Infra(), APIConfig(), auth_required=False)
    with TestClient(app) as client:
        yield client


def _assert_error(response, *, status: int, code: str, request_id: str) -> None:
    assert response.status_code == status
    assert response.json() == {
        "error": response.json()["error"],
        "code": code,
        "request_id": request_id,
    }
    assert response.headers["x-request-id"] == request_id


def test_health_and_readiness_match_go_plain_text_contract(wire_client):
    for path in ("/healthz", "/readyz"):
        response = wire_client.get(path)
        assert response.status_code == 200
        assert response.text == "ok"
        assert response.headers["content-type"].startswith("text/plain")


def test_auth_middleware_distinguishes_missing_invalid_and_expired_tokens(
    wire_client,
):
    missing = wire_client.get(
        "/api/status", headers={"X-Request-ID": "wire-missing"}
    )
    _assert_error(
        missing, status=401, code="missing_token", request_id="wire-missing"
    )
    assert missing.json()["error"] == "缺少 Authorization 头"

    malformed_scheme = wire_client.get(
        "/api/status",
        headers={
            "Authorization": "Basic abc",
            "X-Request-ID": "wire-scheme",
        },
    )
    _assert_error(
        malformed_scheme,
        status=401,
        code="missing_token",
        request_id="wire-scheme",
    )

    invalid = wire_client.get(
        "/api/status",
        headers={
            "Authorization": "Bearer definitely-not-a-jwt",
            "X-Request-ID": "wire-invalid",
        },
    )
    _assert_error(
        invalid, status=401, code="invalid_token", request_id="wire-invalid"
    )

    registered = wire_client.post(
        "/api/auth/register",
        json={"username": "expiry-user", "password": "strong-password"},
    ).json()
    auth = wire_client.app.state.auth_service
    now = datetime.now(timezone.utc)
    expired_token = jwt.encode(
        {
            "sub": registered["user_id"],
            "username": registered["username"],
            "iat": now - timedelta(hours=2),
            "exp": now - timedelta(hours=1),
            "iss": auth.issuer,
        },
        auth.secret,
        algorithm="HS256",
    )
    expired = wire_client.get(
        "/api/status",
        headers={
            "Authorization": f"Bearer {expired_token}",
            "X-Request-ID": "wire-expired",
        },
    )
    _assert_error(
        expired, status=401, code="token_expired", request_id="wire-expired"
    )
    assert expired.json()["error"] == "访问令牌已过期"


def test_auth_handlers_expose_go_error_codes_and_body_shape(wire_client):
    invalid_input = wire_client.post(
        "/api/auth/register",
        json={},
        headers={"X-Request-ID": "wire-input"},
    )
    _assert_error(
        invalid_input,
        status=400,
        code="invalid_input",
        request_id="wire-input",
    )

    invalid_body = wire_client.post(
        "/api/auth/login",
        content="{",
        headers={
            "Content-Type": "application/json",
            "X-Request-ID": "wire-body",
        },
    )
    _assert_error(
        invalid_body,
        status=400,
        code="invalid_body",
        request_id="wire-body",
    )

    created = wire_client.post(
        "/api/auth/register",
        json={
            "username": "wire-user",
            "password": "strong-password",
            "go_decoder_ignores_this": True,
        },
    )
    assert created.status_code == 200

    duplicate = wire_client.post(
        "/api/auth/register",
        json={"username": "wire-user", "password": "strong-password"},
        headers={"X-Request-ID": "wire-duplicate"},
    )
    _assert_error(
        duplicate,
        status=409,
        code="user_exists",
        request_id="wire-duplicate",
    )

    bad_login = wire_client.post(
        "/api/auth/login",
        json={"username": "wire-user", "password": "wrong-password"},
        headers={"X-Request-ID": "wire-login"},
    )
    _assert_error(
        bad_login,
        status=401,
        code="invalid_credentials",
        request_id="wire-login",
    )
    assert bad_login.json()["error"] == "用户名或密码错误"


def test_memory_go_single_item_contract_and_python_batch_extension(memory_client):
    client, agent = memory_client

    quarantined = client.post(
        "/api/memory/quarantine", json={"id": 7, "reason": "人工复核"}
    )
    assert quarantined.status_code == 200
    assert quarantined.json() == {"ok": True, "id": 7}
    assert agent.ltm.calls[-1] == ([7], "quarantined", "人工复核", None)

    restored = client.post("/api/memory/unquarantine", json={"id": 7})
    assert restored.status_code == 200
    assert restored.json() == {"ok": True, "id": 7}
    assert agent.ltm.calls[-1] == ([7], "active", "", None)

    missing = client.post("/api/memory/quarantine", json={"id": 999})
    assert missing.status_code == 200
    assert missing.json() == {"ok": False, "id": 999}

    batch = client.post(
        "/api/memory/quarantine", json={"ids": [7, 999], "reason": "批量复核"}
    )
    assert batch.status_code == 200
    assert batch.json() == {"ok": True, "changed": [7]}

    invalid = client.post("/api/memory/unquarantine", json={"id": 0})
    assert invalid.status_code == 400
    assert invalid.text == "id required\n"


def test_chat_cancel_matches_go_response_contract(memory_client):
    client, agent = memory_client
    response = client.post("/api/chat/cancel")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "message": "已发送取消信号"}
    assert agent.cancelled is True


def test_chat_stream_common_events_follow_go_order_and_done_terminates(stream_client):
    response = stream_client.post(
        "/api/chat/stream", json={"message": "你好", "use_rag": False}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["connection"] == "keep-alive"

    blocks = [block for block in response.text.split("\n\n") if block]
    events = [
        next(line[7:] for line in block.splitlines() if line.startswith("event: "))
        for block in blocks
    ]
    assert events == ["start", "memory", "route", "step", "token", "done"]
    assert "data: [DONE]" not in response.text
    assert blocks[-1].startswith("event: done\n")


def test_python_contains_every_unconditional_go_http_route(wire_client):
    def normalized(path: str) -> str:
        return re.sub(r"\{[^}]+\}", "{}", path)

    actual = {
        (method, normalized(route.path))
        for route in wire_client.app.routes
        for method in (getattr(route, "methods", None) or set())
    }
    go_subset = {
        ("POST", "/api/auth/register"),
        ("POST", "/api/auth/login"),
        ("GET", "/api/auth/me"),
        ("POST", "/api/chat"),
        ("POST", "/api/chat/stream"),
        ("POST", "/api/chat/cancel"),
        ("POST", "/api/upload"),
        ("POST", "/api/docs/delete"),
        ("GET", "/api/documents/"),
        ("POST", "/api/documents/"),
        ("GET", "/api/documents/{}"),
        ("POST", "/api/documents/{}/ingest"),
        ("GET", "/api/memory/"),
        ("GET", "/api/memory/quarantined"),
        ("POST", "/api/memory/quarantine"),
        ("POST", "/api/memory/unquarantine"),
        ("GET", "/api/memory/superseded"),
        ("GET", "/api/tools"),
        ("POST", "/api/tools/mcp"),
        ("GET", "/api/skills/marketplace"),
        ("GET", "/api/skills/installed"),
        ("POST", "/api/skills/install"),
        ("POST", "/api/skills/uninstall"),
        ("POST", "/api/skills/toggle"),
        ("GET", "/api/snapshots"),
        ("GET", "/api/status"),
        ("GET", "/healthz"),
        ("GET", "/readyz"),
        ("GET", "/api/farm/template.csv"),
        ("POST", "/api/farm/import"),
        ("GET", "/api/farm/records"),
        ("POST", "/api/farm/records"),
        ("GET", "/api/farm/reports"),
        ("POST", "/api/farm/reports"),
        ("GET", "/api/farm/reports/{}"),
    }
    assert go_subset <= actual
