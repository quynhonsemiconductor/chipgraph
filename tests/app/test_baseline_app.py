"""Tests for `chipgraph.app.baseline`: planning and confirming a baseline on tinysoc.

Each test works on a fresh git copy of `examples/tinysoc` in `tmp_path`. The example
ships `baseline` decisions under `.chipgraph/decisions/`; these tests remove them first
so they can show the "before baseline" state (gates waiting) and record the baseline
themselves.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from chipgraph.app.baseline import (
    current_branch,
    main_branch,
    plan_project_baseline,
    run_baseline,
)
from chipgraph.app.build import make_scheduler
from chipgraph.app.context import AppContext

_EXAMPLE_ROOT = Path(__file__).resolve().parents[2] / "examples" / "tinysoc"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


def _copy_tinysoc(dest: Path, *, keep_decisions: bool = False) -> Path:
    shutil.copytree(_EXAMPLE_ROOT, dest)
    if not keep_decisions:
        shutil.rmtree(dest / ".chipgraph" / "decisions", ignore_errors=True)
    subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    _git(dest, "config", "user.email", "t@example.invalid")
    _git(dest, "config", "user.name", "t")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "initial import")
    return dest


def _gate_status(ctx: AppContext, gate_id: str, instance_id: str) -> str:
    scheduler = make_scheduler(ctx, "*")
    instance = scheduler.graph.instances[instance_id]
    return ctx.gates.status(gate_id, instance)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _copy_tinysoc(tmp_path / "tinysoc")


def test_main_branch_defaults_to_main_without_origin(repo: Path) -> None:
    assert main_branch(repo) == "main"
    assert current_branch(repo) in ("main", "master")


def test_plan_lists_gates_with_artifacts_and_commits(repo: Path) -> None:
    ctx = AppContext.load(repo)
    plan, refs_by_path = plan_project_baseline(ctx)

    gate_ids = {g.gate_id for g in plan.gates}
    assert {"spec:timer", "spec:gpio", "spec:top"} <= gate_ids

    timer = next(g for g in plan.gates if g.gate_id == "spec:timer")
    paths = {a.path for a in timer.artifacts}
    assert "doc/specs/TINY_TIMER_MAS.md" in paths
    assert "filelists/timer.f" in paths
    # Every committed artifact is clean and records the last commit that touched it.
    mas = next(a for a in timer.artifacts if a.path == "doc/specs/TINY_TIMER_MAS.md")
    assert mas.status == "clean"
    assert mas.sha256 is not None
    assert mas.last_commit is not None
    assert mas.last_commit.subject == "initial import"

    # `top` has no MAS: its gate covers only the filelist.
    top = next(g for g in plan.gates if g.gate_id == "spec:top")
    assert [a.path for a in top.clean_artifacts] == ["filelists/top.f"]

    assert refs_by_path["doc/specs/TINY_TIMER_MAS.md"].path == "doc/specs/TINY_TIMER_MAS.md"


def test_before_baseline_gate_is_waiting(repo: Path) -> None:
    ctx = AppContext.load(repo)
    assert _gate_status(ctx, "spec:timer", "tinysoc/rtl[block=timer]") == "waiting"


def test_confirm_records_a_decision_per_gate_and_is_idempotent(repo: Path) -> None:
    ctx = AppContext.load(repo)
    plan, refs_by_path = plan_project_baseline(ctx)
    outcome = run_baseline(ctx, plan, refs_by_path, by="lead")

    recorded = {a.gate_id for a in outcome.approvals}
    assert {"spec:timer", "spec:gpio", "spec:top"} == recorded
    for approval in outcome.approvals:
        assert approval.decision == "baseline"
        assert approval.by == "lead"

    # After baseline the gate is approved.
    ctx2 = AppContext.load(repo)
    assert _gate_status(ctx2, "spec:timer", "tinysoc/rtl[block=timer]") == "approved"

    # Running again records nothing new.
    plan2, refs2 = plan_project_baseline(ctx2)
    outcome2 = run_baseline(ctx2, plan2, refs2, by="lead")
    assert outcome2.approvals == ()


def test_editing_the_mas_returns_the_gate_to_waiting(repo: Path) -> None:
    ctx = AppContext.load(repo)
    plan, refs_by_path = plan_project_baseline(ctx)
    run_baseline(ctx, plan, refs_by_path, by="lead")
    assert _gate_status(AppContext.load(repo), "spec:timer", "tinysoc/rtl[block=timer]") == (
        "approved"
    )

    mas = repo / "doc" / "specs" / "TINY_TIMER_MAS.md"
    mas.write_text(mas.read_text() + "\n<!-- edited after baseline -->\n")
    assert _gate_status(AppContext.load(repo), "spec:timer", "tinysoc/rtl[block=timer]") == (
        "waiting"
    )


def test_dirty_artifact_is_listed_and_not_baselined(repo: Path) -> None:
    mas = repo / "doc" / "specs" / "TINY_TIMER_MAS.md"
    mas.write_text(mas.read_text() + "\n<!-- uncommitted edit -->\n")

    ctx = AppContext.load(repo)
    plan, refs_by_path = plan_project_baseline(ctx)
    dirty_paths = {a.path for a in plan.dirty_artifacts}
    assert "doc/specs/TINY_TIMER_MAS.md" in dirty_paths

    outcome = run_baseline(ctx, plan, refs_by_path, by="lead")
    # The timer gate still records a baseline over its *clean* input (the filelist),
    # but not over the dirty MAS.
    timer_approval = next(a for a in outcome.approvals if a.gate_id == "spec:timer")
    assert "doc/specs/TINY_TIMER_MAS.md" not in timer_approval.artifact_hashes
    assert ".:filelists/timer.f" in timer_approval.artifact_hashes

    # The gate is not fully approved: it does not cover the still-changing MAS.
    assert _gate_status(AppContext.load(repo), "spec:timer", "tinysoc/rtl[block=timer]") == (
        "waiting"
    )
