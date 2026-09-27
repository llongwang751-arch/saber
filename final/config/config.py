# config — 配置管理（与主分支 Go 版字段对齐的 Python 配置）
import logging
import os
import re
from typing import Any, Dict, List, Optional

import yaml
from . import research as research_config

logger = logging.getLogger(__name__)

# 项目根目录（final/）：本文件位于 final/config/config.py
DEFAULT_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate_env(value: Any) -> Any:
    """把配置里的 ${VAR} 占位符替换为环境变量（未设置时替换为空串）。"""
    if isinstance(value, str):
        return _ENV_PLACEHOLDER.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(v) for v in value]
    return value


def _project_root() -> str:
    return os.environ.get("AGI_PROJECT_ROOT", DEFAULT_PROJECT_ROOT)


_CONFIG_SCHEMA = {
    "features": {"medical", "farm", "experiments"},
    "research": set(research_config.LIMITS),
    "tools": {"manifest"},
    "llm": {"api_url", "api_key", "model", "fast_model", "temperature"},
    "embedding": {
        "api_url",
        "api_key",
        "model",
        "failure_threshold",
        "cooldown_seconds",
        "half_open_max_calls",
    },
    "rerank": {"api_url", "api_key", "model"},
    "milvus": {"host", "port"},
    "postgres": {"host", "port", "user", "password", "database"},
    "elasticsearch": {"addresses", "username", "password"},
    "kafka": {"brokers", "topic"},
    "neo4j": {"uri", "user", "password", "max_hops", "weight", "enabled"},
    "rag": {
        "profile",
        "chunk_size",
        "chunk_overlap",
        "top_k",
        "rrf_constant_k",
        "semantic_weight",
        "enable_hybrid_search",
        "rag_milvus_dim",
        "no_answer_threshold",
        "fallback_min_overlap",
        "cross_encoder_threshold",
        "require_citations",
        "calibrated_modes",
        "parent_dedup_threshold",
        "rewrite",
        "rerank",
        "retrieval_circuit",
    },
    "memory": {"short_term_max_turns", "long_term_top_k", "consolidation", "compactor"},
    "harness": {"max_retries", "retry_delay_ms", "step_timeout_ms", "max_iterations", "max_llm_calls_per_turn", "max_tool_calls_per_turn"},
    "graph_runtime": {
        "max_parallel",
        "race_timeout_ms",
        "enable_racing",
        "replan_enabled",
        "max_replan",
        "replan_on_failed",
    },
    "search": {"api_key", "api_url"},
    "server": {"port", "cors_origins"},
    "sandbox": {
        "enabled",
        "backend",
        "image",
        "timeout_ms",
        "max_output_bytes",
        "memory_limit_mb",
        "cpu_percent",
        "max_pids",
        "network_disabled",
        "readonly_rootfs",
        "artifact_host_dir",
    },
    "security": {"max_command_length", "allowlist_mode", "allowlist"},
    "auth": {"jwt_secret", "jwt_ttl_hours", "jwt_issuer"},
    "observability": {"trace_retention_days", "pprof"},
    "skillhub": {"enabled", "github_token", "keyword", "cache_ttl_min"},
}

_NESTED_CONFIG_SCHEMA = {
    ("rag", "rewrite"): {"enabled", "num_queries"},
    ("rag", "rerank"): {
        "enabled",
        "preview_len",
        "failure_threshold",
        "cooldown_seconds",
        "half_open_max_calls",
        "fallback_mode",
        "cross_encoder_model",
    },
    ("rag", "retrieval_circuit"): {
        "failure_threshold",
        "cooldown_seconds",
        "half_open_max_calls",
    },
    ("memory", "consolidation"): {
        "similarity_threshold",
        "dedup_threshold",
        "ttl_days",
        "decay_rate",
        "min_importance",
        "trigger_interval",
    },
    ("memory", "compactor"): {
        "enabled",
        "max_recent_turns",
        "char_watermark",
    },
    ("observability", "pprof"): {"enabled", "admin_token"},
}


class APIConfig:
    """整合 Python 版主干能力的运行配置（字段名与 Go 版 APIConfig 对齐）。"""

    def __init__(self):
        research_config.initialize(self)
        self.rag_lightweight_enabled = False
        self.rag_lightweight_path = os.path.join(_project_root(), 'runtime', 'retrieval')
        # ---- LLM / Embedding ----
        self.llm_api_url = ""
        self.llm_api_key = ""
        self.llm_model = ""
        self.llm_fast_model = ""
        self.temperature = 0.7
        self.embedding_api_url = ""
        self.embedding_api_key = ""
        self.embedding_model = ""
        self.embedding_failure_threshold = 3
        self.embedding_cooldown_seconds = 30.0
        self.embedding_half_open_max_calls = 1
        self.rerank_api_url = ""
        self.rerank_api_key = ""
        self.rerank_model = ""

        # ---- Milvus ----
        self.milvus_host = ""
        self.milvus_port = 19530

        # ---- Postgres ----
        self.pg_host = ""
        self.pg_port = 5432
        self.pg_user = ""
        self.pg_password = ""
        self.pg_database = ""

        # ---- Elasticsearch ----
        self.es_addresses: List[str] = []
        self.es_username = ""
        self.es_password = ""

        # ---- Kafka ----
        self.kafka_brokers: List[str] = []
        self.kafka_topic = ""

        # ---- Neo4j（知识图谱）----
        self.neo4j_uri = ""
        self.neo4j_user = ""
        self.neo4j_password = ""
        self.kg_max_hops = 2
        self.kg_weight = 0.3
        self.kg_enabled = False

        # ---- RAG ----
        self.chunk_size = 200
        self.chunk_overlap = 50
        self.top_k = 3
        self.rrf_constant_k = 60
        self.semantic_weight = 0.7
        self.enable_hybrid_search = False
        self.rag_milvus_dim = 1024
        self.rag_no_answer_threshold = 0.30
        self.rag_fallback_min_overlap = 0.08
        self.rag_cross_encoder_threshold = 0.5
        self.rag_require_citations = True
        self.rag_calibrated_modes = []
        self.memory_store_exchange_facts = False
        self.rag_parent_dedup_threshold = 0.85
        self.rag_rewrite_enabled = False
        self.rag_rewrite_num_queries = 3
        self.rag_rerank_enabled = False
        self.rag_rerank_preview_len = 200
        self.rag_rerank_failure_threshold = 3
        self.rag_rerank_cooldown_seconds = 30.0
        self.rag_rerank_half_open_max_calls = 1
        self.rag_rerank_fallback_mode = "rrf"
        self.rag_rerank_cross_encoder_model = ""
        self.rag_retrieval_failure_threshold = 3
        self.rag_retrieval_cooldown_seconds = 30.0
        self.rag_retrieval_half_open_max_calls = 1

        # ---- Memory ----
        self.short_term_max_turns = 5
        self.long_term_top_k = 3
        self.memory_consolidation_similarity = 0.80
        self.memory_consolidation_dedup = 0.95
        self.memory_consolidation_ttl_days = 30
        self.memory_consolidation_decay_rate = 0.995
        self.memory_consolidation_min_import = 0.3
        self.memory_consolidation_trigger = 5
        self.enable_context_compactor = True
        self.context_compactor_max_recent_turns = 3
        self.context_compactor_char_watermark = 1500

        # ---- Harness ----
        self.max_retries = 3
        self.retry_delay_ms = 200
        self.step_timeout_ms = 5000
        self.max_iterations = 5
        self.max_llm_calls_per_turn = 24
        self.max_tool_calls_per_turn = 32

        # ---- Graph Runtime ----
        self.graph_max_parallel = 2
        self.graph_race_timeout_ms = 30000
        self.graph_enable_racing = True
        self.graph_replan_enabled = False
        self.graph_max_replan = 2
        self.graph_replan_on_failed = False

        # ---- Search ----
        self.search_api_key = ""
        self.search_api_url = ""

        # ---- Server ----
        self.server_port = "8090"
        self.cors_origins: List[str] = []

        # ---- Auth / Observability / SkillHub ----
        self.auth_jwt_secret = ""
        self.auth_jwt_ttl_hours = 168
        self.auth_jwt_issuer = "agi-assistant"
        self.trace_retention_days = 30
        self.pprof_enabled = False
        self.pprof_admin_token = ""
        self.skillhub_enabled = True
        self.skillhub_github_token = ""
        self.skillhub_keyword = "office assistant"
        self.skillhub_cache_ttl_min = 30

        # ---- Sandbox ----
        self.sandbox_enabled = False
        self.sandbox_backend = "docker"
        self.sandbox_image = "ubuntu:22.04"
        self.sandbox_timeout_ms = 30000
        self.sandbox_max_output = 65536
        self.sandbox_memory_mb = 256
        self.sandbox_cpu_percent = 50
        self.sandbox_max_pids = 64
        self.sandbox_net_disabled = True
        self.sandbox_readonly = True
        self.sandbox_artifact_host_dir = ""

        # ---- Security ----
        self.sec_max_cmd_length = 500
        self.sec_allowlist_mode = False
        self.sec_allowlist: List[str] = []

    # ---- helpers ----
    def is_real_llm(self) -> bool:
        return bool(self.llm_api_key)

    def is_real_embedding(self) -> bool:
        return bool(self.embedding_api_key)

    def pg_dsn(self) -> str:
        return (
            f"postgres://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_database}?sslmode=disable"
        )

    def milvus_addr(self) -> str:
        return f"{self.milvus_host}:{self.milvus_port}"


def _read_yaml(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("读取 %s 失败，使用默认值: %s", path, e)
        return {}


def _validate_config_schema(data: Dict[str, Any]) -> None:
    for section, value in (data or {}).items():
        if section not in _CONFIG_SCHEMA:
            raise ValueError(f"unknown config field: {section}")
        if value is None:
            continue
        if not isinstance(value, dict):
            raise ValueError(f"config section {section} must be a mapping")
        allowed = _CONFIG_SCHEMA[section]
        for key, nested in value.items():
            if key not in allowed:
                raise ValueError(f"unknown config field: {section}.{key}")
            nested_allowed = _NESTED_CONFIG_SCHEMA.get((section, key))
            if nested_allowed is None or nested is None:
                continue
            if not isinstance(nested, dict):
                raise ValueError(f"config section {section}.{key} must be a mapping")
            for nested_key in nested:
                if nested_key not in nested_allowed:
                    raise ValueError(f"unknown config field: {section}.{key}.{nested_key}")


def _resolve_config_path(explicit: Optional[str]) -> str:
    """按优先级找 config：参数 > 环境变量 > 本地配置 > 项目默认 > cwd。"""
    candidates: List[str] = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("AGI_CONFIG")
    if env:
        candidates.append(env)
    root = _project_root()
    candidates.append(os.path.join(root, "config", "config.local.yaml"))
    candidates.append(os.path.join(root, "config", "conf.yaml"))
    candidates.append(os.path.join(root, "config", "config.yaml"))
    candidates.append("config/config.yaml")
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return candidates[-1]


def default_config(config_path: Optional[str] = None) -> APIConfig:
    """从 config.yaml 加载配置（与 Go 版 DefaultConfig 字段一一对齐）。"""
    c = APIConfig()
    path = _resolve_config_path(config_path)
    _load_dotenv_best_effort()
    data = _interpolate_env(_read_yaml(path))
    _validate_config_schema(data)

    if llm := data.get("llm"):
        c.llm_api_url = llm.get("api_url", "")
        c.llm_api_key = llm.get("api_key", "")
        c.llm_model = llm.get("model", "")
        c.llm_fast_model = llm.get("fast_model", "")
        c.temperature = llm.get("temperature", 0.7)

    if emb := data.get("embedding"):
        c.embedding_api_url = emb.get("api_url", "")
        c.embedding_api_key = emb.get("api_key", "")
        c.embedding_model = emb.get("model", "")
        c.embedding_failure_threshold = emb.get("failure_threshold", 3)
        c.embedding_cooldown_seconds = emb.get("cooldown_seconds", 30.0)
        c.embedding_half_open_max_calls = emb.get("half_open_max_calls", 1)

    if rerank := data.get("rerank"):
        c.rerank_api_url = rerank.get("api_url", "")
        c.rerank_api_key = rerank.get("api_key", "")
        c.rerank_model = rerank.get("model", "")

    if milvus := data.get("milvus"):
        c.milvus_host = milvus.get("host", "")
        c.milvus_port = milvus.get("port", 19530)

    if pg := data.get("postgres"):
        c.pg_host = pg.get("host", "")
        c.pg_port = pg.get("port", 5432)
        c.pg_user = pg.get("user", "")
        c.pg_password = pg.get("password", "")
        c.pg_database = pg.get("database", "")

    if es := data.get("elasticsearch"):
        c.es_addresses = es.get("addresses", []) or []
        c.es_username = es.get("username", "")
        c.es_password = es.get("password", "")

    if kafka := data.get("kafka"):
        c.kafka_brokers = kafka.get("brokers", []) or []
        c.kafka_topic = kafka.get("topic", "")

    if neo4j := data.get("neo4j"):
        c.neo4j_uri = neo4j.get("uri", "")
        c.neo4j_user = neo4j.get("user", "")
        c.neo4j_password = neo4j.get("password", "")
        c.kg_max_hops = neo4j.get("max_hops", 2)
        c.kg_weight = neo4j.get("weight", 0.3)
        c.kg_enabled = bool(neo4j.get("enabled", False))

    if rag := data.get("rag"):
        if "profile" in rag:
            if rag["profile"] not in {"local", "lightweight", "full"}:
                raise ValueError("rag.profile must be local, lightweight or full")
            c.rag_lightweight_enabled = rag["profile"] == "lightweight"
        c.chunk_size = rag.get("chunk_size", 200)
        c.chunk_overlap = rag.get("chunk_overlap", 50)
        c.top_k = rag.get("top_k", 3)
        c.rrf_constant_k = rag.get("rrf_constant_k", 60)
        c.semantic_weight = rag.get("semantic_weight", 0.7)
        c.enable_hybrid_search = rag.get("enable_hybrid_search", False)
        c.rag_milvus_dim = rag.get("rag_milvus_dim", 1024)
        c.rag_no_answer_threshold = rag.get("no_answer_threshold", 0.30)
        c.rag_fallback_min_overlap = float(rag.get("fallback_min_overlap", 0.08))
        c.rag_cross_encoder_threshold = float(rag.get("cross_encoder_threshold", 0.5))
        c.rag_require_citations = bool(rag.get("require_citations", True))
        c.rag_calibrated_modes = list(rag.get("calibrated_modes", []))
        c.rag_parent_dedup_threshold = rag.get("parent_dedup_threshold", 0.85)
        rewrite = rag.get("rewrite", {}) or {}
        c.rag_rewrite_enabled = bool(rewrite.get("enabled", False))
        c.rag_rewrite_num_queries = rewrite.get("num_queries", 3)
        rerank = rag.get("rerank", {}) or {}
        c.rag_rerank_enabled = bool(rerank.get("enabled", False))
        c.rag_rerank_preview_len = rerank.get("preview_len", 200)
        c.rag_rerank_failure_threshold = rerank.get("failure_threshold", 3)
        c.rag_rerank_cooldown_seconds = rerank.get("cooldown_seconds", 30.0)
        c.rag_rerank_half_open_max_calls = rerank.get("half_open_max_calls", 1)
        c.rag_rerank_fallback_mode = rerank.get("fallback_mode", "rrf")
        c.rag_rerank_cross_encoder_model = rerank.get("cross_encoder_model", "")
        circuit = rag.get("retrieval_circuit", {}) or {}
        c.rag_retrieval_failure_threshold = circuit.get("failure_threshold", 3)
        c.rag_retrieval_cooldown_seconds = circuit.get("cooldown_seconds", 30.0)
        c.rag_retrieval_half_open_max_calls = circuit.get("half_open_max_calls", 1)

    if memory := data.get("memory"):
        c.short_term_max_turns = memory.get("short_term_max_turns", 5)
        c.long_term_top_k = memory.get("long_term_top_k", 3)
        cons = memory.get("consolidation", {}) or {}
        c.memory_consolidation_similarity = cons.get("similarity_threshold", 0.80)
        c.memory_consolidation_dedup = cons.get("dedup_threshold", 0.95)
        c.memory_consolidation_ttl_days = cons.get("ttl_days", 30)
        c.memory_consolidation_decay_rate = cons.get("decay_rate", 0.995)
        c.memory_consolidation_min_import = cons.get("min_importance", 0.3)
        c.memory_consolidation_trigger = cons.get("trigger_interval", 5)
        if comp := memory.get("compactor", {}) or {}:
            c.enable_context_compactor = bool(comp.get("enabled", True))
            c.context_compactor_max_recent_turns = int(comp.get("max_recent_turns", 3))
            c.context_compactor_char_watermark = int(comp.get("char_watermark", 1500))

    if harness := data.get("harness"):
        c.max_retries = harness.get("max_retries", 3)
        c.retry_delay_ms = harness.get("retry_delay_ms", 200)
        c.step_timeout_ms = harness.get("step_timeout_ms", 5000)
        c.max_iterations = harness.get("max_iterations", 5)
        c.max_llm_calls_per_turn = max(1, int(harness.get("max_llm_calls_per_turn", 24)))
        c.max_tool_calls_per_turn = max(1, int(harness.get("max_tool_calls_per_turn", 32)))

    if graph_runtime := data.get("graph_runtime"):
        c.graph_max_parallel = graph_runtime.get("max_parallel", 2)
        c.graph_race_timeout_ms = graph_runtime.get("race_timeout_ms", 30000)
        c.graph_enable_racing = bool(graph_runtime.get("enable_racing", True))
        c.graph_replan_enabled = bool(graph_runtime.get("replan_enabled", False))
        c.graph_max_replan = graph_runtime.get("max_replan", 2)
        c.graph_replan_on_failed = bool(graph_runtime.get("replan_on_failed", False))

    if search := data.get("search"):
        c.search_api_key = search.get("api_key", "")
        c.search_api_url = search.get("api_url", "")

    if server := data.get("server"):
        c.server_port = str(server.get("port", "8090"))
        c.cors_origins = server.get("cors_origins", []) or []

    if sb := data.get("sandbox"):
        c.sandbox_enabled = bool(sb.get("enabled", False))
        c.sandbox_backend = sb.get("backend", "docker")
        c.sandbox_image = sb.get("image", "ubuntu:22.04")
        c.sandbox_timeout_ms = sb.get("timeout_ms", 30000)
        c.sandbox_max_output = sb.get("max_output_bytes", 65536)
        c.sandbox_memory_mb = sb.get("memory_limit_mb", 256)
        c.sandbox_cpu_percent = sb.get("cpu_percent", 50)
        c.sandbox_max_pids = sb.get("max_pids", 64)
        c.sandbox_net_disabled = bool(sb.get("network_disabled", True))
        c.sandbox_readonly = bool(sb.get("readonly_rootfs", True))
        c.sandbox_artifact_host_dir = os.path.expanduser(
            str(sb.get("artifact_host_dir", "") or "")
        )

    if sec := data.get("security"):
        c.sec_max_cmd_length = sec.get("max_command_length", 500)
        c.sec_allowlist_mode = bool(sec.get("allowlist_mode", False))
        c.sec_allowlist = sec.get("allowlist", []) or []

    if auth := data.get("auth"):
        c.auth_jwt_secret = auth.get("jwt_secret", "")
        c.auth_jwt_ttl_hours = auth.get("jwt_ttl_hours", 168)
        c.auth_jwt_issuer = auth.get("jwt_issuer", "agi-assistant")

    if obs := data.get("observability"):
        c.trace_retention_days = obs.get("trace_retention_days", 30)
        pprof = obs.get("pprof", {}) or {}
        c.pprof_enabled = bool(pprof.get("enabled", False))
        c.pprof_admin_token = pprof.get("admin_token", "")

    if hub := data.get("skillhub"):
        c.skillhub_enabled = bool(hub.get("enabled", True))
        c.skillhub_github_token = hub.get("github_token", "")
        c.skillhub_keyword = hub.get("keyword", "office assistant")
        c.skillhub_cache_ttl_min = hub.get("cache_ttl_min", 30)

    _apply_environment_overrides(c)
    research_config.configure(c, data)
    if c.tools_manifest and not os.path.isabs(c.tools_manifest):
        c.tools_manifest = os.path.join(os.path.dirname(os.path.abspath(path)), c.tools_manifest)
    _apply_defaults(c)
    return c


def _apply_environment_overrides(c: APIConfig) -> None:
    if 'AGI_RAG_LIGHTWEIGHT' in os.environ:
        c.rag_lightweight_enabled = research_config.boolean(os.environ['AGI_RAG_LIGHTWEIGHT'], 'AGI_RAG_LIGHTWEIGHT')
    c.rag_lightweight_path = os.getenv('AGI_RAG_INDEX_DIR', c.rag_lightweight_path)
    env_map = {
        "AGI_LLM_API_URL": "llm_api_url",
        "AGI_LLM_API_KEY": "llm_api_key",
        "AGI_LLM_MODEL": "llm_model",
        "AGI_LLM_FAST_MODEL": "llm_fast_model",
        "TAVILY_API_KEY": "search_api_key",
        "AGI_SEARCH_API_KEY": "search_api_key",
        "AGI_SEARCH_API_URL": "search_api_url",
        "AGI_EMBEDDING_API_URL": "embedding_api_url",
        "AGI_EMBEDDING_API_KEY": "embedding_api_key",
        "AGI_EMBEDDING_MODEL": "embedding_model",
        "AGI_RERANK_API_URL": "rerank_api_url",
        "AGI_RERANK_API_KEY": "rerank_api_key",
        "AGI_RERANK_MODEL": "rerank_model",
        "AGI_JWT_SECRET": "auth_jwt_secret",
        "AGI_JWT_TTL_HOURS": "auth_jwt_ttl_hours",
        "AGI_JWT_ISSUER": "auth_jwt_issuer",
        "AGI_ES_USERNAME": "es_username",
        "AGI_ES_PASSWORD": "es_password",
        "AGI_NEO4J_URI": "neo4j_uri",
        "AGI_NEO4J_USER": "neo4j_user",
        "AGI_NEO4J_PASSWORD": "neo4j_password",
        "PPROF_ADMIN_TOKEN": "pprof_admin_token",
        "GITHUB_TOKEN": "skillhub_github_token",
    }
    for env_k, attr_k in env_map.items():
        v = os.getenv(env_k)
        if v and v.strip():
            raw = v.strip()
            if env_k.endswith("TTL_HOURS"):
                try:
                    raw = int(raw)
                except ValueError:
                    logger.warning("环境变量 %s 不是合法整数，已忽略: %s", env_k, v)
                    continue
            setattr(c, attr_k, raw)

    if addresses := os.getenv("AGI_ES_ADDRESSES"):
        c.es_addresses = [address.strip() for address in addresses.split(",") if address.strip()]
    if enabled := os.getenv("AGI_KG_ENABLED"):
        c.kg_enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}

    if env_comp := os.getenv("AGI_ENABLE_CONTEXT_COMPACTOR"):
        c.enable_context_compactor = env_comp.strip().lower() in {"1", "true", "yes", "on"}
    if env_turns := os.getenv("AGI_CONTEXT_COMPACTOR_MAX_RECENT_TURNS"):
        try:
            c.context_compactor_max_recent_turns = int(env_turns.strip())
        except ValueError:
            pass
    if env_wm := os.getenv("AGI_CONTEXT_COMPACTOR_CHAR_WATERMARK"):
        try:
            c.context_compactor_char_watermark = int(env_wm.strip())
        except ValueError:
            pass


def _load_dotenv_best_effort() -> None:
    try:
        from dotenv import load_dotenv
        env_file = os.path.join(_project_root(), ".env")
        if os.path.exists(env_file):
            load_dotenv(env_file)
        else:
            load_dotenv()
    except Exception:
        logger.debug("python-dotenv 不可用，跳过 .env 加载")


def _apply_defaults(c: APIConfig) -> None:
    if c.rrf_constant_k <= 0:
        c.rrf_constant_k = 60
    if c.semantic_weight <= 0:
        c.semantic_weight = 0.7
    if c.rag_milvus_dim <= 0:
        c.rag_milvus_dim = 1024
    if c.rag_rewrite_num_queries <= 0:
        c.rag_rewrite_num_queries = 3
    if c.rag_rerank_preview_len <= 0:
        c.rag_rerank_preview_len = 200
    if c.rag_rerank_failure_threshold <= 0:
        c.rag_rerank_failure_threshold = 3
    if c.rag_rerank_cooldown_seconds <= 0:
        c.rag_rerank_cooldown_seconds = 30.0
    if c.rag_rerank_half_open_max_calls <= 0:
        c.rag_rerank_half_open_max_calls = 1
    if c.rag_retrieval_failure_threshold <= 0:
        c.rag_retrieval_failure_threshold = 3
    if c.rag_retrieval_cooldown_seconds <= 0:
        c.rag_retrieval_cooldown_seconds = 30.0
    if c.rag_retrieval_half_open_max_calls <= 0:
        c.rag_retrieval_half_open_max_calls = 1
    if c.embedding_failure_threshold <= 0:
        c.embedding_failure_threshold = 3
    if c.embedding_cooldown_seconds <= 0:
        c.embedding_cooldown_seconds = 30.0
    if c.embedding_half_open_max_calls <= 0:
        c.embedding_half_open_max_calls = 1
    if not c.rag_rerank_fallback_mode:
        c.rag_rerank_fallback_mode = "rrf"
    if c.graph_max_replan <= 0:
        c.graph_max_replan = 2
    if c.auth_jwt_ttl_hours <= 0:
        c.auth_jwt_ttl_hours = 168
    if not c.auth_jwt_issuer:
        c.auth_jwt_issuer = "agi-assistant"
    if c.trace_retention_days <= 0:
        c.trace_retention_days = 30
    if c.skillhub_cache_ttl_min <= 0:
        c.skillhub_cache_ttl_min = 30

    if c.memory_consolidation_similarity <= 0:
        c.memory_consolidation_similarity = 0.80
    if c.memory_consolidation_dedup <= 0:
        c.memory_consolidation_dedup = 0.95
    if c.memory_consolidation_ttl_days <= 0:
        c.memory_consolidation_ttl_days = 30
    if c.memory_consolidation_decay_rate <= 0:
        c.memory_consolidation_decay_rate = 0.995
    if c.memory_consolidation_min_import <= 0:
        c.memory_consolidation_min_import = 0.3
    if c.memory_consolidation_trigger <= 0:
        c.memory_consolidation_trigger = 5

    if c.kg_max_hops <= 0:
        c.kg_max_hops = 2
    if c.kg_weight <= 0:
        c.kg_weight = 0.3

    if c.graph_max_parallel <= 0:
        c.graph_max_parallel = 2
    if c.graph_race_timeout_ms <= 0:
        c.graph_race_timeout_ms = 30000

    if not c.sandbox_backend:
        c.sandbox_backend = "docker"
    if not c.sandbox_image:
        c.sandbox_image = "ubuntu:22.04"
    if c.sandbox_timeout_ms <= 0:
        c.sandbox_timeout_ms = 30000
    if c.sandbox_max_output <= 0:
        c.sandbox_max_output = 65536
    if c.sandbox_memory_mb <= 0:
        c.sandbox_memory_mb = 256
    if c.sandbox_cpu_percent <= 0:
        c.sandbox_cpu_percent = 50
    if c.sandbox_max_pids <= 0:
        c.sandbox_max_pids = 64

    if c.sec_max_cmd_length <= 0:
        c.sec_max_cmd_length = 500
