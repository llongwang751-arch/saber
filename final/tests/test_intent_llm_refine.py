"""可选 LLM 意图复核（refine_intent_with_llm）的行为测试。

覆盖两级漏斗语义：开关默认关闭、关键词命中短路不调 LLM、
复核命中提升路由、超时/异常/非法输出一律回退关键词结果。
"""
import time
from types import SimpleNamespace

import pytest

from internal.agent.agent import UnifiedAgent
from internal.agent.planner import refine_intent_with_llm
import internal.agent.planner as planner_mod

_MISSED_REPORT_QUERY = "把上个月的销售数据整理成一份能汇报的东西"


class FakeLLM:
    def __init__(self, reply: str = "", delay: float = 0.0, exc: Exception | None = None):
        self.reply = reply
        self.delay = delay
        self.exc = exc
        self.calls = 0

    def chat_fast(self, messages, system_prompt=""):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return self.reply


def make_agent(llm) -> SimpleNamespace:
    return SimpleNamespace(llm=llm)


@pytest.fixture()
def enable_intent_llm(monkeypatch):
    monkeypatch.setenv("AGI_INTENT_LLM_ENABLED", "1")


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AGI_INTENT_LLM_ENABLED", raising=False)
    llm = FakeLLM(reply='{"needs_report": true}')
    assert UnifiedAgent._report_intent_refined(make_agent(llm), _MISSED_REPORT_QUERY) is False
    assert llm.calls == 0


def test_keyword_hit_short_circuits_before_llm(enable_intent_llm):
    llm = FakeLLM(reply='{"needs_report": true}')
    calls = {"refined": 0}

    def refined(_query: str) -> bool:
        calls["refined"] += 1
        return True

    fake = SimpleNamespace(
        rag=SimpleNamespace(loaded=True),
        tool_executor=None,
        _report_intent=lambda query: True,
        _report_intent_refined=refined,
    )
    opts = SimpleNamespace(use_rag=True)
    mode, route_tools = UnifiedAgent._route_decide(fake, "写一份调研报告", opts)
    assert mode == "rag_agent"
    assert calls["refined"] == 0
    assert llm.calls == 0


def test_llm_positive_rescues_missed_report(enable_intent_llm):
    llm = FakeLLM(reply='{"needs_report": true}')
    assert refine_intent_with_llm(make_agent(llm), _MISSED_REPORT_QUERY) is True
    assert llm.calls == 1


def test_llm_negative_keeps_rag_route(enable_intent_llm):
    llm = FakeLLM(reply='{"needs_report": false}')
    fake = SimpleNamespace(
        rag=SimpleNamespace(loaded=True),
        tool_executor=None,
        llm=llm,
        _report_intent=lambda query: False,
    )
    fake._report_intent_refined = lambda query: UnifiedAgent._report_intent_refined(fake, query)
    mode, _ = UnifiedAgent._route_decide(fake, _MISSED_REPORT_QUERY, SimpleNamespace(use_rag=True))
    assert mode == "rag"


def test_timeout_falls_back_to_not_report(enable_intent_llm, monkeypatch):
    monkeypatch.setattr(planner_mod, "_INTENT_LLM_TIMEOUT_SECONDS", 0.05)
    llm = FakeLLM(reply='{"needs_report": true}', delay=0.4)
    assert refine_intent_with_llm(make_agent(llm), _MISSED_REPORT_QUERY) is False


def test_llm_error_falls_back_to_not_report(enable_intent_llm):
    llm = FakeLLM(exc=RuntimeError("provider down"))
    assert refine_intent_with_llm(make_agent(llm), _MISSED_REPORT_QUERY) is False


def test_garbage_output_falls_back_to_not_report(enable_intent_llm):
    llm = FakeLLM(reply="我觉得大概是需要的吧")
    assert refine_intent_with_llm(make_agent(llm), _MISSED_REPORT_QUERY) is False


def test_fenced_json_output_is_accepted(enable_intent_llm):
    llm = FakeLLM(reply='```json\n{"needs_report": true}\n```')
    assert refine_intent_with_llm(make_agent(llm), _MISSED_REPORT_QUERY) is True


def test_short_query_skips_llm(enable_intent_llm):
    llm = FakeLLM(reply='{"needs_report": true}')
    assert refine_intent_with_llm(make_agent(llm), "你好") is False
    assert llm.calls == 0
