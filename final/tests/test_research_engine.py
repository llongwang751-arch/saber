import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from internal.agent.cancel import CancelToken
from internal.research import ResearchEngine, ResearchLimits, SourceLedger
from internal.research.coder import execute_python
from internal.research.providers import ProviderUnavailable, RagProvider, StrictLLM
from internal.sandbox.types import ExecResult, SandboxConfig


PLAN = {"objective": "Compare Alpha deployment constraints", "steps": [
    {"id": "research", "title": "Find requirements", "kind": "research", "guidance": "Find deployment requirements",
     "depends_on": []},
    {"id": "report", "title": "Write evidence report", "kind": "write", "depends_on": ["research"]},
]}
ONE = {"url": "https://example.org/alpha?utm_source=demo", "title": "Alpha specification",
       "raw_content": "Alpha requires Python 3.11. Alpha runs on Linux."}
TWO = {"url": "https://example.org/security", "title": "Security specification",
       "raw_content": "Production execution requires an isolated Docker sandbox."}


class ScriptedLLM:
    """An explicit fixture: no mock generation is wired into production providers."""
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def chat(self, messages, system_prompt=""):
        self.calls.append((system_prompt, json.loads(messages[0].content)))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return json.dumps(response) if isinstance(response, dict) else response


def assessment(source="S1", quote="Alpha requires Python 3.11.", sufficient=True, queries=None):
    return {"findings": [{"source_id": source, "quote": quote, "claim": quote}],
            "gaps": [] if sufficient else ["Need isolation evidence"], "queries": queries or [], "sufficient": sufficient}


def test_research_iterates_reads_deduplicates_and_renders_real_sources():
    llm = ScriptedLLM([
        {"queries": ["Alpha requirements"]},
        assessment(sufficient=False, queries=["Alpha security"]),
        assessment("S2", TWO["raw_content"]),
        {"sections": ["Requirements"]},
        "Python 3.11 is required [S1]. Docker isolation is required [S2].",
    ])
    queries, reads, events, checkpoints = [], [], [], []

    def search(query, *, token=None):
        queries.append(query)
        if "security" in query:
            return [{**ONE, "url": "https://example.org/alpha#duplicate"}, TWO]
        return [{k: v for k, v in ONE.items() if k != "raw_content"}]

    def read(url, *, token=None):
        reads.append(url)
        return ONE["raw_content"]

    result = ResearchEngine(llm, search=search, reader=read).execute(
        PLAN, run_id="run-1", on_event=lambda name, data: events.append((name, data)),
        checkpoint=checkpoints.append,
    )
    assert result.status == "completed"
    assert len(queries) == 2 and len(reads) == 1
    assert len(result.sources) == 2 and len(result.evidence) == 2
    assert result.sources[0]["url"] == "https://example.org/alpha"
    assert "[1](#source-1)" in result.report_markdown and "[2](#source-2)" in result.report_markdown
    assert 'run_id: "run-1"' in result.report_markdown
    assert [r["number"] for r in result.references] == [1, 2]
    assert sum(name == "source_found" for name, _ in events) == 2
    assert checkpoints[-1]["artifacts"][0]["content"] == result.report_markdown
    assert "不可信数据" in llm.calls[1][0]
    assert llm.calls[1][1]["retrieved_material"][0]["content"] == ONE["raw_content"]


def test_budget_exhaustion_preserves_retrieved_evidence():
    engine = ResearchEngine(ScriptedLLM([{"queries": ["requirements"]}]),
                            search=lambda *a, **kw: [ONE], limits=ResearchLimits(max_llm_calls=1))
    result = engine.execute(PLAN)
    assert result.status == "partial"
    assert result.usage["llm_calls"] == 1
    assert len(result.sources) == 1
    assert "Alpha requires Python 3.11." in result.report_markdown
    assert any("预算耗尽" in item for item in result.limitations)


def test_quote_validation_rejects_invented_source_and_fabricated_excerpt():
    ledger = SourceLedger()
    ledger.add(ONE)
    assert not ledger.add_evidence("S404", "fake")
    assert not ledger.add_evidence("S1", "Alpha guarantees 100% availability")
    assert ledger.add_evidence("S1", "Alpha requires Python 3.11.", "Requires Python")
    assert not ledger.add_evidence("S1", "Alpha requires Python 3.11.", "same observation")
    ledger.add({**ONE, "url": "https://mirror.example/a", "raw_content": ONE["raw_content"].upper()})
    assert len(ledger.sources) == 1
    restored = SourceLedger(ledger.to_dict())
    assert restored.to_dict() == ledger.to_dict()


def test_reader_failure_retains_labelled_search_excerpt():
    llm = ScriptedLLM([{"queries": ["requirements"]}, assessment(), {"sections": ["Requirements"]}, "Python [S1]."])

    def failed_read(*a, **kw):
        raise ConnectionError("unavailable")

    result = ResearchEngine(llm, search=lambda *a, **kw: [{**ONE, "raw_content": "", "content": ONE["raw_content"]}],
                            reader=failed_read).execute(PLAN)
    assert result.status == "partial"
    assert result.sources[0]["content_kind"] == "search_excerpt"
    assert any("读取失败" in text for text in result.limitations)


def test_missing_provider_never_becomes_successful_mock_research():
    def unavailable(*a, **kw):
        raise ProviderUnavailable("Search key missing")

    result = ResearchEngine(ScriptedLLM([{"queries": ["requirements"]}]), search=unavailable).execute(PLAN)
    assert result.status == "failed"
    assert result.to_dict()["success"] is False
    assert result.sources == [] and "Search key missing" in result.report_markdown


def test_cancellation_before_work_never_calls_model_or_tools():
    token = CancelToken()
    token.cancel()
    llm = ScriptedLLM([])
    result = ResearchEngine(llm, search=lambda *a, **kw: pytest.fail("must not search")).execute(PLAN, cancel_token=token)
    assert result.status == "cancelled" and not llm.calls


def test_plan_validation_prevents_tool_dispatch_for_invalid_dependency():
    with pytest.raises(ValueError):
        ResearchEngine(ScriptedLLM([]), search=lambda *a, **kw: pytest.fail("must not search")).execute(
            {"objective": "test", "steps": [{"id": "a", "kind": "research", "title": "test", "depends_on": ["missing"]}]}
        )


@pytest.mark.parametrize("backend", ["local", "mock"])
def test_coder_never_executes_on_unisolated_backend(backend):
    sandbox = SimpleNamespace(backend=lambda: backend, exec=lambda *a: pytest.fail("unsafe execution"))
    result = execute_python("print(1)", {}, sandbox)
    assert result["status"] == "code_only" and result["stdout"] == ""


def test_coder_passes_only_temporary_explicit_inputs_to_docker():
    mounted = []

    def run(token, request):
        mounted.append(request.workspace_host_dir)
        assert request.command == "python3 -I /workspace/analysis.py"
        assert request.confirm is False
        assert Path(request.workspace_host_dir, "analysis.py").read_text(encoding="utf-8") == "print(42)"
        assert json.loads(Path(request.workspace_host_dir, "evidence.json").read_text()) == {"sources": []}
        return ExecResult(stdout="42", backend="docker", exit_code=0)

    sandbox = SimpleNamespace(backend=lambda: "docker", cfg=SandboxConfig(), exec=run)
    result = execute_python("print(42)", {"sources": []}, sandbox)
    assert result["status"] == "executed" and result["stdout"] == "42"
    assert not Path(mounted[0]).exists()


def test_code_dispatch_of_unknown_outcome_is_not_replayed():
    state = {"steps": {"code": {"id": "code", "kind": "code", "code": "print(1)", "dispatch_state": "dispatching"}}}
    plan = {"objective": "analyze", "steps": [{"id": "code", "title": "Analysis", "kind": "code"}]}
    result = ResearchEngine(ScriptedLLM([]), search=None).execute(plan, state=state)
    assert result.steps[0]["status"] == "code_only"
    assert any("禁止自动重复执行" in item for item in result.limitations)


def test_coder_failure_does_not_claim_generated_code_was_executed():
    plan = {"objective": "analyze", "steps": [{"id": "code", "title": "Analysis", "kind": "code"}]}
    llm = ScriptedLLM([{"code": "print(42)"}])
    result = ResearchEngine(llm, search=None).execute(plan)
    assert result.steps[0]["status"] == "code_only"
    assert result.artifacts[1]["name"] == "code.py"
    assert "仅生成代码" in result.report_markdown


def test_write_resume_skips_already_saved_sections():
    ledger = SourceLedger()
    ledger.add(ONE)
    ledger.add_evidence("S1", "Alpha requires Python 3.11.")
    state = {**ledger.to_dict(), "sections": ["## First\n\nPython [S1]."],
             "steps": {"research": {"finished": True, "status": "completed"},
                       "report": {"outline": ["First", "Second"], "next_section": 1}}}
    llm = ScriptedLLM(["Linux [S1]."])
    result = ResearchEngine(llm, search=lambda *a, **kw: pytest.fail("search replay")).execute(PLAN, state=state)
    assert len(llm.calls) == 1
    assert result.report_markdown.count("## First") == 1
    assert result.report_markdown.count("## Second") == 1


def test_manifest_and_plan_policy_are_enforced():
    result = ResearchEngine(ScriptedLLM([]), search=lambda *a, **kw: pytest.fail("disabled"),
                            allowed_tools={"rag_search"}).execute(PLAN)
    assert result.status == "failed" and result.usage["tool_calls"] == 0
    plan = copy.deepcopy(PLAN)
    plan["steps"][0]["tool_policy"] = ["rag_search"]
    result = ResearchEngine(ScriptedLLM([]), search=lambda *a, **kw: pytest.fail("denied")).execute(plan)
    assert result.usage["tool_calls"] == 0


def test_rag_provider_uses_retrieval_without_synthesis_and_preserves_document_id():
    queries = []
    hybrid = SimpleNamespace(search=lambda query, limit, trace: queries.append(query) or [
        SimpleNamespace(document_id="doc1", pg_id=10, content="child", parent="Original passage", source="keyword")
    ])
    rag = SimpleNamespace(loaded=True, _hybrid=hybrid, cfg=SimpleNamespace(top_k=5),
                          query=lambda *a: pytest.fail("answer generation must not run"))
    source = RagProvider(SimpleNamespace(rag=rag)).search("topic")[0]
    assert source["url_or_doc_id"] == "doc:doc1:10"
    assert source["content"] == "Original passage"


def test_strict_llm_refuses_unconfigured_mock_profile():
    client = SimpleNamespace(cfg=SimpleNamespace(is_real_llm=lambda: False),
                             chat=lambda *a: pytest.fail("mock must not run"))
    with pytest.raises(ProviderUnavailable, match="真实 LLM"):
        StrictLLM(client).chat([])


def test_strict_llm_applies_transport_budget_and_usage(monkeypatch):
    captured = {}

    class Session:
        def post(self, url, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(status_code=200, json=lambda: {
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"completion_tokens": 2, "prompt_tokens": 5, "total_tokens": 7},
            })

        def close(self):
            pass

    monkeypatch.setattr("internal.research.providers.requests.Session", Session)
    cfg = SimpleNamespace(is_real_llm=lambda: True, llm_api_url="https://llm.test", llm_api_key="fixture-key",
                          llm_model="fixture", temperature=0)
    llm = StrictLLM(SimpleNamespace(cfg=cfg))
    assert llm.chat_limited([], max_tokens=7, timeout=2) == "answer"
    assert captured["json"]["max_tokens"] == 7
    assert max(captured["timeout"]) <= 2
    assert llm.last_completion_tokens == 2


def test_complete_serialized_model_context_is_bounded():
    llm = ScriptedLLM([{"sections": ["Findings"]}, "Alpha [S1]."])
    ledger = SourceLedger(max_sources=20)
    for i in range(20):
        text = f"Evidence {i}. " + "x" * 11000
        ledger.add({"url": f"https://example.org/{i}", "content": text})
        ledger.add_evidence(f"S{i + 1}", text[:1500])
    state = {**ledger.to_dict(), "steps": {"research": {"finished": True, "status": "completed"}}}
    result = ResearchEngine(llm, search=None, limits=ResearchLimits(max_context_chars=2000)).execute(PLAN, state=state)
    assert result.sources
    assert all(len(json.dumps(payload, ensure_ascii=False)) <= 2000 for _, payload in llm.calls)


def test_budget_is_checkpointed_before_provider_dispatch():
    snapshots = []
    llm = ScriptedLLM([{"queries": ["Alpha requirements"]}])

    def search(query, *, token=None):
        assert snapshots[-1]["usage"]["tool_calls"] == 1
        assert snapshots[-1]["usage"]["llm_calls"] == 1
        assert snapshots[-1]["usage"]["output_tokens"] > 0
        assert snapshots[-1]["inflight_timeout_seconds"] > 0
        return [ONE]

    result = ResearchEngine(llm, search=search, limits=ResearchLimits(max_llm_calls=1)).execute(
        PLAN, checkpoint=snapshots.append)
    assert result.status == "partial"


def test_resume_does_not_reset_elapsed_time_budget():
    state = {"usage": {"llm_calls": 0, "tool_calls": 0, "rounds": 0, "output_chars": 0,
                       "elapsed_seconds": 290},
             "inflight_timeout_seconds": 20}
    result = ResearchEngine(ScriptedLLM([]), search=lambda *a, **kw: pytest.fail("expired budget")).execute(
        PLAN, state=state)
    assert result.usage["elapsed_seconds"] >= 310
    assert any("时间预算" in text for text in result.limitations)


def test_unknown_provider_exception_does_not_leak_credentials():
    def search(*a, **kw):
        raise RuntimeError("https://user:password@example.org?api_key=TOP_SECRET request failed")

    result = ResearchEngine(ScriptedLLM([{"queries": ["topic"]}]), search=search).execute(PLAN)
    assert "TOP_SECRET" not in result.report_markdown
    assert "password" not in result.report_markdown


def test_successful_code_only_plan_reports_actual_output():
    sandbox = SimpleNamespace(backend=lambda: "docker", cfg=SandboxConfig(),
                              exec=lambda *a: ExecResult(stdout="computed=42", backend="docker", exit_code=0))
    plan = {"objective": "Compute", "steps": [{"id": "code", "title": "Compute", "kind": "code"}]}
    result = ResearchEngine(ScriptedLLM([{"code": "print('computed=42')"}]), search=None, sandbox=sandbox).execute(plan)
    assert result.status == "completed"
    assert "computed=42" in result.report_markdown
    assert "隔离代码执行结果" in result.report_markdown
