"""Wire `chipgraph baseline` to the graph, git, the review store and the finding store.

The generic planning lives in `chipgraph.core.engine.baseline`; this module supplies the
concrete facts it needs and applies the plan:

- it builds the graph for every target, collects each gated instance's gate and the
  file inputs it covers, and asks the `GateEvaluator` whether each gate is already
  decided;
- it answers `VcsFacts` (content hash, tracked/dirty, last commit) from the git working
  tree and history;
- on ``--confirm`` it records one `baseline` decision per undecided gate over its clean,
  tracked artifacts (through `core.engine.gate.baseline`) and marks every open finding as
  baselined, and it refuses to run on a branch other than the configured main branch.

`baseline` runs after `ingest`; the spec files ingest reads are ordinary file artifacts
here, gated by whatever rule consumes them.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from chipgraph.app.build import load_rules
from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.app.findings import finding_current_hashes
from chipgraph.core.contracts import Approval, ArtifactRef, Finding, RuleInstance
from chipgraph.core.engine import gate as gate_mod
from chipgraph.core.engine.baseline import (
    BaselinePlan,
    CommitInfo,
    GateInput,
    clean_refs_for_gate,
    gate_inputs_from_instances,
    plan_baseline,
)
from chipgraph.core.engine.graph import GraphError, build_graph
from chipgraph.core.engine.scheduler import _format_gate
from chipgraph.core.state.findings import FindingStore, effective_status, waiver_gate_id

__all__ = [
    "BaselineOutcome",
    "WrongBranchError",
    "baseline_gate_id",
    "current_branch",
    "main_branch",
    "plan_project_baseline",
    "run_baseline",
]

_DEFAULT_MAIN_BRANCH = "main"


def baseline_gate_id(finding_id: str) -> str:
    """The `Approval.gate_id` a finding's `baseline` decision is recorded under.

    Distinct from the waiver gate id (`finding:<id>`), so a baseline decision never
    reads as a waiver: `chipgraph findings` keeps listing the finding as open (recorded,
    non-blocking), which is what a baseline of existing findings means (DESIGN.md 6.4).
    """
    return f"baseline:finding:{finding_id}"


class WrongBranchError(AppError):
    """Raised when `baseline` runs on a branch other than the configured main branch."""


@dataclass(frozen=True, slots=True)
class BaselineOutcome:
    """What a `--confirm` run recorded: the decisions written and findings baselined."""

    approvals: tuple[Approval, ...]
    findings_baselined: tuple[str, ...]


# --- git facts ----------------------------------------------------------------------


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a read-only git command under `root`, capturing output (never a shell)."""
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def current_branch(root: Path) -> str | None:
    """The current branch name, or `None` when detached / not a git repo."""
    result = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    return None if name in ("", "HEAD") else name


def main_branch(root: Path) -> str:
    """The configured main branch: `origin/HEAD`'s target, falling back to `main`.

    Read from `git symbolic-ref refs/remotes/origin/HEAD` (e.g. `origin/main` -> `main`);
    if there is no such ref, the default `main` is used (DESIGN.md 6.4).
    """
    result = _git(root, "symbolic-ref", "refs/remotes/origin/HEAD")
    if result.returncode == 0:
        ref = result.stdout.strip()
        prefix = "refs/remotes/origin/"
        if ref.startswith(prefix):
            name = ref[len(prefix) :]
            if name:
                return name
    return _DEFAULT_MAIN_BRANCH


class _GitFacts:
    """`VcsFacts` answered from a git working tree and history under `root`.

    Tracked/dirty status and content hashes come from the working tree; `last_commit`
    reads `git log`. Results are cached per path so a listing that touches an artifact
    from several gates does not re-shell for it.
    """

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self._root = ctx.root
        self._sha: dict[str, str | None] = {}
        self._tracked: dict[str, bool] = {}
        self._dirty: dict[str, bool] = {}
        self._commit: dict[str, CommitInfo | None] = {}
        self._changed = self._collect_changed()

    def _collect_changed(self) -> frozenset[str]:
        result = _git(self._root, "status", "--porcelain=v1", "-z")
        paths: set[str] = set()
        for entry in result.stdout.split("\0"):
            if len(entry) > 3:
                paths.add(entry[3:])
        return frozenset(paths)

    def sha256(self, path: str) -> str | None:
        if path not in self._sha:
            ref = ArtifactRef(kind="other", path=path)
            hashes = self._ctx.store.current_hashes((ref,))
            self._sha[path] = hashes.get(f"{ref.repo}:{path}")
        return self._sha[path]

    def is_tracked(self, path: str) -> bool:
        if path not in self._tracked:
            result = _git(self._root, "ls-files", "--error-unmatch", "--", path)
            self._tracked[path] = result.returncode == 0
        return self._tracked[path]

    def is_dirty(self, path: str) -> bool:
        if path not in self._dirty:
            # Untracked paths appear in `git status` too; only a *tracked* modified path
            # is "dirty" here (an untracked one is reported by `is_tracked`).
            self._dirty[path] = path in self._changed and self.is_tracked(path)
        return self._dirty[path]

    def last_commit(self, path: str) -> CommitInfo | None:
        if path not in self._commit:
            self._commit[path] = self._read_last_commit(path)
        return self._commit[path]

    def _read_last_commit(self, path: str) -> CommitInfo | None:
        # %h short sha, %ad author date (short), %s subject, %P parents (to spot merges).
        # %P is last so an empty parent list (a root commit) just yields a trailing empty
        # field; parse with a fixed split count so that empty field is never lost.
        result = _git(
            self._root,
            "log",
            "-1",
            "--date=short",
            "--format=%h%x1f%ad%x1f%s%x1f%P",
            "--",
            path,
        )
        if result.returncode != 0:
            return None
        line = result.stdout.rstrip("\n")
        if not line:
            return None
        parts = line.split("\x1f", 3)
        if len(parts) < 3:
            return None
        short_sha, date, subject = parts[0], parts[1], parts[2]
        parents = parts[3] if len(parts) > 3 else ""
        is_merge = len(parents.split()) > 1
        return CommitInfo(short_sha=short_sha, date=date, subject=subject, is_merge=is_merge)


# --- planning -----------------------------------------------------------------------


def _gated_instances(ctx: AppContext) -> list[tuple[str, RuleInstance, bool]]:
    """Every gated instance across all targets: `(gate_id, instance, already_decided)`.

    The graph is built once over `"*"` (all targets). A rule with a `gate` yields one
    entry per instance, with the gate id formatted from the instance's params; whether
    the gate is already decided is asked of the project's `GateEvaluator`.
    """
    rules = load_rules(ctx)
    from chipgraph.app.build import _resolver_for

    try:
        graph = build_graph(rules, _resolver_for(ctx), repo=ctx.store.repo)
    except GraphError as exc:
        raise AppError(str(exc)) from exc

    gated: list[tuple[str, RuleInstance, bool]] = []
    seen: set[tuple[str, str]] = set()
    for iid in sorted(graph.instances):
        instance = graph.instances[iid]
        rule = graph.rules[instance.rule_id]
        if not rule.gate:
            continue
        gate_id = _format_gate(rule.gate, instance.params)
        key = (gate_id, iid)
        if key in seen:
            continue
        seen.add(key)
        already = ctx.gates.status(gate_id, instance) != "waiting"
        gated.append((gate_id, instance, already))
    return gated


def _refs_by_path(gated: Iterable[tuple[str, RuleInstance, bool]]) -> dict[str, ArtifactRef]:
    """Map each gated instance's file-input path to its `ArtifactRef` (repo/label kept)."""
    by_path: dict[str, ArtifactRef] = {}
    for _gate_id, instance, _decided in gated:
        for ref in instance.inputs:
            if ref.path is not None and ref.path not in by_path:
                by_path[ref.path] = ref
    return by_path


def _open_finding_count(ctx: AppContext) -> int:
    return len(_open_findings(ctx))


def _open_findings(ctx: AppContext) -> list[Finding]:
    store = FindingStore(ctx.layout)
    open_rows: list[Finding] = []
    for finding in store.list():
        waivers = ctx.review.approvals(waiver_gate_id(finding.id))
        current_hashes = finding_current_hashes(ctx.store, finding, waivers)
        if effective_status(finding, waivers, current_hashes) == "open":
            open_rows.append(finding)
    return open_rows


def plan_project_baseline(ctx: AppContext) -> tuple[BaselinePlan, dict[str, ArtifactRef]]:
    """Build the `BaselinePlan` for this project, plus a path -> `ArtifactRef` map.

    The map lets `run_baseline` pin a decision to the exact refs (with repo and label)
    a gate covers, not just their paths.
    """
    gated = _gated_instances(ctx)
    facts = _GitFacts(ctx)
    gate_inputs: list[GateInput] = gate_inputs_from_instances(gated)
    plan = plan_baseline(gate_inputs, facts, open_findings=_open_finding_count(ctx))
    return plan, _refs_by_path(gated)


# --- confirming ---------------------------------------------------------------------


def run_baseline(
    ctx: AppContext,
    plan: BaselinePlan,
    refs_by_path: dict[str, ArtifactRef],
    *,
    by: str,
    note: str = "",
) -> BaselineOutcome:
    """Record the plan: one `baseline` decision per undecided gate, mark findings baselined.

    Idempotent: a gate that is already decided (skipped) records nothing, so running this
    again after a first confirm writes nothing new. A gate with no clean, tracked artifact
    is skipped too (there is nothing on the main branch to pin). Returns what was written.
    """
    approvals: list[Approval] = []
    for gate in plan.gates:
        if not gate.will_baseline:
            continue
        refs = clean_refs_for_gate(gate, refs_by_path)
        if not refs:
            continue
        try:
            approval = gate_mod.baseline(
                ctx.review, ctx.store, gate.gate_id, refs, by=by, note=note
            )
        except gate_mod.GateError as exc:
            raise AppError(str(exc)) from exc
        approvals.append(approval)

    baselined = _baseline_open_findings(ctx, by=by, note=note)
    return BaselineOutcome(approvals=tuple(approvals), findings_baselined=tuple(baselined))


def _baseline_open_findings(ctx: AppContext, *, by: str, note: str) -> list[str]:
    """Record a `baseline` decision against every open finding, keyed by its id.

    Findings never block a build, so "baselined" here means *recorded, non-blocking,
    still listed*: a `baseline` `Approval` bound to the finding's current artifact hashes
    is written through the review store, distinct from a `waive` (so `chipgraph findings`
    still lists the finding as open). Idempotent per (finding, hashes): a finding whose
    artifacts have a current baseline decision is not recorded again.
    """
    recorded: list[str] = []
    now = datetime.now(UTC)
    for finding in _open_findings(ctx):
        gate_id = baseline_gate_id(finding.id)
        hashes = ctx.store.current_hashes(finding.artifacts)
        if not hashes:
            # No artifact to pin (e.g. a whole-check finding); still count it as recorded
            # via its detection-time hashes if it has any, else skip pinning.
            hashes = dict(finding.artifact_hashes)
        if not hashes:
            continue
        existing = ctx.review.approvals(gate_id)
        if any(a.decision == "baseline" and a.is_current(hashes) for a in existing):
            continue
        ctx.review.record(
            Approval(
                gate_id=gate_id,
                by=by,
                at=now,
                artifact_hashes=hashes,
                decision="baseline",
                note=note or "baselined: recorded, non-blocking",
            )
        )
        recorded.append(finding.id)
    return recorded
