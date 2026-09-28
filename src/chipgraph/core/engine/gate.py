"""Gates: whether a rule instance's gate is approved, rejected, or waiting.

A gate covers a rule instance's *file-based* inputs: the things a person actually
reviewed. An approval is only current when every one of those hashes still matches
what is on disk today *and* the approval's hashes cover every current input (an
approval recorded against a smaller, older input set does not count as covering a
new one). This is what makes "edit a file after approval" put the gate back to
`waiting` (DESIGN.md 6.1).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from datetime import UTC, datetime
from typing import Literal

from chipgraph.core.contracts import Approval, ArtifactRef, RuleInstance
from chipgraph.core.plugin_api.protocols import ReviewAdapter
from chipgraph.core.state.artifacts import ArtifactStore

GateStatus = Literal["approved", "rejected", "waiting"]


class GateError(Exception):
    """Raised for gate id formatting errors, and invalid approve/baseline calls."""


def gate_id_for(template: str, params: Mapping[str, str]) -> str:
    """Format a gate id template, e.g. `"spec:{block}"` with `{"block": "timer"}`.

    Raises `GateError` if `template` references a param that is not in `params`.
    """
    try:
        return template.format(**params)
    except KeyError as exc:
        raise GateError(
            f"gate id template {template!r} needs param {exc.args[0]!r}, "
            f"which is not in {dict(params)!r}"
        ) from exc


def _file_inputs(instance: RuleInstance) -> tuple[ArtifactRef, ...]:
    return tuple(ref for ref in instance.inputs if ref.path is not None)


class GateEvaluator:
    """Evaluates gate status for the scheduler's `GateChecker` shape (DESIGN.md 6.1).

    `pr_gates` names gate id prefixes (the part before `:`) whose approval is meant
    to come from a code review platform's own PR review, not a decision file. Until
    the `github` review adapter (M2-10) exists, a PR gate here is `approved` only
    when a current `approve` decision has been recorded through `review` for it
    (there is no PR-status source yet), and `waiting` otherwise; it is never derived
    from a real PR's review state.
    """

    def __init__(
        self,
        review: ReviewAdapter,
        store: ArtifactStore,
        *,
        pr_gates: Collection[str] = (),
    ) -> None:
        self.review = review
        self.store = store
        self.pr_gates = frozenset(pr_gates)

    def status(self, gate_id: str, instance: RuleInstance) -> GateStatus:
        """Return the current status of `gate_id` for `instance`.

        `gate_id` is assumed already formatted with `instance`'s params (the
        scheduler does that with `gate_id_for`). The gate's covered artifacts are
        `instance`'s file-based inputs.
        """
        current = self.store.current_hashes(_file_inputs(instance))
        decisions = self._current_decisions(gate_id, current)

        prefix = gate_id.split(":", 1)[0]
        if prefix in self.pr_gates:
            has_approve = any(decision.decision == "approve" for decision in decisions)
            return "approved" if has_approve else "waiting"

        if not decisions:
            return "waiting"
        latest = decisions[-1]
        if latest.decision in ("approve", "baseline"):
            return "approved"
        if latest.decision == "reject":
            return "rejected"
        return "waiting"

    def _current_decisions(self, gate_id: str, current: Mapping[str, str]) -> list[Approval]:
        current_keys = frozenset(current)
        decisions = [
            approval
            for approval in self.review.approvals(gate_id)
            if approval.decision != "waive"
            and approval.is_current(current)
            and current_keys.issubset(approval.artifact_hashes.keys())
        ]
        decisions.sort(key=lambda approval: approval.at)
        return decisions


def approve(
    review: ReviewAdapter,
    store: ArtifactStore,
    gate_id: str,
    instance: RuleInstance,
    *,
    by: str,
    decision: Literal["approve", "reject"] = "approve",
    note: str = "",
) -> Approval:
    """Record an approve/reject decision for `gate_id` over `instance`'s file inputs.

    Backs `chipgraph approve` (CLI in M0-12). Raises `GateError` if `instance` has no
    file-based inputs (there is nothing on disk to pin the decision's hashes to).
    """
    file_inputs = _file_inputs(instance)
    if not file_inputs:
        raise GateError(
            f"cannot record a decision for gate {gate_id!r}: instance "
            f"{instance.instance_id!r} has no file-based inputs"
        )
    hashes = store.current_hashes(file_inputs)
    if not hashes:
        raise GateError(
            f"cannot record a decision for gate {gate_id!r}: none of instance "
            f"{instance.instance_id!r}'s file inputs exist on disk"
        )
    approval = Approval(
        gate_id=gate_id,
        by=by,
        at=datetime.now(UTC),
        artifact_hashes=hashes,
        decision=decision,
        note=note,
    )
    review.record(approval)
    return approval


def baseline(
    review: ReviewAdapter,
    store: ArtifactStore,
    gate_id: str,
    refs: Iterable[ArtifactRef],
    *,
    by: str,
    note: str = "",
) -> Approval:
    """Record a `baseline` decision over `refs`' current hashes (DESIGN.md 6.4).

    The full `chipgraph baseline` command arrives in M1-24; this is the primitive it
    calls once per gate. Raises `GateError` if none of `refs` exist on disk.
    """
    hashes = store.current_hashes(refs)
    if not hashes:
        raise GateError(f"cannot baseline gate {gate_id!r}: none of the given refs exist on disk")
    approval = Approval(
        gate_id=gate_id,
        by=by,
        at=datetime.now(UTC),
        artifact_hashes=hashes,
        decision="baseline",
        note=note,
    )
    review.record(approval)
    return approval
