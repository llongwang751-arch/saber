import json

from internal.rag.circuit_breaker import CLOSED, HALF_OPEN, OPEN, CircuitBreaker
from internal.rag.reranker import LLMReranker


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _Result:
    def __init__(self, content, score):
        self.content = content
        self.score = score
        self.source = "hybrid"


def test_circuit_breaker_closed_open_half_open_and_recovery():
    clock = _Clock()
    circuit = CircuitBreaker(2, 10, 1, clock=clock)

    assert circuit.allow_request() is True
    circuit.record_failure()
    assert circuit.snapshot().state == CLOSED
    assert circuit.allow_request() is True
    circuit.record_failure()
    assert circuit.snapshot().state == OPEN
    assert circuit.allow_request() is False

    clock.advance(10)
    assert circuit.allow_request() is True
    assert circuit.snapshot().state == HALF_OPEN
    assert circuit.allow_request() is False
    circuit.record_success()
    assert circuit.snapshot().state == CLOSED


def test_half_open_probe_failure_reopens_circuit():
    clock = _Clock()
    circuit = CircuitBreaker(1, 5, 1, clock=clock)
    assert circuit.allow_request() is True
    circuit.record_failure()
    clock.advance(5)
    assert circuit.allow_request() is True
    circuit.record_failure()

    snapshot = circuit.snapshot()
    assert snapshot.state == OPEN
    assert snapshot.retry_after_seconds == 5


def test_reranker_skips_remote_calls_while_open_and_recovers_on_probe():
    clock = _Clock()
    circuit = CircuitBreaker(2, 10, 1, clock=clock)
    calls = []
    should_fail = {"value": True}

    def generate(_system, _user):
        calls.append("called")
        if should_fail["value"]:
            raise TimeoutError("remote timeout")
        return json.dumps({"scores": [{"idx": 0, "score": 2}, {"idx": 1, "score": 9}]})

    reranker = LLMReranker(generate, circuit_breaker=circuit)
    make_results = lambda: [_Result("first", 0.7), _Result("second", 0.6)]

    assert reranker.rerank("q", make_results(), 2)[0].content == "first"
    assert reranker.rerank("q", make_results(), 2)[0].content == "first"
    assert reranker.circuit_snapshot()["state"] == OPEN
    assert reranker.rerank("q", make_results(), 2)[0].content == "first"
    assert len(calls) == 2

    clock.advance(10)
    should_fail["value"] = False
    recovered = reranker.rerank("q", make_results(), 2)
    assert [result.content for result in recovered] == ["second", "first"]
    assert reranker.circuit_snapshot()["state"] == CLOSED
    assert len(calls) == 3
