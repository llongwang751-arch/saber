import pytest

from internal.research import SourceLedger
from internal.research.reporting import render_report, validate_citations


def ledger():
    result = SourceLedger()
    result.add({"url": "https://example.org/a", "title": "A", "content": "Alpha evidence."})
    result.add({"url": "https://example.org/b", "title": "B", "content": "Beta evidence."})
    result.add_evidence("S1", "Alpha evidence.")
    result.add_evidence("S2", "Beta evidence.")
    return result


def test_citation_numbers_follow_first_mention_and_cover_all_sources():
    markdown, references = render_report("Topic", ["Beta [S2]."], ledger())
    assert references[0]["source_id"] == "S2"
    assert references[1]["source_id"] == "S1"
    assert "Beta [1](#source-1)" in markdown
    assert "Alpha evidence. [2](#source-2)" in markdown
    assert not validate_citations(markdown.split("## References")[0], references)


def test_missing_and_dangling_references_are_reported():
    refs = [{"number": 1}, {"number": 2}]
    errors = validate_citations("Claim [1] and invented [3].", refs)
    assert "Unknown citations: 3" in errors
    assert "Uncited sources: 2" in errors
    assert validate_citations("Claim [2]", [{"number": 2}])


@pytest.mark.parametrize("body", ["Fake [S99].", "Unmapped [9]."])
def test_renderer_rejects_model_fabricated_citations(body):
    with pytest.raises(ValueError):
        render_report("Topic", [body], ledger())


def test_source_excerpt_cannot_inject_citations_or_html():
    sources = SourceLedger()
    sources.add({"url": "https://example.org", "title": "<script>bad</script>",
                 "content": "Untrusted [900] <img src=x onerror=evil()>."})
    markdown, references = render_report("Topic", [], sources)
    assert "[900]" not in markdown and "<img" not in markdown and "<script>" not in markdown
    assert not validate_citations(markdown.split("## References")[0], references)


def test_untrusted_reference_links_are_rejected():
    sources = SourceLedger()
    source, created = sources.add({"url": "javascript:alert(1)", "content": "malicious"})
    assert source is None and created is False
    source, created = sources.add({"url": "file:///etc/passwd", "content": "private"})
    assert source is None and created is False
