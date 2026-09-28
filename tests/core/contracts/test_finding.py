"""Tests for the `Finding`/`Evidence` contracts (M1-18, DESIGN.md 4.8)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from chipgraph.core.contracts import ArtifactRef, Evidence, Finding
from chipgraph.core.contracts.finding import compute_fingerprint, is_ai_source

SHA = "c" * 64
NOW = datetime.now(UTC)


def _finding(**overrides: object) -> Finding:
    kwargs: dict[str, object] = dict(
        layer=1,
        severity="error",
        source="check:cross_chip",
        evidence=(Evidence(file="doc/specs/TIMER_MAS.md", line=118),),
        claim="REQ-TIM-004 and REQ-TIM-009 conflict",
        first_seen=NOW,
        last_seen=NOW,
    )
    kwargs.update(overrides)
    return Finding(**kwargs)  # type: ignore[arg-type]


def test_finding_round_trip() -> None:
    finding = _finding()
    assert Finding.model_validate_json(finding.model_dump_json()) == finding


def test_finding_requires_at_least_one_evidence() -> None:
    with pytest.raises(ValidationError):
        _finding(evidence=())


def test_evidence_needs_exactly_one_locator() -> None:
    with pytest.raises(ValidationError):
        Evidence()
    with pytest.raises(ValidationError):
        Evidence(file="a.sv", model_key="k")


def test_evidence_line_requires_file() -> None:
    with pytest.raises(ValidationError):
        Evidence(model_key="k", line=3)


def test_ai_source_cannot_be_error() -> None:
    with pytest.raises(ValidationError):
        _finding(source="critic", severity="error")


def test_ai_source_can_be_warning_or_question() -> None:
    warning = _finding(source="critic", severity="warning")
    question = _finding(source="critic", severity="question")
    assert warning.severity == "warning"
    assert question.severity == "question"


def test_deterministic_sources_may_be_error() -> None:
    for source in ("check:cross_chip", "z3", "formal", "lint"):
        finding = _finding(source=source, severity="error")
        assert finding.severity == "error"


def test_is_ai_source() -> None:
    assert not is_ai_source("check:cross_chip")
    assert not is_ai_source("z3")
    assert not is_ai_source("formal")
    assert not is_ai_source("lint")
    assert is_ai_source("critic")
    assert is_ai_source("something-unknown")


def test_id_is_derived_from_fingerprint() -> None:
    finding = _finding()
    assert finding.id == f"F-{finding.fingerprint[:8]}"


def test_fingerprint_stable_across_runs_independent_of_last_seen() -> None:
    first = _finding(first_seen=NOW, last_seen=NOW)
    later = datetime.now(UTC)
    second = _finding(first_seen=later, last_seen=later, confidence=0.4)
    assert first.fingerprint == second.fingerprint
    assert first.id == second.id


def test_fingerprint_changes_with_evidence_location() -> None:
    first = _finding()
    second = _finding(evidence=(Evidence(file="doc/specs/TIMER_MAS.md", line=131),))
    assert first.fingerprint != second.fingerprint


def test_compute_fingerprint_matches_model() -> None:
    finding = _finding()
    assert finding.fingerprint == compute_fingerprint(
        finding.source, finding.claim, finding.evidence
    )


def test_layer_bounds() -> None:
    with pytest.raises(ValidationError):
        _finding(layer=0)
    with pytest.raises(ValidationError):
        _finding(layer=6)


def test_artifact_hashes_must_match_an_artifact() -> None:
    ref = ArtifactRef(kind="rtl", path="design/timer/rtl/m_cnt.sv")
    with pytest.raises(ValidationError):
        _finding(artifacts=(ref,), artifact_hashes={".:design/other.sv": SHA})


def test_artifact_hashes_accepted_when_matching() -> None:
    ref = ArtifactRef(kind="rtl", path="design/timer/rtl/m_cnt.sv")
    finding = _finding(artifacts=(ref,), artifact_hashes={".:design/timer/rtl/m_cnt.sv": SHA})
    assert finding.artifact_hashes[".:design/timer/rtl/m_cnt.sv"] == SHA
