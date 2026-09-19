"""Adapters that turn an Agent invocation into a stable evaluation contract.

The evaluator never reaches into an Agent's private state.  Every target is
adapted to :class:`AgentOutput`, so the same dataset can exercise the local
AGI-saber runtime, an HTTP service, or deterministic replay fixtures used in
CI and interview demos.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import json
import time
from typing import Any, Mapping

import requests

from internal.agent.agent import ChatOptions

from .schemas import AgentOutput, EvalCase, ToolCall, TraceEvent, TraceEventType


class AgentAdapter(ABC):
    """Synchronous execution boundary used by the offline runner."""

    name: str
    version: str

    @abstractmethod
    def execute(self, case: EvalCase) -> AgentOutput:
        raise NotImplementedError


class ReplayAgentAdapter(AgentAdapter):
    """Replay labelled fixture outputs without calling a model.

    A case may contain ``metadata.outputs.<profile>``.  This makes a complete
    evaluation run deterministic and allows a baseline/fixed regression demo
    to run without API keys.  The data is synthetic and must not be presented
    as a live model result.
    """

    name = "fixture-replay"

    def __init__(self, profile: str = "baseline"):
        self.profile = profile.strip() or "baseline"
        self.version = self.profile

    def execute(self, case: EvalCase) -> AgentOutput:
        started = time.perf_counter()
        profiles = case.metadata.get("outputs", {})
        payload = profiles.get(self.profile) if isinstance(profiles, Mapping) else None
        if payload is None:
            raise ValueError(
                f"case {case.case_id!r} has no replay output for profile {self.profile!r}"
            )
        output = AgentOutput.model_validate(payload)
        return _ensure_final_trace(output, elapsed_ms=(time.perf_counter() - started) * 1000)


class LocalAGISaberAdapter(AgentAdapter):
    """Run cases against the in-process :class:`UnifiedAgent` instance."""

    name = "agi-saber-local"

    def __init__(self, agent: Any, version: str = "python"):
        self.agent = agent
        self.version = version

    def execute(self, case: EvalCase) -> AgentOutput:
        started = time.perf_counter()
        raw_events: list[dict[str, Any]] = []
        response = None
        before_memory = _agent_memory_snapshot(self.agent)

        user_turns = [turn for turn in case.turns if turn.role == "user"]
        if not user_turns:
            raise ValueError(f"case {case.case_id!r} has no user turn")
        use_rag = _case_use_rag(case)

        # Replay the conversation in order so short-term memory is exercised.
        # Only the last turn is captured as the scored output.
        for index, turn in enumerate(user_turns):
            is_last = index == len(user_turns) - 1
            # Mock streaming sleeps per character to simulate a typewriter.  It
            # is a UI effect, not Agent latency, so benchmarks use the sync path
            # in Mock mode and reconstruct trace events from the completed task.
            if is_last and hasattr(self.agent, "process_stream") and _agent_uses_real_llm(self.agent):
                response = self.agent.process_stream(
                    turn.content,
                    ChatOptions(use_rag=use_rag),
                    lambda event: raw_events.append(event if isinstance(event, dict) else {}),
                )
            else:
                response = self.agent.process_with_options(
                    turn.content,
                    ChatOptions(use_rag=use_rag),
                )

        if response is None:
            raise RuntimeError("local Agent returned no response")

        writer = getattr(self.agent, "memory_writer", None)
        if writer is not None and hasattr(writer, "flush"):
            writer.flush(timeout=5.0)
        after_memory = _agent_memory_snapshot(self.agent)
        memory_writes = [value for value in after_memory if value not in before_memory]

        tool_calls = _tool_calls_from_local_response(response)
        evidence_ids = _evidence_ids_from_search_results(getattr(response, "search_results", []))
        trace = _normalise_local_events(raw_events, response)
        rag_trace = getattr(response, "rag_trace", {})
        rag_decision = rag_trace.get("decision") if isinstance(rag_trace, Mapping) else None
        structured = _structured_domain_output(response)
        structured_error = None
        if isinstance(structured, Mapping) and structured.get("ok") is False:
            error = structured.get("error")
            structured_error = str(error.get("message")) if isinstance(error, Mapping) else str(error or "tool failed")
        structured_abstention = bool(isinstance(structured, Mapping) and structured.get("ok") is False)
        output = AgentOutput(
            intent=(
                str(getattr(response, "intent", "") or "").strip()
                or str(getattr(response, "mode", "") or "").strip()
                or None
            ),
            slots=dict(getattr(response, "slots", {}) or {}),
            tool_calls=tool_calls,
            content=str(getattr(response, "answer", "") or ""),
            evidence_ids=evidence_ids,
            fallback=bool(
                getattr(response, "interrupted", False)
                or getattr(response, "fallback", False)
                or structured_error
            ),
            abstained=(
                True if structured_abstention or rag_decision == "no_answer"
                else False if rag_decision == "answer"
                else None
            ),
            error=str(getattr(response, "error", "") or structured_error or "") or None,
            trace=trace,
            memory_writes=memory_writes,
        )
        return _ensure_final_trace(output, elapsed_ms=(time.perf_counter() - started) * 1000)


class HTTPAgentAdapter(AgentAdapter):
    """Evaluate an OpenAI-like or AGI-saber HTTP endpoint.

    The target response may already use the ``AgentOutput`` contract.  When it
    instead returns AGI-saber's ``/api/chat`` shape, the adapter derives the
    intent, tool calls, evidence and a minimal auditable trace.
    """

    name = "http-agent"

    def __init__(
        self,
        endpoint: str,
        *,
        version: str = "unknown",
        timeout_seconds: float = 30.0,
        headers: Mapping[str, str] | None = None,
    ):
        endpoint = endpoint.strip()
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("HTTP Agent endpoint must start with http:// or https://")
        self.endpoint = endpoint
        self.version = version
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.headers = {str(key): str(value) for key, value in (headers or {}).items()}

    def execute(self, case: EvalCase) -> AgentOutput:
        started = time.perf_counter()
        last_user = next((turn for turn in reversed(case.turns) if turn.role == "user"), None)
        if last_user is None:
            raise ValueError(f"case {case.case_id!r} has no user turn")
        request_payload = {
            "message": last_user.content,
            "turns": [turn.model_dump(mode="json") for turn in case.turns],
            "use_rag": _case_use_rag(case),
            "evaluation": {"case_id": case.case_id, "scenario": case.scenario},
        }
        try:
            response = requests.post(
                self.endpoint,
                json=request_payload,
                headers={"Content-Type": "application/json", **self.headers},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("HTTP Agent response must be a JSON object")
            output = _output_from_http_payload(payload)
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000
            return AgentOutput(
                content="",
                error=f"{type(exc).__name__}: {exc}",
                trace=[
                    TraceEvent(
                        sequence=0,
                        event_type=TraceEventType.ERROR.value,
                        name="http_agent",
                        status="timeout" if isinstance(exc, requests.Timeout) else "error",
                        payload={"error_type": type(exc).__name__},
                        duration_ms=elapsed_ms,
                    )
                ],
            )
        return _ensure_final_trace(output, elapsed_ms=(time.perf_counter() - started) * 1000)


def build_adapter(config: Mapping[str, Any], local_agent: Any | None = None) -> AgentAdapter:
    """Build a safe, explicit adapter from persisted run configuration."""

    adapter_type = str(config.get("type", "replay")).strip().casefold()
    if adapter_type == "replay":
        return ReplayAgentAdapter(profile=str(config.get("profile", "baseline")))
    if adapter_type == "local":
        if local_agent is None:
            raise ValueError("local Agent adapter is unavailable in this process")
        return LocalAGISaberAdapter(local_agent, version=str(config.get("version", "python")))
    if adapter_type == "http":
        # Secrets stay in request headers and are never copied into traces or reports.
        return HTTPAgentAdapter(
            endpoint=str(config.get("endpoint", "")),
            version=str(config.get("version", "unknown")),
            timeout_seconds=float(config.get("timeout_seconds", 30)),
            headers=config.get("headers") if isinstance(config.get("headers"), Mapping) else None,
        )
    raise ValueError(f"unsupported Agent adapter type: {adapter_type}")


def _output_from_http_payload(payload: dict[str, Any]) -> AgentOutput:
    contract_fields = {
        "intent", "slots", "content", "evidence_ids", "fallback", "abstained", "trace",
        "memory_reads", "memory_writes",
    }
    if "content" in payload and "answer" not in payload and contract_fields.intersection(payload):
        return AgentOutput.model_validate(payload)

    tool_calls: list[ToolCall] = []
    raw_calls = payload.get("tool_calls")
    if not isinstance(raw_calls, list):
        raw_call = payload.get("tool_call")
        raw_calls = [raw_call] if isinstance(raw_call, dict) and raw_call else []
    for raw_call in raw_calls:
        if not isinstance(raw_call, dict):
            continue
        success = bool(raw_call.get("success", payload.get("success", True)))
        tool_calls.append(ToolCall(
            name=str(
                raw_call.get("tool_name") or raw_call.get("tool")
                or raw_call.get("name") or "unknown"
            ),
            arguments=raw_call.get("params") or raw_call.get("arguments") or {},
            status="success" if success else "error",
            result=raw_call.get("tool_result", raw_call.get("result")),
            error=str(raw_call.get("error") or "") or None,
        ))
    if not tool_calls:
        for step in payload.get("steps") or []:
            if not isinstance(step, dict) or str(step.get("type", "")).casefold() != "action":
                continue
            tool_calls.append(
                ToolCall(
                    name=str(step.get("tool") or "unknown"),
                    arguments=step.get("params") or {},
                )
            )

    evidence_ids = _evidence_ids_from_search_results(payload.get("search_results") or [])
    trace: list[TraceEvent] = [
        TraceEvent(
            sequence=0,
            event_type=TraceEventType.INTENT_PREDICTED.value,
            payload={"intent": payload.get("mode")},
        )
    ]
    for call in tool_calls:
        trace.append(
            TraceEvent(
                sequence=len(trace),
                event_type=TraceEventType.TOOL_CALL.value,
                name=call.name,
                payload={"arguments": call.arguments},
                status="ok" if call.status == "success" else call.status,
            )
        )
        trace.append(
            TraceEvent(
                sequence=len(trace),
                event_type=TraceEventType.TOOL_RESULT.value,
                name=call.name,
                payload={},
                status="ok" if call.status == "success" else call.status,
            )
        )
    if evidence_ids:
        trace.append(
            TraceEvent(
                sequence=len(trace),
                event_type=TraceEventType.RETRIEVAL.value,
                payload={"evidence_ids": evidence_ids},
            )
        )
    trace.append(
        TraceEvent(
            sequence=len(trace),
            event_type=TraceEventType.FINAL_RESPONSE.value,
            payload={},
        )
    )
    return AgentOutput(
        intent=str(payload.get("mode") or "") or None,
        slots=payload.get("slots") or {},
        tool_calls=tool_calls,
        content=str(payload.get("answer") or ""),
        evidence_ids=evidence_ids,
        fallback=bool(payload.get("fallback") or payload.get("interrupted")),
        abstained=payload.get("abstained"),
        error=None if payload.get("success", True) else str(payload.get("error") or "agent failed"),
        trace=trace,
        memory_reads=payload.get("memory_reads") or [],
        memory_writes=payload.get("memory_writes") or [],
    )


def _agent_memory_snapshot(agent: Any) -> list[str]:
    values: list[str] = []
    ltm = getattr(agent, "ltm", None)
    if ltm is not None and hasattr(ltm, "snapshot"):
        try:
            values.extend(str(item.content) for item in ltm.snapshot() if getattr(item, "status", "active") == "active")
        except Exception:
            pass
    preference = getattr(agent, "preference", None)
    if preference is not None:
        try:
            snapshot = preference.snapshot() if hasattr(preference, "snapshot") else preference.get_all()
            if isinstance(snapshot, Mapping):
                values.extend(f"{key}={value}" for key, value in sorted(snapshot.items()))
        except Exception:
            pass
    return values


def _tool_calls_from_local_response(response: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    task = getattr(response, "task", None)
    graph = task.get("graph") if isinstance(task, Mapping) else None
    nodes = graph.get("nodes") if isinstance(graph, Mapping) else None
    if isinstance(nodes, Mapping):
        for node in nodes.values():
            if not isinstance(node, Mapping) or not node.get("tool_name"):
                continue
            status = str(node.get("status") or "success").casefold()
            status = {"done": "success", "failed": "error", "cancelled": "skipped"}.get(status, status)
            if status not in {"success", "error", "timeout", "skipped"}:
                status = "success"
            result = _maybe_json(node.get("result"))
            error = str(node.get("error") or "") or None
            if isinstance(result, Mapping) and result.get("ok") is False:
                status = "error"
                result_error = result.get("error")
                error = str(result_error.get("message")) if isinstance(result_error, Mapping) else str(result_error or "tool failed")
            calls.append(ToolCall(
                name=str(node.get("tool_name")),
                arguments=dict(node.get("params") or {}),
                status=status,
                result=result,
                error=error,
            ))
        if calls:
            return calls
    raw_call = getattr(response, "tool_call", None)
    if isinstance(raw_call, dict) and raw_call:
        calls.append(
            ToolCall(
                name=str(raw_call.get("tool") or raw_call.get("name") or "unknown"),
                arguments=raw_call.get("params") or raw_call.get("arguments") or {},
                result=raw_call.get("result"),
            )
        )
    for step in getattr(response, "steps", []) or []:
        if str(getattr(step, "type", "")).casefold() != "action":
            continue
        calls.append(
            ToolCall(
                name=str(getattr(step, "tool", "") or "unknown"),
                arguments=getattr(step, "params", None) or {},
            )
        )
    return calls


def _case_use_rag(case: EvalCase) -> bool:
    """从测试输入配置决定是否启用 RAG，绝不读取 expected 标签。

    ``expected.evidence_ids`` 是评分答案的一部分。用它控制被测系统的路由会
    形成标签泄漏，让离线分数虚高。数据集应在 ``metadata.input.use_rag``
    （兼容 ``metadata.use_rag``）显式描述用户当时的开关状态。
    """
    metadata = case.metadata if isinstance(case.metadata, Mapping) else {}
    input_options = metadata.get("input", {})
    if isinstance(input_options, Mapping) and "use_rag" in input_options:
        return bool(input_options.get("use_rag"))
    if "use_rag" in metadata:
        return bool(metadata.get("use_rag"))
    return False


def _evidence_ids_from_search_results(results: Any) -> list[str]:
    evidence: list[str] = []
    for index, item in enumerate(results if isinstance(results, list) else []):
        if not isinstance(item, dict):
            continue
        candidate = (
            item.get("evidence_id")
            or item.get("chunk_id")
            or item.get("id")
            or item.get("doc_hash")
            or item.get("source")
            or f"result-{index}"
        )
        value = str(candidate).strip()
        if value and value not in evidence:
            evidence.append(value)
    return evidence


def _normalise_local_events(raw_events: list[dict[str, Any]], response: Any) -> list[TraceEvent]:
    mapping = {
        "route": TraceEventType.INTENT_PREDICTED.value,
        "memory": TraceEventType.SLOT_EXTRACTED.value,
        "guardrail": TraceEventType.GUARDRAIL.value,
        "plan_created": "plan_created",
        "tool_call": TraceEventType.TOOL_CALL.value,
        "tool_result": TraceEventType.TOOL_RESULT.value,
        "rag_result": TraceEventType.RETRIEVAL.value,
        "done": TraceEventType.FINAL_RESPONSE.value,
    }
    trace: list[TraceEvent] = []
    for raw in raw_events:
        raw_type = str(raw.get("type", "")).casefold()
        event_type = mapping.get(raw_type)
        if event_type is None:
            continue
        payload = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        tool_name = payload.get("tool") or payload.get("name")
        raw_status = str(payload.get("status") or "ok").casefold()
        status = "error" if raw_status in {"error", "failed"} else "timeout" if raw_status == "timeout" else "skipped" if raw_status in {"skipped", "cancelled"} else "ok"
        trace.append(
            TraceEvent(
                sequence=len(trace),
                event_type=event_type,
                name=str(tool_name) if tool_name else None,
                payload=payload,
                status=status,
            )
        )
    if not any(event.event_type == TraceEventType.INTENT_PREDICTED.value for event in trace):
        trace.insert(
            0,
            TraceEvent(
                sequence=0,
                event_type=TraceEventType.INTENT_PREDICTED.value,
                payload={"intent": getattr(response, "mode", None)},
            ),
        )
        trace = [event.model_copy(update={"sequence": index}) for index, event in enumerate(trace)]
    response_slots = getattr(response, "slots", {}) or {}
    if (
        isinstance(response_slots, Mapping)
        and any(value is not None for value in response_slots.values())
        and not any(event.event_type == TraceEventType.SLOT_EXTRACTED.value for event in trace)
    ):
        intent_index = next(
            (index for index, event in enumerate(trace) if event.event_type == TraceEventType.INTENT_PREDICTED.value),
            -1,
        )
        trace.insert(intent_index + 1, TraceEvent(
            sequence=intent_index + 1,
            event_type=TraceEventType.SLOT_EXTRACTED.value,
            payload={"slots": dict(response_slots)},
        ))
        trace = [event.model_copy(update={"sequence": index}) for index, event in enumerate(trace)]
    structured = _structured_domain_output(response)
    evidence = structured.get("evidence") if isinstance(structured, Mapping) else None
    guardrails = evidence.get("guardrails") if isinstance(evidence, Mapping) else None
    if isinstance(guardrails, list) and not any(
        event.event_type == TraceEventType.GUARDRAIL.value for event in trace
    ):
        insert_at = next(
            (
                index for index, event in enumerate(trace)
                if event.event_type in {"plan_created", TraceEventType.TOOL_CALL.value, TraceEventType.FINAL_RESPONSE.value}
            ),
            len(trace),
        )
        for offset, guardrail in enumerate(guardrails):
            if not isinstance(guardrail, Mapping):
                continue
            trace.insert(insert_at + offset, TraceEvent(
                sequence=insert_at + offset,
                event_type=TraceEventType.GUARDRAIL.value,
                name=str(guardrail.get("id") or "farm_guardrail"),
                payload=dict(guardrail),
            ))
        trace = [event.model_copy(update={"sequence": index}) for index, event in enumerate(trace)]
    if not any(event.event_type == TraceEventType.TOOL_CALL.value for event in trace):
        calls = _tool_calls_from_local_response(response)
        if calls:
            task = getattr(response, "task", None)
            graph = task.get("graph") if isinstance(task, Mapping) else None
            nodes = graph.get("nodes") if isinstance(graph, Mapping) else {}
            trace.append(TraceEvent(
                sequence=len(trace),
                event_type="plan_created",
                payload={"nodes": list(nodes.values()) if isinstance(nodes, Mapping) else []},
            ))
            for call in calls:
                trace.append(TraceEvent(
                    sequence=len(trace),
                    event_type=TraceEventType.TOOL_CALL.value,
                    name=call.name,
                    payload={"arguments": call.arguments},
                    status="ok",
                ))
                result_status = "ok" if call.status == "success" else call.status
                trace.append(TraceEvent(
                    sequence=len(trace),
                    event_type=TraceEventType.TOOL_RESULT.value,
                    name=call.name,
                    payload={"result": call.result, "error": call.error},
                    status=result_status,
                ))
    needs_fallback = bool(
        getattr(response, "fallback", False)
        or (isinstance(structured, Mapping) and structured.get("ok") is False)
    )
    if needs_fallback and not any(event.event_type == TraceEventType.FALLBACK.value for event in trace):
        insert_at = next(
            (index for index, event in enumerate(trace) if event.event_type == TraceEventType.FINAL_RESPONSE.value),
            len(trace),
        )
        trace.insert(insert_at, TraceEvent(
            sequence=insert_at,
            event_type=TraceEventType.FALLBACK.value,
            payload={"reason": getattr(response, "error", None) or "domain_tool_failure"},
        ))
        trace = [event.model_copy(update={"sequence": index}) for index, event in enumerate(trace)]
    return trace


def _agent_uses_real_llm(agent: Any) -> bool:
    cfg = getattr(agent, "cfg", None)
    check = getattr(cfg, "is_real_llm", None)
    try:
        return bool(check()) if callable(check) else False
    except Exception:
        return False


def _structured_domain_output(response: Any) -> Mapping[str, Any] | None:
    for call in reversed(_tool_calls_from_local_response(response)):
        if call.name == "farm_copilot" and isinstance(call.result, Mapping):
            return call.result
    return None


def _maybe_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _ensure_final_trace(output: AgentOutput, *, elapsed_ms: float) -> AgentOutput:
    trace = list(output.trace)
    if not any(event.event_type == TraceEventType.FINAL_RESPONSE.value for event in trace):
        trace.append(
            TraceEvent(
                sequence=(max((event.sequence for event in trace), default=-1) + 1),
                event_type=TraceEventType.FINAL_RESPONSE.value,
                payload={"content_length": len(output.content)},
                duration_ms=max(0.0, elapsed_ms),
            )
        )
    return output.model_copy(update={"trace": trace})


def redact_adapter_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return a report-safe adapter config without headers or token values."""

    safe = {str(key): value for key, value in config.items() if str(key).casefold() != "headers"}
    for key in list(safe):
        if any(marker in key.casefold() for marker in ("key", "token", "secret", "password")):
            safe[key] = "***"
    # Ensure values remain JSON serialisable before persistence.
    return json.loads(json.dumps(safe, ensure_ascii=False, default=str))
