"""Small, validated configuration surface for the research application."""

import os


LIMITS = {
    "max_rounds": (3, 1, 20),
    "max_llm_calls": (16, 1, 200),
    "max_tool_calls": (20, 1, 200),
    "max_sources": (20, 1, 100),
    "max_output_tokens": (6000, 128, 32000),
    "max_context_chars": (24000, 1000, 200000),
    "timeout_seconds": (300, 1, 3600),
}


def boolean(value, name):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    raise ValueError(f"{name} must be a boolean")


def initialize(cfg):
    for name in ("medical", "farm", "experiments"):
        setattr(cfg, f"enable_{name}", False)
    for key, (default, _, _) in LIMITS.items():
        setattr(cfg, f"research_{key}", default)
    cfg.tools_manifest = ""


def configure(cfg, data):
    features = data.get("features") or {}
    for name in ("medical", "farm", "experiments"):
        value = os.getenv(f"AGI_ENABLE_{name.upper()}", features.get(name, False))
        setattr(cfg, f"enable_{name}", boolean(value, f"features.{name}"))
    research = data.get("research") or {}
    for key, (default, low, high) in LIMITS.items():
        raw = os.getenv(f"AGI_RESEARCH_{key.upper()}", research.get(key, default))
        if isinstance(raw, bool) or not str(raw).isdigit() or not low <= int(raw) <= high:
            raise ValueError(f"research.{key} must be an integer between {low} and {high}")
        setattr(cfg, f"research_{key}", int(raw))
    cfg.tools_manifest = os.getenv("AGI_TOOLS_MANIFEST", (data.get("tools") or {}).get("manifest", ""))
    if not isinstance(cfg.tools_manifest, str):
        raise ValueError("tools.manifest must be a path string")


def feature_status(cfg):
    return {"research": True, **{name: bool(getattr(cfg, f"enable_{name}", False))
                               for name in ("medical", "farm", "experiments")}}
