"""Best-effort secret and personal-data redaction before Trace persistence."""

from __future__ import annotations

import re
from typing import Any


_SENSITIVE_KEYS = {
    "api_key", "apikey", "authorization", "cookie", "password", "passwd",
    "secret", "access_token", "refresh_token", "jwt", "private_key",
}
_PATTERNS = (
    (re.compile(r"(?i)bearer\s+[a-z0-9._~+/-]+=*"), "Bearer [REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[REDACTED_PHONE]"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
    (re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)"), "[REDACTED_ID]"),
)


def redact_text(value: str, *, max_length: int = 4000) -> str:
    text = str(value or "")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    if max_length > 0 and len(text) > max_length:
        text = text[:max_length] + "…[TRUNCATED]"
    return text


def sanitize_trace(value: Any, *, max_depth: int = 10) -> Any:
    """Recursively redact secrets and bound persisted trace size."""

    return _sanitize(value, depth=0, max_depth=max_depth)


def _sanitize(value: Any, *, depth: int, max_depth: int) -> Any:
    if depth > max_depth:
        return "[MAX_DEPTH]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        output = {}
        for raw_key, item in list(value.items())[:200]:
            key = str(raw_key)
            if key.casefold() in _SENSITIVE_KEYS:
                output[key] = "[REDACTED]"
            else:
                output[key] = _sanitize(item, depth=depth + 1, max_depth=max_depth)
        return output
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item, depth=depth + 1, max_depth=max_depth) for item in list(value)[:500]]
    return redact_text(str(value))
