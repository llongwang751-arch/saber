"""Dataset-driven evaluation for memory and Agent harness behaviour.

This suite deliberately uses deterministic stubs.  It measures framework
contracts (state, isolation and failure semantics), not the quality of a live
LLM.  The JSONL remains portable to the regular evaluation service because
every row conforms to ``EvalCase``; component-specific labels live under
``metadata.oracle``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from internal.agent.cancel import CancelToken
from internal.agent.graph_runtime import GraphConfig, GraphRuntime
from internal.agent.memory_writer import extract_memory_from_reply
from internal.evaluation.schemas import EvalCase
from internal.graph.task_graph import Node, NodeStatus, TaskGraph
from internal.memory.memory import Item, LongTerm, MemoryManager
from internal.memory.preference import Preference
from internal.rag.rewriter import HistoryMessage, LLMRewriter


DATASET_PATH = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "evaluation"
    / "memory_harness_eval.jsonl"
)


def _load_cases() -> list[EvalCase]:
    rows: list[EvalCase] = []
    with DATASET_PATH.open("r", encoding="utf-8") as stream:
        for line_number, raw in enumerate(stream, start=1):
            if not raw.strip():
                continue
            try:
                rows.append(EvalCase.model_validate(json.loads(raw)))
            except Exception as exc:  # pragma: no cover - diagnostic guard
                raise AssertionError(f"invalid dataset row {line_number}: {exc}") from exc
    return rows


CASES = _load_cases()


class _Tool:
    def __init__(self, fn: Callable[[dict], str]):
        self.func = fn
        self.description = "synthetic evaluation tool"
        self.params: list[dict[str, Any]] = []


class _HarnessAgent:
    def __init__(self, **config: Any):
        defaults = {
            "max_retries": 1,
            "retry_delay_ms": 0,
            "step_timeout_ms": 0,
            "is_real_llm": lambda: False,
        }
        defaults.update(config)
        self.cfg = SimpleNamespace(**defaults)
        self.snapshots: list[dict] = []
        self.observations: list[Any] = []
        self.tool_calls: list[Any] = []

    def save_snapshot(self, task: dict) -> None:
        self.snapshots.append(dict(task))

    def push_task_mem(self, observation: Any) -> None:
        self.observations.append(observation)

    def record_tool_call(self, trace: Any) -> None:
        self.tool_calls.append(trace)


class _TenantPreferenceRepo:
    def __init__(self):
        self.values: dict[str, dict[str, str]] = {}

    def load(self, user_id: str) -> dict[str, str]:
        return dict(self.values.get(user_id, {}))

    def save(self, user_id: str, key: str, value: str) -> None:
        self.values.setdefault(user_id, {})[key] = value


class _LongTermRepo:
    def __init__(self):
        self.next_id = 1
        self.saved_by_user: dict[str, list[str]] = {}

    def load(self, user_id: str = "default_user") -> list[Any]:
        return []

    def save(self, content: str, _importance: float, _embedding: str, *, user_id: str = "default_user", **_kwargs: Any) -> int:
        self.saved_by_user.setdefault(user_id, []).append(content)
        value = self.next_id
        self.next_id += 1
        return value

    def update_classified(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _MemoryConfig:
    short_term_max_turns = 10
    memory_consolidation_similarity = 0.85
    memory_consolidation_dedup = 0.95
    memory_consolidation_ttl_days = 30
    memory_consolidation_decay_rate = 1.0
    memory_consolidation_min_import = 0.1
    memory_consolidation_trigger = 5


def _oracle(case: EvalCase) -> dict[str, Any]:
    return dict(case.metadata["oracle"])


LOWER_IS_BETTER = {
    "memory_contamination_rate",
    "cross_tenant_leak_rate",
    "stale_fact_exposure_rate",
}


def _evaluate_multi_turn_coreference(case: EvalCase, _monkeypatch: pytest.MonkeyPatch) -> tuple[float, dict]:
    expected = _oracle(case)
    current = case.turns[-1].content
    history = [HistoryMessage(role=turn.role, content=turn.content) for turn in case.turns[:-1]]
    captured: dict[str, str] = {}
    resolved = "上海智慧养殖场的高温预警阈值怎么配置？"

    def generate(system_prompt: str, user_message: str) -> str:
        captured["system"] = system_prompt
        captured["user"] = user_message
        return json.dumps({"queries": [resolved, "上海猪场高温告警参数"]}, ensure_ascii=False)

    queries = LLMRewriter(generate, num_queries=2).rewrite(current, history)
    required = expected["resolved_query_contains"]
    checks = [all(value in queries[0] for value in required)]
    checks.append(expected["history_contains"] in captured["user"])
    checks.append(f"当前问题：{current}" in captured["user"])
    return sum(checks) / len(checks), {"queries": queries, "checks": checks}


def _evaluate_preference_retention(case: EvalCase, _monkeypatch: pytest.MonkeyPatch) -> tuple[float, dict]:
    repo = _TenantPreferenceRepo()
    preference = Preference("u1", SimpleNamespace(repo=SimpleNamespace(preference=repo)))
    preference.extract_and_save(case.turns[0].content)
    expected = _oracle(case)
    context = preference.build_context()
    checks = [
        preference.get(expected["key"]) == expected["value"],
        expected["value"] in context,
        "【用户偏好】" in context,
    ]
    return sum(checks) / len(checks), {"context": context, "checks": checks}


def _evaluate_preference_correction(case: EvalCase, _monkeypatch: pytest.MonkeyPatch) -> tuple[float, dict]:
    repo = _TenantPreferenceRepo()
    preference = Preference("u1", SimpleNamespace(repo=SimpleNamespace(preference=repo)))
    preference.extract_and_save(case.turns[0].content)
    preference.extract_and_save(case.turns[-1].content)
    expected = _oracle(case)
    context = preference.build_context()
    checks = [
        preference.get(expected["key"]) == expected["active_value"],
        expected["active_value"] in context,
        expected["inactive_value"] not in context,
    ]
    return sum(checks) / len(checks), {"context": context, "checks": checks}


def _evaluate_third_party_contamination(case: EvalCase, _monkeypatch: pytest.MonkeyPatch) -> tuple[float, dict]:
    expected = _oracle(case)

    class LLM:
        @staticmethod
        def chat(_messages: list[Any], system_prompt: str = "") -> str:
            return json.dumps(expected["llm_extraction"], ensure_ascii=False)

    class PreferenceSpy:
        def __init__(self):
            self.writes: list[tuple[str, str]] = []

        def set(self, key: str, value: str) -> None:
            self.writes.append((key, value))

    class LongTermSpy:
        def __init__(self):
            self.writes: list[Any] = []
            self._embed_fn = lambda _text: [1.0]

        def store_classified(self, *args: Any) -> bool:
            self.writes.append(args)
            return True

    preference = PreferenceSpy()
    long_term = LongTermSpy()
    agent = SimpleNamespace(
        cfg=SimpleNamespace(is_real_llm=lambda: True),
        llm=LLM(),
        preference=preference,
        ltm=long_term,
        graph_memory=None,
    )
    extract_memory_from_reply(agent, case.turns[-1].content)
    contamination_count = len(preference.writes) + len(long_term.writes)
    expected_count = expected["preference_write_count"] + expected["long_term_write_count"]
    return float(contamination_count > expected_count), {
        "contamination_count": contamination_count,
        "preference_writes": preference.writes,
        "long_term_writes": long_term.writes,
    }


def _evaluate_tenant_isolation(case: EvalCase, _monkeypatch: pytest.MonkeyPatch) -> tuple[float, dict]:
    preference_repo = _TenantPreferenceRepo()
    long_term_repo = _LongTermRepo()
    infra = SimpleNamespace(repo=SimpleNamespace(preference=preference_repo, ltm=long_term_repo))
    alice = Preference("alice", infra)
    bob = Preference("bob", infra)
    expected = _oracle(case)
    alice.set("饲料", expected["alice_value"])
    bob.set("饲料", expected["bob_value"])
    alice_ltm = LongTerm(_MemoryConfig(), infra, user_id="alice")
    bob_ltm = LongTerm(_MemoryConfig(), infra, user_id="bob")
    alice_ltm.add(expected["alice_value"])
    bob_ltm.add(expected["bob_value"])
    leaks = int(expected["bob_value"] in alice.get_all().values())
    leaks += int(expected["alice_value"] in bob.get_all().values())
    leaks += int(expected["bob_value"] in long_term_repo.saved_by_user.get("alice", []))
    leaks += int(expected["alice_value"] in long_term_repo.saved_by_user.get("bob", []))
    return float(leaks > 0), {"leak_count": leaks, "stored": long_term_repo.saved_by_user}


def _evaluate_superseded_fact_filtering(case: EvalCase, _monkeypatch: pytest.MonkeyPatch) -> tuple[float, dict]:
    expected = _oracle(case)
    repo = _LongTermRepo()
    ltm = LongTerm(_MemoryConfig(), SimpleNamespace(repo=SimpleNamespace(ltm=repo)))
    ltm.items = [
        Item(id=31, content=expected["superseded"], importance=0.8, embedding=[1.0, 0.0], category="fact"),
        Item(id=32, content=expected["active"], importance=0.8, embedding=[0.0, 1.0], category="fact"),
    ]
    ltm.mark_superseded([31], 32)
    visible = [item.content for item in ltm.active_items()]
    stale_exposed = expected["superseded"] in visible
    audit_item, found = ltm.find_by_id(31)
    audit_ok = bool(found and audit_item.status == "superseded" and audit_item.superseded_by == 32)
    raw_rate = float(stale_exposed or not audit_ok or expected["active"] not in visible)
    return raw_rate, {"visible": visible, "audit_ok": audit_ok}


def _evaluate_ttl_forgetting(case: EvalCase, _monkeypatch: pytest.MonkeyPatch) -> tuple[float, dict]:
    repo = _LongTermRepo()
    ltm = LongTerm(_MemoryConfig(), SimpleNamespace(repo=SimpleNamespace(ltm=repo)))
    now = time.time()
    ltm.items = [
        Item(id=1, content="young-low", importance=0.05, embedding=[1.0, 0.0, 0.0], created_at=now - 5 * 86400),
        Item(id=2, content="old-high", importance=0.9, embedding=[0.0, 1.0, 0.0], created_at=now - 60 * 86400),
        Item(id=3, content="old-low", importance=0.05, embedding=[0.0, 0.0, 1.0], created_at=now - 60 * 86400),
    ]
    result = ltm.consolidate()
    expected = _oracle(case)
    actual_removed = set(result.delete_from_db)
    actual_retained = {item.id for item in ltm.items}
    precision = len(actual_removed & set(expected["removed_ids"])) / len(actual_removed) if actual_removed else 0.0
    recall = len(actual_removed & set(expected["removed_ids"])) / len(expected["removed_ids"])
    removal_f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    retained_accuracy = float(actual_retained == set(expected["retained_ids"]))
    return (removal_f1 + retained_accuracy) / 2, {
        "removed": sorted(actual_removed), "retained": sorted(actual_retained),
    }


def _evaluate_memory_deduplication(case: EvalCase, _monkeypatch: pytest.MonkeyPatch) -> tuple[float, dict]:
    repo = _LongTermRepo()
    ltm = LongTerm(_MemoryConfig(), SimpleNamespace(repo=SimpleNamespace(ltm=repo)))
    now = time.time()
    ltm.items = [
        Item(id=10, content="用户喜欢咖啡", importance=0.6, embedding=[1.0, 0.0], tags=["coffee"], created_at=now),
        Item(id=11, content="用户偏好咖啡", importance=0.7, embedding=[1.0, 0.0001], tags=["preference"], created_at=now),
    ]
    result = ltm.consolidate()
    expected = _oracle(case)
    survivor = ltm.items[0] if len(ltm.items) == 1 else None
    checks = [
        result.deduped == 1,
        set(result.delete_from_db) == set(expected["removed_ids"]),
        survivor is not None and survivor.id == expected["survivor_id"],
        survivor is not None and set(survivor.tags) == set(expected["merged_tags"]),
    ]
    return sum(checks) / len(checks), {"checks": checks, "deduped": result.deduped}


def _evaluate_graph_neighbor_recall(case: EvalCase, _monkeypatch: pytest.MonkeyPatch) -> tuple[float, dict]:
    expected = _oracle(case)

    class GraphStub:
        @staticmethod
        def find_related(memory_id: int) -> list[int]:
            return [expected["expanded_id"]] if memory_id == expected["seed_id"] else []

    infra = SimpleNamespace(
        repo=SimpleNamespace(ltm=_LongTermRepo(), preference=_TenantPreferenceRepo())
    )
    manager = MemoryManager(_MemoryConfig(), infra, user_id="u1", graph_memory=GraphStub())
    now = time.time()
    manager.long_term.items = [
        Item(id=expected["seed_id"], content="猪舍高温应急", importance=0.9, embedding=[1.0, 0.0], category="fact", created_at=now),
        Item(id=expected["expanded_id"], content="通风设备检修", importance=0.5, embedding=[0.0, 1.0], category="fact", created_at=now),
    ]
    hits = manager.recall("猪舍高温应急方案", top_k=2, query_embedding=[1.0, 0.0], categories=["fact"])
    by_id = {item.id: item for item in hits}
    checks = [
        expected["seed_id"] in by_id,
        expected["expanded_id"] in by_id,
        expected["expanded_id"] in by_id and by_id[expected["expanded_id"]].score == expected["expanded_score"],
    ]
    return sum(checks) / len(checks), {"hit_ids": list(by_id), "checks": checks}


def _evaluate_cancellation(case: EvalCase, _monkeypatch: pytest.MonkeyPatch) -> tuple[float, dict]:
    calls: list[str] = []
    token = CancelToken()
    token.cancel()
    graph = TaskGraph([Node(id="n1", tool_name="should_not_run")])
    result = GraphRuntime(
        graph,
        _HarnessAgent(),
        GraphConfig(max_parallel=1),
        {"should_not_run": _Tool(lambda _params: calls.append("called") or "bad")},
    ).execute(token)
    expected = _oracle(case)
    checks = [
        len(calls) == expected["tool_call_count"],
        graph.nodes["n1"].status.value == expected["node_status"],
        result.interrupted is expected["interrupted"],
    ]
    return sum(checks) / len(checks), {"checks": checks, "calls": calls}


def _evaluate_timeout_enforcement(case: EvalCase, _monkeypatch: pytest.MonkeyPatch) -> tuple[float, dict]:
    expected = _oracle(case)
    graph = TaskGraph([Node(id="n1", tool_name="slow")])
    started = time.perf_counter()
    result = GraphRuntime(
        graph,
        _HarnessAgent(max_retries=1, step_timeout_ms=expected["timeout_ms"]),
        GraphConfig(max_parallel=1),
        {"slow": _Tool(lambda _params: time.sleep(0.08) or "late")},
    ).execute(CancelToken())
    elapsed = time.perf_counter() - started
    error = result.node_results["n1"].error
    checks = [
        graph.nodes["n1"].status.value == expected["node_status"],
        expected["error_contains"] in error,
    ]
    return sum(checks) / len(checks), {"checks": checks, "elapsed": elapsed, "error": error}


def _evaluate_retry_recovery(case: EvalCase, _monkeypatch: pytest.MonkeyPatch) -> tuple[float, dict]:
    expected = _oracle(case)
    attempts = {"count": 0}

    def flaky(_params: dict) -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient")
        return expected["result"]

    graph = TaskGraph([Node(id="n1", tool_name="flaky")])
    result = GraphRuntime(
        graph,
        _HarnessAgent(max_retries=2),
        GraphConfig(max_parallel=1),
        {"flaky": _Tool(flaky)},
    ).execute(CancelToken())
    checks = [
        attempts["count"] == expected["attempts"],
        graph.nodes["n1"].retry_count == expected["retry_count"],
        graph.nodes["n1"].status.value == expected["node_status"],
        result.node_results["n1"].result == expected["result"],
    ]
    return sum(checks) / len(checks), {"checks": checks, "attempts": attempts["count"]}


def _evaluate_dependency_failure(case: EvalCase, _monkeypatch: pytest.MonkeyPatch) -> tuple[float, dict]:
    calls: list[str] = []
    graph = TaskGraph([
        Node(id="n1", tool_name="fail"),
        Node(id="n2", tool_name="required", depends_on=["n1"]),
        Node(id="n3", tool_name="optional", depends_on=["n1"], optional_depends_on=["n1"]),
    ])
    GraphRuntime(
        graph,
        _HarnessAgent(),
        GraphConfig(max_parallel=2),
        {
            "fail": _Tool(lambda _params: (_ for _ in ()).throw(RuntimeError("upstream failed"))),
            "required": _Tool(lambda _params: calls.append("required") or "bad"),
            "optional": _Tool(lambda _params: calls.append("optional") or "degraded"),
        },
    ).execute(CancelToken())
    expected = _oracle(case)
    checks = [
        graph.nodes[expected["failed"]].status == NodeStatus.FAILED,
        graph.nodes["n2"].status.value == expected["required_downstream"],
        graph.nodes["n3"].status.value == expected["optional_downstream"],
        calls == ["optional"],
    ]
    return sum(checks) / len(checks), {"checks": checks, "calls": calls}


def _evaluate_dynamic_replan(case: EvalCase, monkeypatch: pytest.MonkeyPatch) -> tuple[float, dict]:
    expected = _oracle(case)
    events: list[dict] = []

    def fake_replan(*_args: Any, **_kwargs: Any) -> list[Node]:
        return [Node(id=expected["added_node"], tool_name="second", depends_on=["n1"], name="补充检查")]

    monkeypatch.setattr("internal.agent.planner.llm_replan", fake_replan)
    graph = TaskGraph([Node(id="n1", tool_name="first")])
    runtime = GraphRuntime(
        graph,
        _HarnessAgent(),
        GraphConfig(max_parallel=1, replan_enabled=True, max_replan=1),
        {"first": _Tool(lambda _params: "first"), "second": _Tool(lambda _params: "second")},
        {"query": case.turns[-1].content},
        on_event=events.append,
    )
    runtime.set_replan_context(case.turns[-1].content, "", False)
    result = runtime.execute(CancelToken())
    replan_events = [event for event in events if event["type"] == "replan"]
    checks = [
        graph.nodes[expected["added_node"]].status == NodeStatus.DONE,
        result.observations == expected["observations"],
        len(replan_events) == expected["replan_event_count"],
    ]
    return sum(checks) / len(checks), {"checks": checks, "events": replan_events}


EVALUATORS: dict[str, Callable[[EvalCase, pytest.MonkeyPatch], tuple[float, dict]]] = {
    "multi_turn_coreference": _evaluate_multi_turn_coreference,
    "preference_retention": _evaluate_preference_retention,
    "preference_correction": _evaluate_preference_correction,
    "third_party_contamination": _evaluate_third_party_contamination,
    "tenant_isolation": _evaluate_tenant_isolation,
    "superseded_fact_filtering": _evaluate_superseded_fact_filtering,
    "ttl_forgetting": _evaluate_ttl_forgetting,
    "memory_deduplication": _evaluate_memory_deduplication,
    "graph_neighbor_recall": _evaluate_graph_neighbor_recall,
    "cancellation": _evaluate_cancellation,
    "timeout_enforcement": _evaluate_timeout_enforcement,
    "retry_recovery": _evaluate_retry_recovery,
    "dependency_failure_containment": _evaluate_dependency_failure,
    "dynamic_replan": _evaluate_dynamic_replan,
}


def test_dataset_is_valid_versionable_and_covers_release_gate_dimensions() -> None:
    assert len(CASES) == 14
    assert len({case.case_id for case in CASES}) == len(CASES)
    assert {case.metadata["component"] for case in CASES} == {"memory", "harness"}
    assert {case.metadata["capability"] for case in CASES} == set(EVALUATORS)
    for case in CASES:
        assert case.turns
        assert case.risk_tags
        assert case.metadata["metric"]
        assert 0.0 <= float(case.metadata["threshold"]) <= 1.0
        assert case.metadata["oracle"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.case_id)
def test_memory_and_harness_release_gate(case: EvalCase, monkeypatch: pytest.MonkeyPatch) -> None:
    evaluator = EVALUATORS[case.metadata["capability"]]
    value, details = evaluator(case, monkeypatch)
    metric = case.metadata["metric"]
    threshold = float(case.metadata["threshold"])
    passed = value <= threshold if metric in LOWER_IS_BETTER else value >= threshold
    assert passed, (
        f"{case.case_id} metric={metric} value={value:.4f} "
        f"threshold={threshold:.4f} details={details}"
    )
