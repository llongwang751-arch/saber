"""Compile immutable offline strategy manifests into safe request overrides."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from internal.evaluation.strategy import validate_strategy_manifest
from pydantic import BaseModel, ConfigDict

from .schemas import RAGRuntimeOverrides


RUNTIME_COMPILER_VERSION = "rag-readonly-v1"


class _RuntimeEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    rag: RAGRuntimeOverrides


def compile_runtime_overrides(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the only runtime knobs allowed by online experiment v1.

    A strategy manifest may carry arbitrary offline metadata, so runtime
    settings live below ``runtime_overrides``.  A top-level ``rag`` envelope is
    accepted as a compact compatibility form.  No ``control`` section exists:
    the control arm always executes with an empty override mapping.
    """

    safe_manifest = validate_strategy_manifest(manifest)
    for forbidden in ("control", "control_overrides"):
        if safe_manifest.get(forbidden):
            raise ValueError("v1 control_overrides must be empty")
    if "runtime_overrides" in safe_manifest:
        raw = safe_manifest["runtime_overrides"]
    elif "rag" in safe_manifest:
        raw = {"rag": safe_manifest["rag"]}
    else:
        raise ValueError("strategy manifest has no runtime_overrides.rag section")
    if not isinstance(raw, Mapping):
        raise ValueError("runtime_overrides must be an object")
    envelope = _RuntimeEnvelope.model_validate(dict(raw))
    compiled = envelope.model_dump(mode="json", exclude_none=True)
    rag = compiled.get("rag") or {}
    if not rag:
        raise ValueError("candidate RAG runtime overrides must not be empty")
    return {"rag": dict(rag)}


__all__ = [
    "RAGRuntimeOverrides",
    "RUNTIME_COMPILER_VERSION",
    "compile_runtime_overrides",
]
