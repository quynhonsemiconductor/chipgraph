"""Tests for AgentResult and Decision."""

import pytest
from pydantic import ValidationError

from chipgraph.core.contracts import AgentResult, Decision


def test_agent_result_round_trip() -> None:
    result = AgentResult(
        status="done",
        files_written=("design/timer/rtl/m_cnt.sv",),
        assumptions=("clock is single-domain",),
        tokens=1234,
        cost=0.05,
    )
    assert AgentResult.model_validate_json(result.model_dump_json()) == result


def test_agent_result_needs_human_requires_open_question() -> None:
    with pytest.raises(ValidationError):
        AgentResult(status="needs_human")


def test_agent_result_needs_human_with_question_ok() -> None:
    result = AgentResult(status="needs_human", open_questions=("what reset polarity?",))
    assert result.status == "needs_human"


def test_decision_round_trip() -> None:
    decision = Decision(
        question_id="reset_polarity", value="active_low", confidence=0.9, backend="large"
    )
    assert Decision.model_validate_json(decision.model_dump_json()) == decision


def test_decision_confidence_bounds() -> None:
    with pytest.raises(ValidationError):
        Decision(question_id="q", value=1, confidence=1.5, backend="rule")
    with pytest.raises(ValidationError):
        Decision(question_id="q", value=1, confidence=-0.1, backend="rule")
