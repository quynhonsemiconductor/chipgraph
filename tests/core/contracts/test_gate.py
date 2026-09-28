"""Tests for GateSpec and Approval."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from chipgraph.core.contracts import Approval, ArtifactRef, GateSpec

SHA = "c" * 64


def _gate() -> GateSpec:
    return GateSpec(
        id="gate/rtl_review",
        artifacts=(ArtifactRef(kind="rtl", path="design/timer/rtl/m_cnt.sv"),),
        approvers=("nghiavt",),
        mode="file",
    )


def test_gate_spec_round_trip() -> None:
    gate = _gate()
    assert GateSpec.model_validate_json(gate.model_dump_json()) == gate


def test_gate_spec_requires_at_least_one_artifact() -> None:
    with pytest.raises(ValidationError):
        GateSpec(id="gate/x", artifacts=())


def _approval(
    gate_id: str = "gate/rtl_review", key: str = ".:design/timer/rtl/m_cnt.sv"
) -> Approval:
    return Approval(
        gate_id=gate_id,
        by="nghiavt",
        at=datetime.now(UTC),
        artifact_hashes={key: SHA},
        decision="approve",
    )


def test_approval_round_trip() -> None:
    approval = _approval()
    assert Approval.model_validate_json(approval.model_dump_json()) == approval


def test_approval_requires_at_least_one_hash() -> None:
    with pytest.raises(ValidationError):
        Approval(
            gate_id="gate/x",
            by="nghiavt",
            at=datetime.now(UTC),
            artifact_hashes={},
            decision="approve",
        )


def test_approval_is_current_true_when_hashes_match() -> None:
    approval = _approval()
    current = {".:design/timer/rtl/m_cnt.sv": SHA}
    assert approval.is_current(current)


def test_approval_is_current_false_when_hash_changed() -> None:
    approval = _approval()
    current = {".:design/timer/rtl/m_cnt.sv": "d" * 64}
    assert not approval.is_current(current)


def test_approval_is_current_false_when_key_missing() -> None:
    approval = _approval()
    assert not approval.is_current({})


def test_approval_at_requires_timezone() -> None:
    with pytest.raises(ValidationError):
        Approval(
            gate_id="gate/x",
            by="nghiavt",
            at=datetime.now(),
            artifact_hashes={"k": SHA},
            decision="approve",
        )
