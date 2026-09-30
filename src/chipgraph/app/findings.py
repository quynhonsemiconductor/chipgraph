"""App-layer glue for findings: `CheckResult` -> `Finding`, and waiving a finding.

Core (`chipgraph.core.contracts.finding`, `chipgraph.core.state.findings`) defines what a
finding and a waiver are; this module wires them to the rest of the app: mapping a check
id to a DESIGN.md 4.8 layer, turning a check's `Issue`s into `Finding`s, and recording a
waiver decision through the `ReviewAdapter` (`chipgraph.adapters.review.file.FileReview`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

from chipgraph.app.errors import AppError
from chipgraph.core.contracts import Approval, ArtifactRef, CheckResult
from chipgraph.core.contracts.finding import Evidence, Finding, FindingSeverity
from chipgraph.core.plugin_api.protocols import ReviewAdapter
from chipgraph.core.state.artifacts import ArtifactStore
from chipgraph.core.state.findings import FindingStore, waiver_gate_id

# Layer per DESIGN.md 4.8's table, keyed by check id (falling back to capability, then to
# layer 1, "cấu trúc và số liệu", which is where most cross-artifact checks land). Checks
# not listed here (including ones not yet built, e.g. M1-07/M1-19) default to layer 1;
# add them here as they are built if they belong to a different layer.
LAYER_BY_CHECK: Mapping[str, int] = {
    # layer 1: structural/deterministic cross-checks
    "spec_schema": 1,
    "cross_chip": 1,
    "ports_diff": 1,
    "hardcode": 1,
    "duplicate": 1,
    "connect": 1,
    "cdc_struct": 1,
    # layer 4: lint / synth "smells"
    "lint": 4,
    "layout": 4,
    "synth": 4,
    "naming": 4,
    # layer 5: process (traceability, staleness, gate/approval hygiene)
    "trace": 5,
}
_DEFAULT_LAYER = 1


def layer_for_check(check_id: str, capability: str = "") -> int:
    """The DESIGN.md 4.8 layer a check's findings belong to, defaulting to layer 1."""
    if check_id in LAYER_BY_CHECK:
        return LAYER_BY_CHECK[check_id]
    if capability in LAYER_BY_CHECK:
        return LAYER_BY_CHECK[capability]
    return _DEFAULT_LAYER


def _finding_severity(issue_severity: str) -> FindingSeverity:
    # `Issue.severity` is "error" | "warning" | "info"; `Finding.severity` has no "info".
    if issue_severity == "info":
        return "warning"
    if issue_severity == "error":
        return "error"
    return "warning"


def findings_from_check(
    result: CheckResult,
    *,
    layer: int = 1,
    store: ArtifactStore | None = None,
    run_id: str | None = None,
) -> list[Finding]:
    """Build one `Finding` per `Issue` in `result`.

    An issue with a `file` gets that as its evidence's `file`/`line`; if `store` is
    given, the file's current content hash is recorded in `artifacts`/`artifact_hashes`
    (what a waiver against this finding would bind to). An issue with no `file` (e.g. a
    whole-check configuration error) has nowhere else to point at, so its evidence uses
    the check id itself as a Design-Model-style key: `model_key="check:<check_id>"`.

    All findings from a check are deterministic (`source="check:<check_id>"`), so they
    may carry `severity="error"`.
    """
    now = datetime.now(UTC)
    findings: list[Finding] = []
    for issue in result.issues:
        if issue.file is not None:
            evidence = Evidence(file=issue.file, line=issue.line, note=issue.rule)
            artifacts: tuple[ArtifactRef, ...] = (ArtifactRef(kind="other", path=issue.file),)
            artifact_hashes = store.current_hashes(artifacts) if store is not None else {}
        else:
            evidence = Evidence(model_key=f"check:{result.check_id}", note=issue.rule)
            artifacts = ()
            artifact_hashes = {}

        findings.append(
            Finding(
                layer=layer,
                severity=_finding_severity(issue.severity),
                source=f"check:{result.check_id}",
                evidence=(evidence,),
                claim=issue.msg,
                artifacts=artifacts,
                artifact_hashes=artifact_hashes,
                first_seen=now,
                last_seen=now,
                run_id=run_id,
            )
        )
    return findings


ABSENT_HASH = hashlib.sha256(b"chipgraph:absent-file").hexdigest()
"""The hash a waiver records for a bound file that does not exist yet.

When the file appears, its real hash differs, so the waiver stops being current."""


def bound_hashes(store: ArtifactStore, paths: Iterable[str]) -> dict[str, str]:
    """`{"<repo>:<path>": hash}` for `paths`, with `ABSENT_HASH` for a missing file."""
    refs = [ArtifactRef(kind="other", path=path) for path in paths]
    present = store.current_hashes(refs)
    return {
        f"{ref.repo}:{ref.path}": present.get(f"{ref.repo}:{ref.path}", ABSENT_HASH) for ref in refs
    }


def finding_current_hashes(
    store: ArtifactStore, finding: Finding, waivers: Iterable[Approval]
) -> dict[str, str]:
    """The current hashes to judge `finding`'s waivers against.

    The finding's own artifacts, plus every file a waiver was bound to with `--bind`
    (a missing one hashes to `ABSENT_HASH`), so a waiver on a whole-check finding
    expires when a bound file appears or changes.
    """
    current = store.current_hashes(finding.artifacts)
    extra = sorted(
        key.partition(":")[2]
        for waiver in waivers
        for key in waiver.artifact_hashes
        if key not in current and key.partition(":")[2]
    )
    current.update(bound_hashes(store, extra))
    return current


def waive(
    finding: Finding,
    *,
    by: str,
    reason: str,
    store: FindingStore,
    review: ReviewAdapter,
    current_hashes: Mapping[str, str] | None = None,
) -> Finding:
    """Waive `finding`: record a `waive` decision through `review`, bound to its artifacts.

    `reason` is required (an empty reason is refused) and is stored as the decision's
    `note`. The waiver's `artifact_hashes` come from `finding.artifact_hashes` (its
    hashes at detection time), unless `current_hashes` is given, in which case the
    waiver binds to `finding.artifacts`' *current* hashes instead (waiving as of now,
    not as of whenever the finding was first recorded). A finding with no file (a
    whole-check error) is bound with `current_hashes` from `bound_hashes` instead (the
    CLI's `--bind`). Raises `ValueError` if `reason` is blank, or `AppError` if there is
    no artifact hash to bind a waiver to.

    Also updates the finding's stored `status` to `"waived"` in `store`, for display
    convenience; `chipgraph.core.state.findings.effective_status` remains the source of
    truth for whether the waiver is still current.
    """
    if not reason.strip():
        raise ValueError("a waiver requires a non-empty reason")

    hashes = dict(current_hashes) if current_hashes else dict(finding.artifact_hashes)
    if not hashes:
        raise AppError(
            f"cannot waive finding {finding.id!r}: it has no file to bind the waiver to; "
            "name the files whose change should end the waiver with --bind PATH"
        )

    approval = Approval(
        gate_id=waiver_gate_id(finding.id),
        by=by,
        at=datetime.now(UTC),
        artifact_hashes=hashes,
        decision="waive",
        note=reason,
    )
    review.record(approval)

    updated = finding.model_copy(update={"status": "waived"})
    (stored,) = store.upsert((updated,))
    return stored


__all__ = [
    "ABSENT_HASH",
    "LAYER_BY_CHECK",
    "bound_hashes",
    "finding_current_hashes",
    "findings_from_check",
    "layer_for_check",
    "waive",
]
