from internal.evaluation.adapters import _case_use_rag
from internal.evaluation.schemas import EvalCase


def _case(*, evidence_ids=None, metadata=None):
    return EvalCase(
        case_id="case-1",
        scenario="adapter input isolation",
        turns=[{"role": "user", "content": "question"}],
        expected={"evidence_ids": evidence_ids or []},
        metadata=metadata or {},
    )


def test_expected_evidence_does_not_leak_into_rag_switch():
    case = _case(evidence_ids=["gold-chunk-1"])

    assert _case_use_rag(case) is False


def test_rag_switch_comes_from_explicit_input_metadata():
    case = _case(
        evidence_ids=[],
        metadata={"input": {"use_rag": True}},
    )

    assert _case_use_rag(case) is True
