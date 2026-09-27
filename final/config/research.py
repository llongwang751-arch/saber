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


def initialize(cfg):
    for key, (default, _, _) in LIMITS.items():
        setattr(cfg, f"research_{key}", default)
    cfg.tools_manifest = ""


def configure(cfg, data):
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
    return {"research": True}
