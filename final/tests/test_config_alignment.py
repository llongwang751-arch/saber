import pytest

from config.config import default_config


def test_alignment_config_defaults(tmp_path):
    config_path = tmp_path / "empty.yaml"
    config_path.write_text("", encoding="utf-8")

    cfg = default_config(str(config_path))

    assert cfg.rag_rewrite_enabled is False
    assert cfg.rag_rewrite_num_queries == 3
    assert cfg.rag_rerank_enabled is False
    assert cfg.rag_rerank_preview_len == 200
    assert cfg.rag_rerank_failure_threshold == 3
    assert cfg.rag_rerank_cooldown_seconds == 30.0
    assert cfg.rag_rerank_half_open_max_calls == 1
    assert cfg.rag_rerank_fallback_mode == "rrf"
    assert cfg.rag_rerank_cross_encoder_model == ""
    assert cfg.rag_no_answer_threshold == 0.30
    assert cfg.rag_parent_dedup_threshold == 0.85
    assert cfg.trace_retention_days == 30
    assert cfg.embedding_failure_threshold == 3
    assert cfg.rag_retrieval_failure_threshold == 3
    assert cfg.graph_max_parallel == 2
    assert cfg.graph_race_timeout_ms == 30000
    assert cfg.graph_enable_racing is True


def test_alignment_config_reads_rag_and_graph_runtime(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
rag:
  rewrite:
    enabled: true
    num_queries: 4
  rerank:
    enabled: true
    preview_len: 320
    failure_threshold: 5
    cooldown_seconds: 12.5
    half_open_max_calls: 2
    fallback_mode: cross_encoder
    cross_encoder_model: local/reranker
  no_answer_threshold: 0.42
  parent_dedup_threshold: 0.91
  retrieval_circuit:
    failure_threshold: 4
    cooldown_seconds: 9
    half_open_max_calls: 2
embedding:
  failure_threshold: 6
  cooldown_seconds: 7
  half_open_max_calls: 3
observability:
  trace_retention_days: 14
graph_runtime:
  max_parallel: 5
  race_timeout_ms: 1234
  enable_racing: false
""",
        encoding="utf-8",
    )

    cfg = default_config(str(config_path))

    assert cfg.rag_rewrite_enabled is True
    assert cfg.rag_rewrite_num_queries == 4
    assert cfg.rag_rerank_enabled is True
    assert cfg.rag_rerank_preview_len == 320
    assert cfg.rag_rerank_failure_threshold == 5
    assert cfg.rag_rerank_cooldown_seconds == 12.5
    assert cfg.rag_rerank_half_open_max_calls == 2
    assert cfg.rag_rerank_fallback_mode == "cross_encoder"
    assert cfg.rag_rerank_cross_encoder_model == "local/reranker"
    assert cfg.rag_no_answer_threshold == 0.42
    assert cfg.rag_parent_dedup_threshold == 0.91
    assert cfg.trace_retention_days == 14
    assert cfg.embedding_failure_threshold == 6
    assert cfg.embedding_cooldown_seconds == 7
    assert cfg.embedding_half_open_max_calls == 3
    assert cfg.rag_retrieval_failure_threshold == 4
    assert cfg.rag_retrieval_cooldown_seconds == 9
    assert cfg.rag_retrieval_half_open_max_calls == 2
    assert cfg.graph_max_parallel == 5
    assert cfg.graph_race_timeout_ms == 1234
    assert cfg.graph_enable_racing is False


def test_config_rejects_unknown_top_level_field(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("lllm:\n  model: typo\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unknown config field"):
        default_config(str(config_path))


def test_config_rejects_unknown_nested_field(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
rag:
  chunk_size: 200
  chunk_szie: 999
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="rag.chunk_szie"):
        default_config(str(config_path))


def test_default_config_prefers_local_config(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    config_dir = project_root / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "server:\n  port: 8000\n",
        encoding="utf-8",
    )
    (config_dir / "config.local.yaml").write_text(
        "server:\n  port: 9001\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("AGI_PROJECT_ROOT", str(project_root))

    cfg = default_config()

    assert cfg.server_port == "9001"
