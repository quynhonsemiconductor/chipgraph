"""`/audit` (task M1-20): run every deterministic check over the whole project.

`run_audit` runs, once, every check the project's profile configures -- the same
per-block / chip-wide split as `chipgraph check` -- through the shared
`chipgraph.app.checks.ProfileCheckRunner`, so their findings land in the finding store
exactly as any other check run would. It then reads the store back and builds an
`AuditReport` that groups every finding by DESIGN.md 4.8 layer and severity, records each
check's status (so a check that *errored*, e.g. a missing tool, is visible, not silent),
and folds in the ingest issues it ran first (the model checks need a model).

The report is pure data (pydantic, JSON-dumpable, deterministic order) and never touches
the repo tree: `run_audit` writes only machine-local state (the finding store and the
model cache). `chipgraph audit` prints it and the `audit` MCP tool returns it; `chipgraph
try` (M1-21) reuses `run_audit` as its read-only whole-project pass.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from chipgraph.app.baseline import baseline_gate_id
from chipgraph.app.checks import ProfileCheckRunner
from chipgraph.app.context import AppContext
from chipgraph.app.findings import layer_for_check
from chipgraph.app.ingest import run_ingest
from chipgraph.core.contracts import ArtifactRef, CheckResult, RuleInstance
from chipgraph.core.contracts.finding import Finding, FindingSeverity
from chipgraph.core.state.findings import FindingStore, effective_status, waiver_gate_id

# The layers whose *open* errors block a strict audit (DESIGN.md 4.8: only deterministic
# layers may report `error`; a strict `/audit` treats layers 1 and 5 as hard, and layer 4
# only for lint errors -- a design "smell" that is a real error, not a warning).
_HARD_LAYERS = frozenset({1, 5})
_LINT_LAYER = 4
# A lint error is reported by the `lint` check (`check:lint`) or, in principle, directly
# by the pyslang extractor (`lint`); either counts as a blocking layer-4 error.
_LINT_SOURCES = frozenset({"lint", "check:lint"})

_SEVERITY_ORDER = {"error": 0, "warning": 1, "question": 2}
_STATUS_ORDER = {"open": 0, "waived": 1, "baselined": 2, "fixed": 3}

# A finding's audit status is its effective status, plus `"baselined"`: an open finding
# with a current `baseline` decision (DESIGN.md 6.4 -- recorded, listed, never blocking).
AuditFindingStatus = Literal["open", "waived", "baselined", "fixed"]


class AuditFinding(BaseModel):
    """One finding as the audit reports it: its layer, severity, source and status."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(description="The finding id, 'F-<8 hex>'.")
    layer: int = Field(ge=1, le=5, description="DESIGN.md 4.8 layer (1..5).")
    severity: FindingSeverity = Field(description="How serious the finding is.")
    source: str = Field(description="Where it came from, e.g. 'check:cross_chip'.")
    status: AuditFindingStatus = Field(description="Audit status (adds 'baselined').")
    evidence: str = Field(description="First evidence location: 'file:line' or 'model:<key>'.")
    claim: str = Field(description="What is wrong, in plain language.")
    blocking: bool = Field(description="Whether this finding blocks a strict audit.")


class LayerReport(BaseModel):
    """Every finding at one layer, with counts by severity and status."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    layer: int = Field(ge=1, le=5, description="The DESIGN.md 4.8 layer.")
    by_severity: dict[str, int] = Field(
        default_factory=dict, description="Finding count keyed by severity."
    )
    by_status: dict[str, int] = Field(
        default_factory=dict, description="Finding count keyed by audit status."
    )
    open_errors: int = Field(default=0, description="Open (not waived/baselined) errors here.")
    blocking: int = Field(
        default=0, description="Findings at this layer that block a strict audit."
    )
    findings: tuple[AuditFinding, ...] = Field(
        default=(), description="The findings at this layer, ordered."
    )


class CheckReport(BaseModel):
    """One entry per check that was run: its status and how many issues it raised.

    A check that errored (`status='error'`, e.g. a missing tool or a missing model) is
    listed here so it is visible rather than silently contributing no findings.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    check_id: str = Field(description="The configured check id.")
    block: str | None = Field(
        default=None, description="The block it ran for, or None (chip-wide)."
    )
    status: Literal["pass", "fail", "error", "skipped"] = Field(description="The check's outcome.")
    layer: int = Field(ge=1, le=5, description="The layer its findings belong to.")
    issues: int = Field(ge=0, description="How many issues the check raised.")


class IngestSection(BaseModel):
    """The ingest run's own issues (the model checks need a model): counts and errors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ran: bool = Field(description="Whether ingest was run at all.")
    by_severity: dict[str, int] = Field(
        default_factory=dict, description="Ingest issue count keyed by severity."
    )
    errors: tuple[str, ...] = Field(
        default=(), description="The error-severity ingest issues, as messages."
    )


class AuditReport(BaseModel):
    """The whole-project audit: findings grouped by layer, per-check status, ingest section."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    ingest: IngestSection = Field(description="The ingest run's issues (run first).")
    checks: tuple[CheckReport, ...] = Field(
        default=(), description="Per-check status, in run order."
    )
    layers: tuple[LayerReport, ...] = Field(
        default=(), description="One entry per non-empty layer, ordered 1..5."
    )
    open_errors_by_layer: dict[str, int] = Field(
        default_factory=dict, description="Open error count keyed by layer number (as a string)."
    )
    blocking: int = Field(default=0, description="Total findings that block a strict audit.")

    @property
    def has_blocking(self) -> bool:
        """Whether any finding blocks a strict audit (open errors in layers 1/5, lint in 4)."""
        return self.blocking > 0

    def summary_line(self) -> str:
        """A one-line summary: open errors per layer and the blocking count."""
        if self.open_errors_by_layer:
            parts = ", ".join(
                f"L{layer}={self.open_errors_by_layer[layer]}"
                for layer in sorted(self.open_errors_by_layer, key=int)
            )
            errors = f"open errors: {parts}"
        else:
            errors = "open errors: none"
        return f"{errors}   blocking: {self.blocking}"


def _check_combos(
    runner: ProfileCheckRunner, ctx: AppContext, blocks: list[str] | None
) -> list[tuple[str, str | None]]:
    """The (check_id, block) combos to run, mirroring `chipgraph check`'s own rule.

    A chip-wide check (`per_block=False`) runs once for the whole project, unless the
    caller scoped the run to specific `blocks`; running it per block would repeat its
    issues and drop those that belong to no block.
    """
    resolved = ctx.require_profile()
    check_ids = sorted(resolved.profile.adapters)
    scoped = blocks is not None
    raw_blocks = list(blocks) if blocks is not None else sorted(resolved.profile.blocks)
    block_list: list[str | None] = list(raw_blocks) if raw_blocks else [None]
    return [
        (cid, b)
        for cid in check_ids
        for b in (block_list if scoped or runner.runs_per_block(cid) else [None])
    ]


def _fake_instance(check_id: str, block: str | None) -> RuleInstance:
    """A throwaway `RuleInstance` carrying just the params `ProfileCheckRunner` reads.

    Identical to `chipgraph.cli._fake_instance` (private there); `/audit` runs checks
    directly, outside the build graph, so there is no real rule instance to pass.
    """
    params = {"block": block} if block is not None else {}
    rule_id = "chipgraph/audit"
    safe = "".join(c if c.isalnum() else "_" for c in check_id)
    output_path = f".chipgraph/tmp/audit-{safe}-{block or 'all'}.json"
    return RuleInstance(
        rule_id=rule_id,
        params=params,
        outputs=(ArtifactRef(kind="report", path=output_path),),
        instance_id=RuleInstance.make_id(rule_id, params),
    )


async def _run_check_combos(
    runner: ProfileCheckRunner, combos: list[tuple[str, str | None]]
) -> list[CheckReport]:
    reports: list[CheckReport] = []
    for check_id, block in combos:
        instance = _fake_instance(check_id, block)
        result: CheckResult = await runner.run(check_id, instance)
        reports.append(
            CheckReport(
                check_id=check_id,
                block=block,
                status=result.status,
                layer=layer_for_check(check_id, result.check_id),
                issues=len(result.issues),
            )
        )
    return reports


def _audit_status(finding: Finding, ctx: AppContext) -> tuple[AuditFindingStatus, bool]:
    """A finding's audit status and whether it blocks a strict audit.

    Its effective status (`open`/`waived`/`fixed`) is computed against its current
    artifact hashes; an open finding is reported `"baselined"` instead when it has a
    current `baseline` decision (DESIGN.md 6.4). Only an `open` (not waived, not
    baselined) `error` in a hard layer -- layers 1 and 5, or a lint error in layer 4 --
    is blocking.
    """
    current_hashes = ctx.store.current_hashes(finding.artifacts)
    waivers = ctx.review.approvals(waiver_gate_id(finding.id))
    status: AuditFindingStatus = effective_status(finding, waivers, current_hashes)
    if status == "open" and _is_baselined(finding, ctx, current_hashes):
        status = "baselined"
    blocking = (
        status == "open"
        and finding.severity == "error"
        and (
            finding.layer in _HARD_LAYERS
            or (finding.layer == _LINT_LAYER and finding.source in _LINT_SOURCES)
        )
    )
    return status, blocking


def _is_baselined(finding: Finding, ctx: AppContext, current_hashes: dict[str, str]) -> bool:
    """Whether `finding` has a current `baseline` decision bound to its current hashes.

    Mirrors `chipgraph.app.baseline._baseline_open_findings`: a whole-check finding with
    no artifacts is baselined against its detection-time hashes instead.
    """
    hashes = dict(current_hashes) or dict(finding.artifact_hashes)
    if not hashes:
        return False
    decisions = ctx.review.approvals(baseline_gate_id(finding.id))
    return any(a.decision == "baseline" and a.is_current(hashes) for a in decisions)


def _first_evidence(finding: Finding) -> str:
    ev = finding.evidence[0]
    if ev.file is not None:
        return f"{ev.file}:{ev.line}" if ev.line is not None else ev.file
    return f"model:{ev.model_key}"


def _finding_sort_key(af: AuditFinding) -> tuple[int, int, str, str]:
    return (
        _SEVERITY_ORDER.get(af.severity, 9),
        _STATUS_ORDER.get(af.status, 9),
        af.source,
        af.id,
    )


def _build_layers(ctx: AppContext) -> tuple[list[LayerReport], dict[str, int], int]:
    """Read the finding store and group every finding by layer, then severity/status."""
    store = FindingStore(ctx.layout)
    by_layer: dict[int, list[AuditFinding]] = {}
    for finding in store.list():
        status, blocking = _audit_status(finding, ctx)
        af = AuditFinding(
            id=finding.id,
            layer=finding.layer,
            severity=finding.severity,
            source=finding.source,
            status=status,
            evidence=_first_evidence(finding),
            claim=finding.claim,
            blocking=blocking,
        )
        by_layer.setdefault(finding.layer, []).append(af)

    layers: list[LayerReport] = []
    open_errors_by_layer: dict[str, int] = {}
    total_blocking = 0
    for layer in sorted(by_layer):
        items = sorted(by_layer[layer], key=_finding_sort_key)
        by_severity: dict[str, int] = {}
        by_status: dict[str, int] = {}
        open_errors = 0
        layer_blocking = 0
        for af in items:
            by_severity[af.severity] = by_severity.get(af.severity, 0) + 1
            by_status[af.status] = by_status.get(af.status, 0) + 1
            if af.status == "open" and af.severity == "error":
                open_errors += 1
            if af.blocking:
                layer_blocking += 1
        if open_errors:
            open_errors_by_layer[str(layer)] = open_errors
        total_blocking += layer_blocking
        layers.append(
            LayerReport(
                layer=layer,
                by_severity=dict(sorted(by_severity.items())),
                by_status=dict(sorted(by_status.items())),
                open_errors=open_errors,
                blocking=layer_blocking,
                findings=tuple(items),
            )
        )
    return layers, open_errors_by_layer, total_blocking


def run_audit(
    ctx: AppContext, *, ingest: bool = True, blocks: list[str] | None = None
) -> AuditReport:
    """Run every deterministic check over the whole project and group the findings.

    Steps (DESIGN.md 4.8, 6.4):

    1. If `ingest`, run `run_ingest(ctx)` first (the model checks need a model); its
       issues are kept as their own report section (counts by severity + the errors).
    2. Run every check the profile's `adapters` configures over the whole project -- the
       same per-block / chip-wide split as `chipgraph check` -- through
       `ProfileCheckRunner`, so findings are recorded in the store as usual. When
       `blocks` is given, only those blocks are run (a chip-wide check runs once per
       requested block, matching `chipgraph check --block`).
    3. Build the report from the finding store: every finding with its layer, severity,
       source, evidence, claim and audit status, grouped by layer then severity, plus
       per-check status so a check that errored is visible.
    4. A finding is "blocking" when it is an open (not waived, not baselined) error in a
       hard layer (1 or 5) or an open lint error in layer 4. Baselined findings are
       listed but never blocking (DESIGN.md 6.4).

    Requires a loaded profile (raises `AppError` otherwise). Writes only machine-local
    state (the finding store and the model cache), never files in the repo tree.
    """
    ctx.require_profile()

    ingest_section = _run_ingest_section(ctx) if ingest else IngestSection(ran=False)

    runner = ProfileCheckRunner(ctx)
    combos = _check_combos(runner, ctx, blocks)
    check_reports = asyncio.run(_run_check_combos(runner, combos))

    layers, open_errors_by_layer, total_blocking = _build_layers(ctx)

    return AuditReport(
        ingest=ingest_section,
        checks=tuple(check_reports),
        layers=tuple(layers),
        open_errors_by_layer=open_errors_by_layer,
        blocking=total_blocking,
    )


def _run_ingest_section(ctx: AppContext) -> IngestSection:
    report = run_ingest(ctx)
    by_severity: dict[str, int] = {}
    errors: list[str] = []
    for issue in report.issues:
        by_severity[issue.severity] = by_severity.get(issue.severity, 0) + 1
        if issue.severity == "error":
            loc = f"{issue.file}:{issue.line}" if issue.file else (issue.block or "")
            errors.append(f"{loc} {issue.message}".strip() if loc else issue.message)
    return IngestSection(
        ran=True,
        by_severity=dict(sorted(by_severity.items())),
        errors=tuple(errors),
    )


__all__ = [
    "AuditFinding",
    "AuditFindingStatus",
    "AuditReport",
    "CheckReport",
    "IngestSection",
    "LayerReport",
    "run_audit",
]
