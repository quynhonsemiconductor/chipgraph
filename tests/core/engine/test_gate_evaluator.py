"""Tests for gate evaluation and the approve/baseline helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from chipgraph.adapters.review.file import FileReview
from chipgraph.core.contracts import ArtifactRef, RuleInstance
from chipgraph.core.engine.gate import GateError, GateEvaluator, approve, baseline, gate_id_for
from chipgraph.core.state.artifacts import ArtifactStore, LabelRules


def _instance(
    *, inputs: tuple[ArtifactRef, ...], params: dict[str, str] | None = None
) -> RuleInstance:
    params = params or {"block": "timer"}
    output = ArtifactRef(kind="rtl", path="design/timer/rtl/m_timer.sv")
    return RuleInstance(
        rule_id="digital-rtl/rtl_module",
        params=params,
        inputs=inputs,
        outputs=(output,),
        instance_id=RuleInstance.make_id("digital-rtl/rtl_module", params),
    )


def _write(root: Path, rel_path: str, content: str) -> ArtifactRef:
    full = root / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    return ArtifactRef(kind="spec", path=rel_path)


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path, LabelRules({}))


@pytest.fixture
def review(tmp_path: Path) -> FileReview:
    return FileReview(tmp_path / ".chipgraph" / "decisions")


# --- gate_id_for ---------------------------------------------------------------------


def test_gate_id_for_formats_params() -> None:
    assert gate_id_for("spec:{block}", {"block": "timer"}) == "spec:timer"


def test_gate_id_for_ignores_extra_params() -> None:
    assert gate_id_for("spec:{block}", {"block": "timer", "other": "x"}) == "spec:timer"


def test_gate_id_for_missing_param_raises() -> None:
    with pytest.raises(GateError, match="block"):
        gate_id_for("spec:{block}", {})


# --- GateEvaluator.status --------------------------------------------------------------


def test_status_waiting_with_no_decisions(
    tmp_path: Path, store: ArtifactStore, review: FileReview
) -> None:
    spec = _write(tmp_path, "design/timer/spec.md", "v1")
    instance = _instance(inputs=(spec,))
    evaluator = GateEvaluator(review, store)
    assert evaluator.status("spec:timer", instance) == "waiting"


def test_status_approved_after_approve(
    tmp_path: Path, store: ArtifactStore, review: FileReview
) -> None:
    spec = _write(tmp_path, "design/timer/spec.md", "v1")
    instance = _instance(inputs=(spec,))
    approve(review, store, "spec:timer", instance, by="nghia")
    evaluator = GateEvaluator(review, store)
    assert evaluator.status("spec:timer", instance) == "approved"


def test_status_back_to_waiting_after_input_changes(
    tmp_path: Path, store: ArtifactStore, review: FileReview
) -> None:
    spec = _write(tmp_path, "design/timer/spec.md", "v1")
    instance = _instance(inputs=(spec,))
    approve(review, store, "spec:timer", instance, by="nghia")
    evaluator = GateEvaluator(review, store)
    assert evaluator.status("spec:timer", instance) == "approved"

    (tmp_path / "design/timer/spec.md").write_text("v2")
    assert evaluator.status("spec:timer", instance) == "waiting"


def test_status_rejected_after_reject(
    tmp_path: Path, store: ArtifactStore, review: FileReview
) -> None:
    spec = _write(tmp_path, "design/timer/spec.md", "v1")
    instance = _instance(inputs=(spec,))
    approve(review, store, "spec:timer", instance, by="nghia", decision="reject")
    evaluator = GateEvaluator(review, store)
    assert evaluator.status("spec:timer", instance) == "rejected"


def test_status_latest_decision_wins_approve_then_reject(
    tmp_path: Path, store: ArtifactStore, review: FileReview
) -> None:
    spec = _write(tmp_path, "design/timer/spec.md", "v1")
    instance = _instance(inputs=(spec,))
    approve(review, store, "spec:timer", instance, by="a")
    approve(review, store, "spec:timer", instance, by="b", decision="reject")
    evaluator = GateEvaluator(review, store)
    assert evaluator.status("spec:timer", instance) == "rejected"


def test_status_latest_decision_wins_reject_then_approve(
    tmp_path: Path, store: ArtifactStore, review: FileReview
) -> None:
    spec = _write(tmp_path, "design/timer/spec.md", "v1")
    instance = _instance(inputs=(spec,))
    approve(review, store, "spec:timer", instance, by="a", decision="reject")
    approve(review, store, "spec:timer", instance, by="b")
    evaluator = GateEvaluator(review, store)
    assert evaluator.status("spec:timer", instance) == "approved"


def test_status_baseline_counts_as_approved(
    tmp_path: Path, store: ArtifactStore, review: FileReview
) -> None:
    spec = _write(tmp_path, "design/timer/spec.md", "v1")
    instance = _instance(inputs=(spec,))
    baseline(review, store, "spec:timer", (spec,), by="lead")
    evaluator = GateEvaluator(review, store)
    assert evaluator.status("spec:timer", instance) == "approved"


def test_status_waive_does_not_count_as_approved(
    tmp_path: Path, store: ArtifactStore, review: FileReview
) -> None:
    spec = _write(tmp_path, "design/timer/spec.md", "v1")
    instance = _instance(inputs=(spec,))
    hashes = store.current_hashes((spec,))
    from datetime import UTC, datetime

    from chipgraph.core.contracts import Approval

    review.record(
        Approval(
            gate_id="spec:timer",
            by="a",
            at=datetime.now(UTC),
            artifact_hashes=hashes,
            decision="waive",
        )
    )
    evaluator = GateEvaluator(review, store)
    assert evaluator.status("spec:timer", instance) == "waiting"


def test_status_approval_covering_fewer_inputs_is_waiting(
    tmp_path: Path, store: ArtifactStore, review: FileReview
) -> None:
    spec = _write(tmp_path, "design/timer/spec.md", "v1")
    instance = _instance(inputs=(spec,))
    approve(review, store, "spec:timer", instance, by="nghia")

    # A second input file is added to the instance; the old approval only covers the
    # first one, so it must no longer count.
    iface = _write(tmp_path, "design/timer/iface.md", "v1")
    wider_instance = _instance(inputs=(spec, iface))
    evaluator = GateEvaluator(review, store)
    assert evaluator.status("spec:timer", wider_instance) == "waiting"


def test_status_pr_gate_waiting_without_approve(
    tmp_path: Path, store: ArtifactStore, review: FileReview
) -> None:
    spec = _write(tmp_path, "design/timer/spec.md", "v1")
    instance = _instance(inputs=(spec,))
    evaluator = GateEvaluator(review, store, pr_gates=("pr",))
    assert evaluator.status("pr:timer-review", instance) == "waiting"


def test_status_pr_gate_approved_with_current_approve(
    tmp_path: Path, store: ArtifactStore, review: FileReview
) -> None:
    spec = _write(tmp_path, "design/timer/spec.md", "v1")
    instance = _instance(inputs=(spec,))
    approve(review, store, "pr:timer-review", instance, by="nghia")
    evaluator = GateEvaluator(review, store, pr_gates=("pr",))
    assert evaluator.status("pr:timer-review", instance) == "approved"


# --- approve / baseline errors ---------------------------------------------------------


def test_approve_with_no_file_inputs_raises(store: ArtifactStore, review: FileReview) -> None:
    model_ref = ArtifactRef(kind="report", model_key="block/timer")
    instance = _instance(inputs=(model_ref,))
    with pytest.raises(GateError):
        approve(review, store, "spec:timer", instance, by="nghia")


def test_baseline_with_no_existing_refs_raises(
    tmp_path: Path, store: ArtifactStore, review: FileReview
) -> None:
    missing = ArtifactRef(kind="spec", path="design/timer/missing.md")
    with pytest.raises(GateError):
        baseline(review, store, "spec:timer", (missing,), by="lead")
