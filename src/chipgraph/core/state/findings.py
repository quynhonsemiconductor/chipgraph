"""The finding store, and how a waiver's currency is computed (DESIGN.md 4.8, 6.1).

Findings are machine-local state, not a repo artifact (DESIGN.md 6.1 "State chạy máy"):
one JSON file per `fingerprint` under ``<state_dir>/findings/``. A waiver, by contrast,
is a person's decision and lives in ``.chipgraph/decisions/`` as an `Approval` (through
the `file` review adapter); this module only defines what a waiver *is* -- an
`Approval` with `decision == "waive"`, bound by `gate_id` to a finding's id -- and how to
tell whether one is still current. Actually writing a waiver decision (which needs the
`ReviewAdapter`) is an app-layer concern (`chipgraph.app.findings.waive`).
"""

from __future__ import annotations

import builtins
import os
from collections.abc import Iterable, Mapping
from pathlib import Path

from chipgraph.core.contracts import Approval
from chipgraph.core.contracts.finding import Finding, FindingSeverity, FindingStatus
from chipgraph.core.state.layout import StateLayout


def waiver_gate_id(finding_id: str) -> str:
    """The `Approval.gate_id` a waiver for `finding_id` is recorded (and looked up) under."""
    return f"finding:{finding_id}"


def effective_status(
    finding: Finding, waivers: Iterable[Approval], current_hashes: Mapping[str, str]
) -> FindingStatus:
    """The finding's status right now, resolving waiver currency against `current_hashes`.

    - `"fixed"` if the stored finding is already marked fixed.
    - `"waived"` only if at least one of `waivers` has `decision == "waive"` and is
      still current: every artifact hash it is bound to still matches `current_hashes`
      (`Approval.is_current`). A waiver bound to a hash that no longer matches -- the
      artifact changed -- no longer counts: the finding is reported `"open"` again, and
      the expiry is visible to the caller by simply not finding a current waiver.
    - `"open"` otherwise.
    """
    if finding.status == "fixed":
        return "fixed"
    for waiver in waivers:
        if waiver.decision == "waive" and waiver.is_current(current_hashes):
            return "waived"
    return "open"


class FindingStore:
    """Stores `Finding`s as one JSON file per fingerprint, under the state backend."""

    def __init__(self, layout: StateLayout) -> None:
        self.layout = layout
        self._dir = layout.state_dir / "findings"

    def _path(self, fingerprint: str) -> Path:
        return self._dir / f"{fingerprint}.json"

    def get(self, fingerprint: str) -> Finding | None:
        """Return the stored finding for `fingerprint`, or `None` if there is none."""
        path = self._path(fingerprint)
        if not path.is_file():
            return None
        return Finding.model_validate_json(path.read_text(encoding="utf-8"))

    def get_by_id(self, finding_id: str) -> Finding | None:
        """Return the stored finding whose `id` is `finding_id`, or `None`."""
        for finding in self.list():
            if finding.id == finding_id:
                return finding
        return None

    def _put(self, finding: Finding) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(finding.fingerprint)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(finding.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp_path, path)

    def upsert(self, findings: Iterable[Finding]) -> list[Finding]:
        """Insert or refresh `findings`, keyed by `fingerprint`.

        `last_seen` is taken from each incoming `finding` as given (the caller decides
        what "now" means, e.g. a run's start time). For a finding already on disk:
        `first_seen` is kept from the existing record, and `status` is kept as
        `"waived"` if it already was (a waiver survives re-detection; its currency is
        decided separately by `effective_status`), otherwise the incoming finding's
        status is used (so a `"fixed"` finding that is detected again goes back to
        `"open"`).
        """
        stored: list[Finding] = []
        for finding in findings:
            existing = self.get(finding.fingerprint)
            if existing is not None:
                status = "waived" if existing.status == "waived" else finding.status
                finding = finding.model_copy(
                    update={"first_seen": existing.first_seen, "status": status}
                )
            self._put(finding)
            stored.append(finding)
        return stored

    def list(
        self,
        *,
        status: FindingStatus | None = None,
        layer: int | None = None,
        severity: FindingSeverity | None = None,
        source: str | None = None,
    ) -> list[Finding]:
        """Return every stored finding matching the given filters, sorted by `id`."""
        if not self._dir.is_dir():
            return []
        results: list[Finding] = []
        for path in sorted(self._dir.glob("*.json")):
            finding = Finding.model_validate_json(path.read_text(encoding="utf-8"))
            if status is not None and finding.status != status:
                continue
            if layer is not None and finding.layer != layer:
                continue
            if severity is not None and finding.severity != severity:
                continue
            if source is not None and finding.source != source:
                continue
            results.append(finding)
        results.sort(key=lambda f: f.id)
        return results

    def mark_fixed(
        self, seen_fingerprints: Iterable[str], *, source: str
    ) -> builtins.list[Finding]:
        """Mark open findings from `source` as `"fixed"` when not in `seen_fingerprints`.

        Meant to be called after a full run of `source` over the whole project (e.g.
        `/audit`, M1-20): any of that source's open findings not rediscovered in this
        run are no longer present, so they are fixed. Returns the findings that were
        just marked fixed.
        """
        seen = set(seen_fingerprints)
        fixed: builtins.list[Finding] = []
        for finding in self.list(source=source, status="open"):
            if finding.fingerprint in seen:
                continue
            updated = finding.model_copy(update={"status": "fixed"})
            self._put(updated)
            fixed.append(updated)
        return fixed


__all__ = ["FindingStore", "effective_status", "waiver_gate_id"]
