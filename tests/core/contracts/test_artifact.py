"""Tests for ArtifactRef, ProducedBy, and Artifact."""

import pytest
from pydantic import ValidationError

from chipgraph.core.contracts import Artifact, ArtifactRef, ProducedBy

SHA_A = "a" * 64
SHA_B = "b" * 64


def _rtl_ref() -> ArtifactRef:
    return ArtifactRef(kind="rtl", path="design/timer/rtl/m_x.sv")


def test_artifact_round_trip() -> None:
    artifact = Artifact(
        ref=_rtl_ref(),
        content_hash=SHA_A,
        produced_by=ProducedBy(rule_id="digital-rtl/rtl_module", run_id="run-1"),
        inputs_hash=SHA_B,
    )
    assert Artifact.model_validate_json(artifact.model_dump_json()) == artifact


def test_artifact_ref_round_trip_model_key() -> None:
    ref = ArtifactRef(kind="model", model_key="block/timer")
    assert ArtifactRef.model_validate_json(ref.model_dump_json()) == ref


def test_artifact_ref_requires_exactly_one_locator() -> None:
    with pytest.raises(ValidationError):
        ArtifactRef(kind="rtl")
    with pytest.raises(ValidationError):
        ArtifactRef(kind="rtl", path="a/b.sv", model_key="block/x")


def test_artifact_ref_rejects_dotdot_path() -> None:
    with pytest.raises(ValidationError):
        ArtifactRef(kind="rtl", path="design/../etc/passwd")


def test_artifact_ref_rejects_absolute_path() -> None:
    with pytest.raises(ValidationError):
        ArtifactRef(kind="rtl", path="/etc/passwd")


def test_artifact_ref_rejects_empty_path() -> None:
    with pytest.raises(ValidationError):
        ArtifactRef(kind="rtl", path="")


def test_artifact_ref_is_frozen() -> None:
    ref = _rtl_ref()
    with pytest.raises(ValidationError):
        ref.path = "design/other.sv"  # type: ignore[misc]


def test_artifact_ref_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ArtifactRef.model_validate({"kind": "rtl", "path": "a/b.sv", "bogus": 1})


def test_artifact_produced_by_none_for_preexisting() -> None:
    artifact = Artifact(ref=_rtl_ref(), content_hash=SHA_A)
    assert artifact.produced_by is None
    assert artifact.inputs_hash is None
