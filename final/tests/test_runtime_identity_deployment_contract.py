from __future__ import annotations

import re
from pathlib import Path

from config.config import APIConfig
from internal.handler.handler import setup_routes


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = PROJECT_ROOT / "Dockerfile"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"

REQUIRED_COMPONENT_ENV = {
    "AGI_EXPERIMENT_COMPONENT_MANIFEST_PATH",
    "AGI_EXPERIMENT_COMPONENT_MANIFEST_SHA256",
    "AGI_RAG_CORPUS_VERSION",
    "AGI_RAG_CORPUS_SHA256",
    "AGI_RAG_INDEX_VERSION",
    "AGI_RAG_INDEX_SHA256",
    "AGI_APPLICATION_BUILD_SHA",
    "AGI_GIT_COMMIT_SHA",
    "AGI_IMAGE_DIGEST",
}


def _environment_assignments() -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        assert name not in result, f"duplicate environment example: {name}"
        result[name] = value
    return result


def test_runtime_image_drops_root_before_healthcheck_and_entrypoint():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    user_position = dockerfile.index("USER 10001:10001")

    assert "addgroup --system --gid 10001 agi" in dockerfile
    assert "adduser --system --uid 10001 --ingroup agi" in dockerfile
    assert user_position < dockerfile.index("HEALTHCHECK")
    assert user_position < dockerfile.index("ENTRYPOINT")
    assert "USER root" not in dockerfile[user_position:]


def test_only_runtime_tree_is_granted_to_the_application_user():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "--home /app/runtime/home/agi" in dockerfile
    assert "mkdir -p /app/runtime/home/agi/Desktop/agi-saber" in dockerfile
    assert "chown -R agi:agi /app/runtime" in dockerfile
    assert "chmod -R u+rwX,go-rwx /app/runtime" in dockerfile
    assert re.search(r"chown\s+-R\s+agi:agi\s+/app(?:\s|\\|$)", dockerfile) is None
    assert "AGI_EVAL_DATABASE_URL=sqlite:////app/runtime/application.db" in dockerfile


def test_bundled_code_configuration_and_migrations_are_marked_read_only():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    immutable_command = re.search(
        r"chmod -R a-w (?P<targets>.*?)\\\n\s*&& chown",
        dockerfile,
        flags=re.DOTALL,
    )

    assert immutable_command is not None
    targets = immutable_command.group("targets")
    for expected in (
        "/app/config",
        "/app/internal",
        "/app/alembic",
        "/app/web",
        "/app/main.py",
        "/app/alembic.ini",
        "/app/requirements.txt",
    ):
        assert expected in targets


def test_env_example_declares_every_verified_component_input_once():
    values = _environment_assignments()

    assert REQUIRED_COMPONENT_ENV <= set(values)
    assert values["AGI_EXPERIMENT_COMPONENT_MANIFEST_PATH"] == (
        "/run/agi/component-manifest.json"
    )
    for name in REQUIRED_COMPONENT_ENV - {"AGI_EXPERIMENT_COMPONENT_MANIFEST_PATH"}:
        assert values[name] == ""


def test_legacy_self_reported_fingerprint_is_explicitly_insufficient():
    contents = ENV_EXAMPLE.read_text(encoding="utf-8")
    legacy_position = contents.index(
        "AGI_EXPERIMENT_RUNTIME_ENVIRONMENT_FINGERPRINT="
    )
    manifest_position = contents.index("AGI_EXPERIMENT_COMPONENT_MANIFEST_PATH=")
    nearby = contents[max(0, legacy_position - 240) : manifest_position].lower()

    assert "legacy compatibility only" in nearby
    assert "can never satisfy" in nearby
    assert "by itself" in nearby
    assert "scripts/build_runtime_component_manifest.py" in contents
    assert "read-only (0444)" in contents


def test_application_wires_verified_runtime_provider_in_production(
    tmp_path: Path,
    monkeypatch,
    runtime_identity_factory,
):
    identity = runtime_identity_factory("application-wiring")
    calls = []
    monkeypatch.setenv(
        "AGI_EVAL_DATABASE_URL",
        f"sqlite+pysqlite:///{(tmp_path / 'application-wiring.db').as_posix()}",
    )
    monkeypatch.setenv("AGI_ONLINE_TRAFFIC_PROVENANCE", "production_authenticated")
    monkeypatch.setenv(
        "AGI_EXPERIMENT_HMAC_SECRET",
        "application-runtime-wiring-secret-at-least-32-bytes",
    )
    monkeypatch.setenv(
        "AGI_EXPERIMENT_RUNTIME_ENVIRONMENT_FINGERPRINT", "f" * 64
    )

    def verified_provider(config):
        calls.append(config)
        return identity

    monkeypatch.setattr(
        "internal.application.api.production_runtime_identity_from_environment",
        verified_provider,
    )

    class Agent:
        pass

    class Infra:
        pass

    cfg = APIConfig()
    cfg.enable_experiments = True
    app = setup_routes(Agent(), Infra(), cfg, auth_required=True)
    try:
        readiness = app.state.experiment_service.readiness()
        assert calls
        assert readiness["runtime_component_manifest_verified"] is True
        assert readiness["runtime_environment_fingerprint_configured"] is True
        assert readiness["runtime_identity_evidence"]["schema_version"] == (
            "agi-runtime-identity-v1"
        )
        assert readiness["runtime_identity_error_code"] is None
        assert readiness["can_route"] is False
        assert "production_evidence_store_not_ready" in readiness["blockers"]
        assert readiness["runtime_environment_fingerprint_configured"] is True
        assert app.state.experiment_service.runtime_environment_fingerprint != "f" * 64
    finally:
        app.state.evaluation_service_registry.close()
        app.state.evaluation_service.close()
        app.state.experiment_service.close()
        app.state.application_store.close()
