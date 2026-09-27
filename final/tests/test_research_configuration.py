from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from config.config import APIConfig, default_config
from internal.handler.handler import setup_routes
from internal.tools.manifest import apply_tool_manifest, load_manifest
from internal.tools.tools import Tool, ToolExecutor


def test_minimal_research_config_and_environment_override(tmp_path, monkeypatch):
    monkeypatch.setattr("config.config._load_dotenv_best_effort", lambda: None)
    monkeypatch.delenv("AGI_RAG_LIGHTWEIGHT", raising=False)
    path = tmp_path / "conf.yaml"
    path.write_text("rag:\n  profile: lightweight\nfeatures:\n  medical: true\nresearch:\n  max_rounds: 4\ntools:\n  manifest: tools.yaml\n", encoding="utf-8")
    monkeypatch.setenv("AGI_ENABLE_MEDICAL", "false")
    monkeypatch.setenv("AGI_RESEARCH_MAX_ROUNDS", "5")
    cfg = default_config(str(path))
    assert cfg.rag_lightweight_enabled
    assert not cfg.enable_medical and not cfg.enable_farm and not cfg.enable_experiments
    assert cfg.research_max_rounds == 5
    assert cfg.tools_manifest == str(tmp_path / "tools.yaml")


@pytest.mark.parametrize("setting", ["research:\n  max_rounds: 0", "research:\n  max_sources: true", "features:\n  medical: perhaps", "rag:\n  profile: invalid"])
def test_invalid_research_settings_fail_early(tmp_path, monkeypatch, setting):
    monkeypatch.setattr("config.config._load_dotenv_best_effort", lambda: None)
    path = tmp_path / "conf.yaml"
    path.write_text(setting, encoding="utf-8")
    with pytest.raises(ValueError):
        default_config(str(path))


@pytest.mark.parametrize("enabled", [False, True])
def test_optional_features_are_absent_or_explicitly_enabled(tmp_path, monkeypatch, enabled):
    monkeypatch.setenv("AGI_EVAL_DATABASE_URL", f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    cfg = APIConfig()
    cfg.enable_medical = cfg.enable_farm = cfg.enable_experiments = enabled
    registered = []
    agent = SimpleNamespace(status=lambda: {}, add_tool=lambda tool: registered.append(tool.name))
    with TestClient(setup_routes(agent, SimpleNamespace(), cfg)) as client:
        features = client.get("/api/status").json()["features"]
        assert features == {"research": True, "medical": enabled, "farm": enabled, "experiments": enabled}
        routes = client.get("/openapi.json").json()["paths"]
        assert ("/api/medical/score" in routes) == enabled
        assert ("/api/farm/records" in routes) == enabled
        assert any("/experiments" in path for path in routes) == enabled
        assert ("medical_copilot" in registered) == enabled
        assert client.get("/api/farm/records").status_code == (200 if enabled else 404)
        assert (client.app.state.experiment_service is not None) == enabled


def test_manifest_disables_tools_and_discovers_only_enabled_servers(tmp_path):
    path = tmp_path / "tools.yaml"
    path.write_text("builtins:\n  search_web: false\nmcp_servers:\n  - name: disabled\n    enabled: false\n    endpoint: https://unused.example/mcp\n  - name: live\n    endpoint: https://tools.example/mcp\n", encoding="utf-8")
    calls = []
    agent = SimpleNamespace(cfg=SimpleNamespace(tools_manifest=str(path)),
                            tool_executor=ToolExecutor([Tool("search_web", "", [], lambda _: "")]),
                            register_mcp_server=lambda endpoint: calls.append(endpoint))
    apply_tool_manifest(agent)
    assert "search_web" not in agent.tool_executor.snapshot()
    assert calls == ["https://tools.example/mcp"]


@pytest.mark.parametrize("content", ["builtins: {exec_command: yesplease}", "mcp_servers: {}", "builtins: {unknown: true}", "mcp_servers: [{name: x, endpoint: 'file:///secrets'}]"])
def test_manifest_rejects_unsafe_or_malformed_configuration(tmp_path, content):
    path = tmp_path / "tools.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        load_manifest(path)
