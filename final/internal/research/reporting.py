"""Evidence-only report rendering with deterministic citation validation."""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone


def validate_citations(body, references):
    """Validate the body (excluding reference table) against numbered entries."""
    numbers = [int(r["number"]) for r in references]
    cited = {int(n) for n in re.findall(r"\[(\d+)\](?!:)", body)}
    expected = set(numbers)
    errors = []
    if numbers != list(range(1, len(numbers) + 1)):
        errors.append("Reference numbers must be unique and contiguous")
    if cited - expected:
        errors.append("Unknown citations: " + ", ".join(map(str, sorted(cited - expected))))
    if expected - cited:
        errors.append("Uncited sources: " + ", ".join(map(str, sorted(expected - cited))))
    return errors


def _quote(value):
    return html.escape(value).replace("[", "&#91;").replace("]", "&#93;")


def render_report(objective, sections, ledger, *, run_id="", plan=None, limitations=None):
    """Sections use stable [S1] IDs; numbering is assigned only at rendering time."""
    known = {s["source_id"]: s for s in ledger.sources}
    used = []
    body = "# " + _quote(objective.replace("\n", " ")) + "\n\n" + "\n\n".join(sections)
    mentioned = re.findall(r"\[(S\d+)\]", body)
    unknown = set(mentioned) - set(known)
    if unknown or re.search(r"\[\d+\]", body):
        raise ValueError("Report contains unknown or pre-numbered citations")
    for source_id in mentioned:
        if source_id not in used:
            used.append(source_id)
    supplement = []
    for source in ledger.sources:
        source_id = source["source_id"]
        if source_id not in used:
            quote = next((e["quote"] for e in ledger.evidence if e["source_id"] == source_id), source["content"][:500])
            supplement.append(f"> {_quote(quote)} [{source_id}]")
            used.append(source_id)
    if supplement:
        body += "\n\n## 补充来源摘录\n\n" + "\n\n".join(supplement)
    references = []
    for number, source_id in enumerate(used, 1):
        source = known[source_id]
        references.append({"number": number, "source_id": source_id, "url": source["url"],
                           "url_or_doc_id": source["url_or_doc_id"], "title": source["title"],
                           "quote": next((e["quote"] for e in ledger.evidence if e["source_id"] == source_id), source["content"][:500])})
    numbers = {r["source_id"]: r["number"] for r in references}
    body = re.sub(r"\[(S\d+)\](?:\([^)]*\))?", lambda m: f"[{numbers[m[1]]}](#source-{numbers[m[1]]})", body)
    if limitations:
        body += "\n\n## 研究限制\n\n" + "\n".join("- " + _quote(str(item)) for item in dict.fromkeys(limitations))
    errors = validate_citations(body, references)
    if errors:
        raise ValueError("; ".join(errors))
    frontmatter = {"topic": objective, "run_id": run_id, "generated_at": datetime.now(timezone.utc).isoformat(),
                   "plan": [s.get("title", "") for s in (plan or {}).get("steps", [])],
                   "source_count": len(references)}
    metadata = "---\n" + "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in frontmatter.items()) + "\n---\n\n"
    rows = []
    for ref in references:
        target = ref["url"].replace("(", "%28").replace(")", "%29") if ref["url"] else ""
        title = _quote(ref["title"])
        label = f"[{title}]({target})" if target else title + " — " + _quote(ref["url_or_doc_id"])
        rows.append(f'<a id="source-{ref["number"]}"></a> [{ref["number"]}] {label}')
    markdown = metadata + body + ("\n\n## References\n\n" + "\n\n".join(rows) if rows else "")
    return markdown, references
