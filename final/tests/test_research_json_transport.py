"""Offline transport regression: serialize real requests without provider charges."""

import json
from types import SimpleNamespace

import pytest
from requests import Response
from requests.adapters import HTTPAdapter
from requests.sessions import Session

from internal.research import ResearchEngine
from internal.research.providers import StrictLLM


PLAN = {"objective": "Summarize Alpha requirements", "steps": [
    {"id": "research", "kind": "research", "title": "Read requirements", "depends_on": []},
    {"id": "report", "kind": "write", "title": "Summarize findings", "depends_on": ["research"]},
]}
SOURCE = {"url": "https://example.org/alpha", "title": "Alpha specification",
          "raw_content": "Alpha requires Python 3.11."}


def transport_llm(monkeypatch, outputs, *, url="https://api.deepseek.com/chat/completions", json_mode=None):
    """Intercept PreparedRequest after requests has serialized the actual body."""
    captured, responses = [], iter(outputs)

    class OfflineAdapter(HTTPAdapter):
        def send(self, request, **kwargs):
            captured.append(json.loads(request.body))
            value = next(responses)
            content = json.dumps(value) if isinstance(value, dict) else value
            response = Response()
            response.status_code = 200
            response.encoding = "utf-8"
            response._content = json.dumps({
                "choices": [{"message": {"content": content}}],
                "usage": {"completion_tokens": 32},
            }).encode("utf-8")
            return response

    def session():
        client = Session()
        client.trust_env = False
        client.mount("https://", OfflineAdapter())
        return client

    monkeypatch.setattr("internal.research.providers.requests.Session", session)
    cfg = SimpleNamespace(is_real_llm=lambda: True, llm_api_url=url,
                          llm_api_key="synthetic-test-key", llm_model="deepseek-flash", temperature=0)
    return StrictLLM(SimpleNamespace(cfg=cfg), json_mode=json_mode), captured


def test_engine_serializes_json_mode_for_structured_stages_only(monkeypatch):
    llm, payloads = transport_llm(monkeypatch, [
        PLAN,
        {"queries": ["Alpha requirements"]},
        {"findings": [{"source_id": "S1", "quote": SOURCE["raw_content"], "claim": SOURCE["raw_content"]}],
         "gaps": [], "queries": [], "sufficient": True},
        {"sections": ["Requirements"]},
        "Alpha requires Python 3.11 [S1].",
    ])
    engine = ResearchEngine(llm, search=lambda *args, **kwargs: [SOURCE])
    result = engine.execute(engine.plan("Summarize Alpha requirements"))
    assert result.status == "completed"
    assert len(payloads) == 5
    assert all(payload["response_format"] == {"type": "json_object"} for payload in payloads[:-1])
    assert all("JSON" in payload["messages"][0]["content"] for payload in payloads[:-1])
    assert "response_format" not in payloads[-1]
    assert "Markdown" in payloads[-1]["messages"][0]["content"]
    assert "Python 3.11" in result.report_markdown and len(result.references) == 1


@pytest.mark.parametrize(("url", "json_mode", "expected"), [
    ("https://compatible.example/chat/completions", None, False),
    ("https://compatible.example/chat/completions", True, True),
    ("https://api.deepseek.com/v1/chat/completions", False, False),
])
def test_unknown_provider_json_mode_requires_explicit_opt_in(monkeypatch, url, json_mode, expected):
    llm, payloads = transport_llm(monkeypatch, [{"queries": ["alpha"]}], url=url, json_mode=json_mode)
    assert json.loads(llm.chat_json_limited([], system_prompt='Return JSON {"queries": ["..."]}.')) == {"queries": ["alpha"]}
    assert ("response_format" in payloads[0]) is expected


@pytest.mark.parametrize("client_kind", ["transport", "legacy_limited"])
def test_invalid_json_is_not_repaired_and_legacy_limited_signature_is_preserved(monkeypatch, client_kind):
    calls = []
    if client_kind == "transport":
        llm, calls = transport_llm(monkeypatch, ['{"queries": ["alpha"]'])
    else:
        class LegacyLimited:
            last_completion_tokens = 5

            def chat_limited(self, messages, system_prompt="", *, max_tokens=6000, token=None, timeout=60):
                calls.append(system_prompt)
                return '{"queries": ["alpha"]'

        llm = LegacyLimited()
    result = ResearchEngine(llm, search=lambda *args, **kwargs: pytest.fail("invalid JSON must not trigger search")).execute(PLAN)
    assert len(calls) == 1
    assert result.status == "failed" and result.to_dict()["success"] is False
    assert result.sources == [] and result.usage["tool_calls"] == 0
    assert any("JSONDecodeError" in limitation for limitation in result.limitations)
