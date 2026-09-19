"""Mode-aware answerability and checkable claim/source/quote references.

Quote checks establish provenance, not semantic entailment. Uncalibrated lexical
scores are a conservative fallback and are exposed separately in the trace.
"""
import json
import re
import math
from .local_reranker import _features, _overlap_score


def select_evidence(question, results, threshold, cfg, diagnostics=None):
    selected = []
    fallback_threshold = float(getattr(cfg, "rag_fallback_min_overlap", 0.08))
    encoder_threshold = float(getattr(cfg, "rag_cross_encoder_threshold", 0.5))
    for item in results:
        item = dict(item)
        source = str(item.get("source", ""))
        score = float(item.get("score") or 0)
        if "+cross_encoder" in source:
            mode, cutoff = "cross_encoder", encoder_threshold
        elif "+local_rerank" in source:
            mode, cutoff = "local_overlap", fallback_threshold
        elif "+rerank" in source or "+remote_rerank" in source:
            mode, cutoff = "remote_rerank", threshold
        else:
            mode, cutoff = "lexical_fallback", fallback_threshold
            score = _overlap_score(question, _features(question), item.get("content", ""))
        item["answerability"] = {"mode": mode, "score": score, "threshold": cutoff,
                                  "calibrated": mode in getattr(cfg, "rag_calibrated_modes", [])}
        accepted = math.isfinite(score) and math.isfinite(cutoff) and score >= cutoff
        if diagnostics is not None:
            diagnostics.append({"pg_id": item.get("pg_id"), **item["answerability"], "accepted": accepted})
        if accepted:
            selected.append(item)
    return selected


def render_claims(raw, evidence, diagnostics=None):
    def fail(reason):
        if diagnostics is not None:
            diagnostics["reason"] = reason
        return None, []
    if not isinstance(raw, str):
        return fail("invalid_claims_json")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            return fail("invalid_claims_schema")
        claims = parsed["claims"]
    except (ValueError, KeyError, TypeError):
        return fail("invalid_claims_json")
    if not isinstance(claims, list):
        return fail("invalid_claims_schema")
    if not claims:
        return fail("model_abstained")
    by_id = {item["evidence_id"]: item for item in evidence}
    checked, rendered = [], []
    for claim in claims:
        if not isinstance(claim, dict):
            return fail("invalid_claims_schema")
        content = str(claim.get("text") or "").strip()
        citations = claim.get("citations")
        if not content or not isinstance(citations, list) or not citations:
            return fail("missing_claim_citations")
        ids = []
        for citation in citations:
            if not isinstance(citation, dict):
                return fail("invalid_citation_schema")
            eid, quote = citation.get("evidence_id"), str(citation.get("quote") or "").strip()
            if not isinstance(eid, str) or eid not in by_id or len(quote) < 4 or quote not in by_id[eid]["content"]:
                return fail("invalid_citation_source_or_quote")
            ids.append(eid)
        checked.append({"text": content, "citations": citations,
                        "validation": "source_and_quote_only"})
        rendered.append(content + " " + " ".join(f"[{eid}]" for eid in dict.fromkeys(ids)))
    if diagnostics is not None:
        diagnostics["reason"] = "validated_claims"
    return "\n\n".join(rendered), checked
