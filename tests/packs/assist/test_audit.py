"""Tests for `/audit` (task M1-20): whole-project audit grouped by layer.

Accept criterion (seeded on a throwaway git copy of `examples/tinysoc`): with errors in
layers 1, 4 and 5 at once, `run_audit` finds all three, each in the right layer, with
evidence; an unmodified tinysoc gives no open errors. Plus: a waived finding shows as
waived and not blocking; after `chipgraph baseline --confirm` existing findings show as
baselined and not blocking; `--strict` exit codes (in `test_audit_cli.py`); the audit
writes only state, so `git status` stays clean.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from audit_helpers import (
    commit_all,
    copy_tinysoc,
    git_status,
    load_ctx,
    seed_layer1_overlap,
    seed_layer4_lint,
    seed_layer5_trace,
)

from chipgraph.app import findings as findings_app
from chipgraph.app.baseline import plan_project_baseline, run_baseline
from chipgraph.core.state.findings import FindingStore
from chipgraph.packs.assist.audit import AuditReport, run_audit

_verilator_missing = shutil.which("verilator") is None or shutil.which("make") is None
requires_eda = pytest.mark.skipif(
    _verilator_missing, reason="verilator and/or make not found on PATH"
)


def _layer(report: AuditReport, layer: int) -> object:
    for entry in report.layers:
        if entry.layer == layer:
            return entry
    raise AssertionError(f"no layer {layer} in report; layers: {[e.layer for e in report.layers]}")


def _finding_in_layer(report: AuditReport, layer: int, source_contains: str) -> object:
    for finding in _layer(report, layer).findings:  # type: ignore[attr-defined]
        if source_contains in finding.source:
            return finding
    raise AssertionError(f"no {source_contains!r} finding in layer {layer}")


# --- clean project: no open errors --------------------------------------------------


@requires_eda
def test_clean_tinysoc_has_no_open_errors(tmp_path: Path) -> None:
    root = copy_tinysoc(tmp_path / "tinysoc")
    report = run_audit(load_ctx(root))

    assert report.open_errors_by_layer == {}
    assert report.blocking == 0
    assert not report.has_blocking
    # Every check ran and none errored (all tools present, model built).
    assert {c.status for c in report.checks} <= {"pass", "skipped"}
    assert git_status(root) == ""


# --- the accept criterion: errors in layers 1, 4 and 5 ------------------------------


@requires_eda
def test_seeded_errors_found_in_the_right_layers(tmp_path: Path) -> None:
    root = copy_tinysoc(tmp_path / "tinysoc")
    seed_layer1_overlap(root)
    seed_layer4_lint(root)
    seed_layer5_trace(root)

    report = run_audit(load_ctx(root))

    # Layer 1: cross_chip address overlap, with evidence pointing at chip.yml.
    l1 = _finding_in_layer(report, 1, "cross_chip")
    assert l1.severity == "error"  # type: ignore[attr-defined]
    assert l1.evidence.startswith("chip.yml")  # type: ignore[attr-defined]
    assert l1.blocking  # type: ignore[attr-defined]

    # Layer 4: a real Verilator lint error, evidence at the injected line.
    l4 = _finding_in_layer(report, 4, "lint")
    assert l4.severity == "error"  # type: ignore[attr-defined]
    assert l4.evidence.startswith("rtl/tiny_gpio.sv:")  # type: ignore[attr-defined]
    assert l4.blocking  # a lint error blocks a strict audit  # type: ignore[attr-defined]

    # Layer 5: trace, a declared REQ with no test.
    l5 = _finding_in_layer(report, 5, "trace")
    assert l5.severity == "error"  # type: ignore[attr-defined]
    assert "TINY_TIMER_MAS.md" in l5.evidence  # type: ignore[attr-defined]
    assert l5.blocking  # type: ignore[attr-defined]

    # All three layers report exactly one open error; total blocking is three.
    assert report.open_errors_by_layer == {"1": 1, "4": 1, "5": 1}
    assert report.blocking == 3
    assert report.has_blocking

    # The report is JSON-dumpable and round-trips.
    dumped = report.model_dump(mode="json")
    assert AuditReport.model_validate(dumped) == report

    # State-only: the seeded edits are the only working-tree changes, no .chipgraph state.
    status = git_status(root)
    assert "chip.yml" in status
    assert ".chipgraph" not in status


# --- an errored check is visible ----------------------------------------------------


def test_a_check_that_errors_is_listed(tmp_path: Path) -> None:
    """Without a model, the model-based checks error; the audit lists them, not silent."""
    root = copy_tinysoc(tmp_path / "tinysoc")
    # Skip ingest, so the cross checks have no Design Model and report a whole-check error.
    report = run_audit(load_ctx(root), ingest=False)

    errored = [c for c in report.checks if c.status == "error"]
    assert errored, "expected at least one check to error without a model"
    assert any(c.check_id == "cross_chip" for c in errored)
    assert report.ingest.ran is False


# --- deterministic ------------------------------------------------------------------


@requires_eda
def test_report_is_deterministic(tmp_path: Path) -> None:
    root = copy_tinysoc(tmp_path / "tinysoc")
    seed_layer1_overlap(root)
    seed_layer5_trace(root)

    first = run_audit(load_ctx(root))
    second = run_audit(load_ctx(root))
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


# --- waived findings are not blocking -----------------------------------------------


@requires_eda
def test_waived_finding_shows_waived_and_not_blocking(tmp_path: Path) -> None:
    root = copy_tinysoc(tmp_path / "tinysoc")
    seed_layer1_overlap(root)

    ctx = load_ctx(root)
    report = run_audit(ctx)
    l1 = _finding_in_layer(report, 1, "cross_chip")
    assert l1.blocking  # type: ignore[attr-defined]

    # Waive it, then re-audit: it is listed as waived and no longer blocking.
    store = FindingStore(ctx.layout)
    finding = store.get_by_id(l1.id)  # type: ignore[attr-defined]
    assert finding is not None
    findings_app.waive(
        finding,
        by="tester",
        reason="known, tracked separately",
        store=store,
        review=ctx.review,
        current_hashes=ctx.store.current_hashes(finding.artifacts),
    )

    report2 = run_audit(load_ctx(root))
    l1b = _finding_in_layer(report2, 1, "cross_chip")
    assert l1b.status == "waived"  # type: ignore[attr-defined]
    assert not l1b.blocking  # type: ignore[attr-defined]
    assert report2.blocking == 0
    assert report2.open_errors_by_layer == {}


# --- baselined findings are recorded, listed, not blocking (DESIGN 6.4) -------------


@requires_eda
def test_baselined_findings_are_not_blocking(tmp_path: Path) -> None:
    root = copy_tinysoc(tmp_path / "tinysoc")
    seed_layer1_overlap(root)
    seed_layer5_trace(root)
    # A baseline pins clean, tracked artifacts on the main branch, so commit the seeds.
    commit_all(root, "seed layer 1 and 5 errors")

    ctx = load_ctx(root)
    run_audit(ctx)  # records the findings in the store

    # Baseline: record every open finding as baselined (DESIGN 6.4).
    plan, refs_by_path = plan_project_baseline(ctx)
    outcome = run_baseline(ctx, plan, refs_by_path, by="lead", note="baseline audit setup")
    assert outcome.findings_baselined, "expected findings to be baselined"

    report = run_audit(load_ctx(root))
    l1 = _finding_in_layer(report, 1, "cross_chip")
    l5 = _finding_in_layer(report, 5, "trace")
    assert l1.status == "baselined"  # type: ignore[attr-defined]
    assert l5.status == "baselined"  # type: ignore[attr-defined]
    assert not l1.blocking and not l5.blocking  # type: ignore[attr-defined]
    assert report.blocking == 0
    # Baselined findings are still listed; they just do not count as open errors.
    assert report.open_errors_by_layer == {}
    assert len(_layer(report, 1).findings) >= 1  # type: ignore[attr-defined]


# --- --block scoping ----------------------------------------------------------------


@requires_eda
def test_block_scopes_the_audit(tmp_path: Path) -> None:
    root = copy_tinysoc(tmp_path / "tinysoc")
    seed_layer4_lint(root)  # breaks gpio (and top, which includes gpio's RTL)

    # Scope to timer only: the gpio lint error must not appear.
    report = run_audit(load_ctx(root), blocks=["timer"])
    lint_checks = [c for c in report.checks if c.check_id == "lint"]
    assert {c.block for c in lint_checks} == {"timer"}
    assert report.blocking == 0
