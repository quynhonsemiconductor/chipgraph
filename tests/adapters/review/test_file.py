"""Tests for the `file` review adapter."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from chipgraph.adapters.review.file import FileReview, ReviewError
from chipgraph.core.contracts import Approval

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+--\d{8}T\d{6}Z-[0-9a-f]{6}\.yml$")


def _approval(
    gate_id: str = "spec:timer",
    *,
    by: str = "nghia",
    decision: str = "approve",
    hashes: dict[str, str] | None = None,
    note: str = "",
) -> Approval:
    return Approval(
        gate_id=gate_id,
        by=by,
        at=datetime.now(UTC),
        artifact_hashes=hashes or {".:design/timer/spec.md": "a" * 64},
        decision=decision,
        note=note,
    )


def test_record_writes_one_file_per_call(tmp_path: Path) -> None:
    review = FileReview(tmp_path)
    review.record(_approval())
    review.record(_approval())
    files = sorted(tmp_path.glob("*.yml"))
    assert len(files) == 2


def test_record_file_name_format(tmp_path: Path) -> None:
    review = FileReview(tmp_path)
    review.record(_approval(gate_id="spec:timer"))
    (path,) = tmp_path.glob("*.yml")
    assert _NAME_RE.match(path.name), path.name
    assert path.name.startswith("spec_timer--")


def test_record_sanitizes_unsafe_gate_id_chars(tmp_path: Path) -> None:
    review = FileReview(tmp_path)
    review.record(_approval(gate_id="pr:review/timer #1"))
    (path,) = tmp_path.glob("*.yml")
    assert _NAME_RE.match(path.name), path.name
    assert "/" not in path.name
    assert " " not in path.name
    assert "#" not in path.name


def test_record_content_round_trips(tmp_path: Path) -> None:
    review = FileReview(tmp_path)
    approval = _approval(note="looks good")
    review.record(approval)
    (path,) = tmp_path.glob("*.yml")
    raw = yaml.safe_load(path.read_text())
    assert raw == approval.model_dump(mode="json")
    assert Approval.model_validate(raw) == approval


def test_approvals_filters_by_gate_id(tmp_path: Path) -> None:
    review = FileReview(tmp_path)
    a = _approval(gate_id="spec:timer")
    b = _approval(gate_id="spec:pwm")
    review.record(a)
    review.record(b)
    assert review.approvals("spec:timer") == (a,)
    assert review.approvals("spec:pwm") == (b,)
    assert review.approvals("spec:missing") == ()


def test_approvals_sorted_by_at_then_name(tmp_path: Path) -> None:
    review = FileReview(tmp_path)
    early = Approval(
        gate_id="spec:timer",
        by="a",
        at=datetime(2026, 1, 1, tzinfo=UTC),
        artifact_hashes={".:x": "a" * 64},
        decision="approve",
    )
    late = Approval(
        gate_id="spec:timer",
        by="b",
        at=datetime(2026, 1, 2, tzinfo=UTC),
        artifact_hashes={".:x": "b" * 64},
        decision="reject",
    )
    # Record in reverse order; result must still be sorted by `at`.
    review.record(late)
    review.record(early)
    assert review.approvals("spec:timer") == (early, late)


def test_approvals_empty_dir_returns_empty(tmp_path: Path) -> None:
    review = FileReview(tmp_path / "does-not-exist-yet")
    assert review.approvals("spec:timer") == ()


def test_approvals_malformed_yaml_raises(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    bad = tmp_path / "spec_timer--20260101T000000Z-abcdef.yml"
    bad.write_text("gate_id: [unterminated\n")
    review = FileReview(tmp_path)
    with pytest.raises(ReviewError, match=str(bad)):
        review.approvals("spec:timer")


def test_approvals_not_a_mapping_raises(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    bad = tmp_path / "spec_timer--20260101T000000Z-abcdef.yml"
    bad.write_text("- just\n- a\n- list\n")
    review = FileReview(tmp_path)
    with pytest.raises(ReviewError, match=str(bad)):
        review.approvals("spec:timer")


def test_approvals_invalid_approval_shape_raises(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    bad = tmp_path / "spec_timer--20260101T000000Z-abcdef.yml"
    bad.write_text(yaml.safe_dump({"gate_id": "spec:timer", "decision": "not-a-real-decision"}))
    review = FileReview(tmp_path)
    with pytest.raises(ReviewError, match=str(bad)):
        review.approvals("spec:timer")


def test_record_never_overwrites_existing_file(tmp_path: Path) -> None:
    review = FileReview(tmp_path)
    approval = _approval()
    review.record(approval)
    (path,) = tmp_path.glob("*.yml")
    original_content = path.read_text()

    # Force a name collision: any attempt with this exact name must not be used.
    review.record(approval)
    files = sorted(tmp_path.glob("*.yml"))
    assert len(files) == 2
    assert path.read_text() == original_content
