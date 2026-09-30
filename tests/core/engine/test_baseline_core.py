"""Tests for `chipgraph.core.engine.baseline`: planning from gates and VCS facts."""

from __future__ import annotations

from chipgraph.core.contracts import ArtifactRef, RuleInstance
from chipgraph.core.engine.baseline import (
    CommitInfo,
    GateInput,
    clean_refs_for_gate,
    gate_inputs_from_instances,
    plan_baseline,
)


class FakeFacts:
    """A `VcsFacts` backed by fixed per-path dicts, for deterministic planning tests."""

    def __init__(
        self,
        sha: dict[str, str],
        tracked: set[str],
        dirty: set[str],
        commits: dict[str, CommitInfo] | None = None,
    ) -> None:
        self._sha = sha
        self._tracked = tracked
        self._dirty = dirty
        self._commits = commits or {}

    def sha256(self, path: str) -> str | None:
        return self._sha.get(path)

    def is_tracked(self, path: str) -> bool:
        return path in self._tracked

    def is_dirty(self, path: str) -> bool:
        return path in self._dirty

    def last_commit(self, path: str) -> CommitInfo | None:
        return self._commits.get(path)


def _gate(gate_id: str, *paths: str, decided: bool = False) -> GateInput:
    return GateInput(
        gate_id=gate_id,
        instance_id=f"pack/rule[{gate_id}]",
        artifacts=tuple(ArtifactRef(kind="spec", path=p) for p in paths),
        already_decided=decided,
    )


def test_plan_classifies_clean_dirty_untracked_and_missing() -> None:
    facts = FakeFacts(
        sha={"a.md": "aa", "b.md": "bb"},  # c.md missing on disk
        tracked={"a.md", "b.md"},
        dirty={"b.md"},
        commits={"a.md": CommitInfo(short_sha="1234abc", date="2026-01-02", subject="add a")},
    )
    plan = plan_baseline([_gate("spec:x", "a.md", "b.md", "c.md")], facts)

    (gate,) = plan.gates
    by_path = {a.path: a for a in gate.artifacts}
    assert by_path["a.md"].status == "clean"
    assert by_path["a.md"].baselineable is True
    assert by_path["a.md"].short_sha256 == "aa"
    assert by_path["a.md"].last_commit is not None
    assert by_path["b.md"].status == "dirty"
    assert by_path["b.md"].baselineable is False
    assert by_path["c.md"].status == "missing"
    assert by_path["c.md"].short_sha256 == "-"

    # Only the clean, tracked artifact would be pinned by a baseline decision.
    assert [a.path for a in gate.clean_artifacts] == ["a.md"]
    assert gate.will_baseline is True


def test_plan_is_deterministic_and_sorts_gates_and_artifacts() -> None:
    facts = FakeFacts(sha={"z.md": "z", "a.md": "a"}, tracked={"z.md", "a.md"}, dirty=set())
    plan = plan_baseline(
        [_gate("spec:b", "z.md", "a.md"), _gate("spec:a", "a.md")],
        facts,
    )
    assert [g.gate_id for g in plan.gates] == ["spec:a", "spec:b"]
    assert [a.path for a in plan.gates[1].artifacts] == ["a.md", "z.md"]


def test_decided_gate_is_skipped_not_baselined() -> None:
    facts = FakeFacts(sha={"a.md": "a"}, tracked={"a.md"}, dirty=set())
    plan = plan_baseline([_gate("spec:x", "a.md", decided=True)], facts)
    assert plan.gates[0].already_decided is True
    assert plan.gates[0].will_baseline is False
    assert [g.gate_id for g in plan.skipped_gates] == ["spec:x"]
    assert plan.gates_to_baseline == ()


def test_gate_with_only_dirty_artifacts_is_not_baselined() -> None:
    facts = FakeFacts(sha={"a.md": "a"}, tracked={"a.md"}, dirty={"a.md"})
    plan = plan_baseline([_gate("spec:x", "a.md")], facts)
    assert plan.gates[0].will_baseline is False
    assert [a.path for a in plan.dirty_artifacts] == ["a.md"]


def test_dirty_artifacts_are_deduplicated_across_gates() -> None:
    facts = FakeFacts(sha={"shared.md": "s"}, tracked=set(), dirty=set())
    plan = plan_baseline(
        [_gate("spec:x", "shared.md"), _gate("spec:y", "shared.md")],
        facts,
    )
    assert [a.path for a in plan.dirty_artifacts] == ["shared.md"]


def test_open_findings_count_is_carried() -> None:
    facts = FakeFacts(sha={}, tracked=set(), dirty=set())
    plan = plan_baseline([], facts, open_findings=3)
    assert plan.open_findings == 3


def test_gate_inputs_from_instances_uses_file_inputs_only() -> None:
    instance = RuleInstance(
        rule_id="pack/rtl",
        params={"block": "timer"},
        inputs=(
            ArtifactRef(kind="spec", path="doc/spec.md"),
            ArtifactRef(kind="model", model_key="block:timer"),  # not a file input
        ),
        outputs=(ArtifactRef(kind="rtl", path="rtl/timer.sv"),),
        instance_id=RuleInstance.make_id("pack/rtl", {"block": "timer"}),
    )
    (gate_input,) = gate_inputs_from_instances([("spec:timer", instance, False)])
    assert [ref.path for ref in gate_input.artifacts] == ["doc/spec.md"]


def test_clean_refs_for_gate_looks_up_original_refs() -> None:
    facts = FakeFacts(sha={"a.md": "a", "b.md": "b"}, tracked={"a.md", "b.md"}, dirty={"b.md"})
    plan = plan_baseline([_gate("spec:x", "a.md", "b.md")], facts)
    ref_a = ArtifactRef(kind="spec", path="a.md", label="public")
    ref_b = ArtifactRef(kind="spec", path="b.md")
    refs = clean_refs_for_gate(plan.gates[0], {"a.md": ref_a, "b.md": ref_b})
    # Only the clean artifact's ref is returned, and it is the original (label kept).
    assert refs == (ref_a,)


def test_a_declared_input_missing_on_disk_is_missing_not_untracked() -> None:
    class _Facts:
        def sha256(self, path: str) -> str | None:
            return None if path == "spec/none.md" else "a" * 64

        def is_tracked(self, path: str) -> bool:
            return path != "spec/none.md"

        def is_dirty(self, path: str) -> bool:
            return False

        def last_commit(self, path: str) -> None:
            return None

    gate = GateInput(
        gate_id="spec:x",
        instance_id="p/r[block=x]",
        artifacts=(
            ArtifactRef(kind="spec", path="spec/none.md"),
            ArtifactRef(kind="spec", path="x.f"),
        ),
        already_decided=False,
    )
    plan = plan_baseline([gate], _Facts())
    statuses = {a.path: a.status for a in plan.gates[0].artifacts}
    assert statuses == {"spec/none.md": "missing", "x.f": "clean"}
    assert plan.dirty_artifacts == ()
