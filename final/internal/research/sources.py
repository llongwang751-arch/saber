"""Serializable source registry; model-created citations never become evidence."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def canonical_location(value: str) -> str:
    value = str(value or "").strip()
    if value.startswith("doc:") and len(value) > 4:
        return value
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Sources require an http(s) URL or a stable doc: identifier")
    host = parsed.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    port = parsed.port
    if port and not ((port == 80 and parsed.scheme.lower() == "http") or (port == 443 and parsed.scheme.lower() == "https")):
        host += ":" + str(port)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((parsed.scheme.lower(), host, parsed.path.rstrip("/") or "/", urlencode(sorted(query)), ""))


def normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


class SourceLedger:
    """Deduplicate URLs and identical normalized content, preserving first provenance.

    Fingerprints collapse whitespace/case differences, not semantic paraphrases: the
    registry deliberately does not claim to prove two different texts equivalent.
    """

    def __init__(self, state=None, *, max_sources=20, max_source_chars=12000):
        self.max_sources = max_sources
        self.max_source_chars = max_source_chars
        self.sources = []
        self.evidence = []
        self._locations = {}
        self._fingerprints = {}
        self._evidence_keys = set()
        for source in (state or {}).get("sources", []):
            self.add(source, round_number=source.get("round", 0), query=source.get("query", ""))
        for evidence in (state or {}).get("evidence", []):
            self.add_evidence(**{k: evidence[k] for k in ("source_id", "quote", "claim", "round") if k in evidence})

    def add(self, item, *, round_number=0, query=""):
        if not isinstance(item, dict):
            return None, False
        try:
            location = canonical_location(item.get("url_or_doc_id") or item.get("url") or item.get("doc_id"))
        except (TypeError, ValueError):
            return None, False
        content = str(item.get("raw_content") or item.get("content") or item.get("text") or "")[:self.max_source_chars].strip()
        if not content:
            return None, False
        fingerprint = hashlib.sha256(normalized_text(content).casefold().encode("utf-8")).hexdigest()
        existing = self._locations.get(location) or self._fingerprints.get(fingerprint)
        if existing:
            self._locations[location] = existing
            return existing, False
        if len(self.sources) >= self.max_sources:
            return None, False
        source = {
            "source_id": f"S{len(self.sources) + 1}", "url_or_doc_id": location,
            "url": location if not location.startswith("doc:") else "",
            "title": normalized_text(item.get("title") or location)[:300],
            "content": content, "round": int(round_number), "query": str(query)[:1000],
            "content_fingerprint": fingerprint,
            "content_kind": str(item.get("content_kind") or ("full_text" if item.get("raw_content") else "search_excerpt")),
        }
        self.sources.append(source)
        self._locations[location] = self._fingerprints[fingerprint] = source
        return source, True

    def contains(self, location):
        try:
            return canonical_location(location) in self._locations
        except ValueError:
            return False

    def add_evidence(self, source_id, quote, claim="", round=0):
        source = next((s for s in self.sources if s["source_id"] == source_id), None)
        quote = normalized_text(quote)
        if not source or not quote or quote not in normalized_text(source["content"]):
            return False
        key = (source_id, quote)
        if key in self._evidence_keys:
            return False
        self._evidence_keys.add(key)
        self.evidence.append({"source_id": source_id, "quote": quote[:1600],
                              "claim": normalized_text(claim)[:1600], "round": int(round)})
        return True

    def to_dict(self):
        return {"sources": [dict(s) for s in self.sources], "evidence": [dict(e) for e in self.evidence]}
