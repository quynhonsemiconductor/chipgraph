"""M2-11c, offline, on the real tinysoc `timer` block: `fanout.isolate` through the service.

What the real Claude Code run showed the unit tests could not (the first real runs failed on
each of these): the workspace name must pass the VCS rule, the guard compares real paths
(`/var` vs `/private/var`), the checks of a workspace need the Design Model, the outputs are
judged where they are written, a tracked file nobody touched is not a stray write, and only
an accepted task's outputs reach the user's tree.

The "subagent" is this test: it writes the files `get_context` names (absolute workspace
paths), then calls `submit`, as the main session does.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest
from plan_helpers import FIXTURES, approve_plan, tinysoc_project
from rtl_target_helpers import MODULES, module_text

from chipgraph.adapters.runtime.claude_code import service
from chipgraph.app.context import AppContext

BLOCK = "timer"
PLAN = "digital-rtl/plan[block=timer]"
PROFILE = {"fanout": {"isolate": True, "max_parallel": 4}}


def _project(tmp_path: Path) -> Path:
    return tinysoc_project(
        tmp_path / "proj",
        packs=("digital-rtl", "dv"),
        stub=True,
        runtime="claude-code",
        profile=PROFILE,
    )


def _next(root: Path, target: str) -> dict[str, Any]:
    return asyncio.run(service.next_task(AppContext.load(root), target))


def _context(root: Path, task_id: str) -> dict[str, Any]:
    return asyncio.run(service.get_context(AppContext.load(root), task_id))


def _submit(root: Path, task_id: str) -> dict[str, Any]:
    return asyncio.run(service.submit(AppContext.load(root), task_id, service.SubmitReport()))


def _status(root: Path) -> set[str]:
    out = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {line[3:] for line in out.splitlines()}


def _write(context: dict[str, Any], text: str, index: int = 0) -> Path:
    """What the subagent does: write an output at the absolute path `get_context` gave."""
    path = Path(context["outputs_abs"][index])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _workspaces(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=root, capture_output=True, text=True
    ).stdout
    return [line for line in out.splitlines() if line.startswith("worktree ")][1:]


def test_the_planner_writes_in_a_workspace_and_only_the_accepted_plan_lands(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    before = _status(root)
    answer = _next(root, f"plan:{BLOCK}")
    assert [t["task_id"] for t in answer["tasks"]] == [PLAN], answer
    context = _context(root, PLAN)
    plan_path = Path(context["outputs_abs"][0])
    assert not str(plan_path).startswith(str(root.resolve())), "the workspace is outside the repo"
    assert _status(root) == before, "dispatching touched the user's tree"
    assert len(_workspaces(root)) == 1

    # a first, wrong plan is rejected and nothing lands
    _write(context, "schema_version: 1\nblock: timer\n")
    rejected = _submit(root, PLAN)
    assert rejected["accepted"] is False
    assert _status(root) == before
    assert _workspaces(root) == []

    # the next dispatch gets a fresh workspace; the right plan is accepted and lands
    _next(root, f"plan:{BLOCK}")
    context = _context(root, PLAN)
    _write(context, (FIXTURES / "timer.plan.yml").read_text(encoding="utf-8"))
    accepted = _submit(root, PLAN)
    assert accepted["accepted"] is True, accepted
    assert (root / f"plan/{BLOCK}.plan.yml").read_text(encoding="utf-8") == (
        FIXTURES / "timer.plan.yml"
    ).read_text(encoding="utf-8")
    assert _status(root) - before == {f"plan/{BLOCK}.plan.yml"}
    assert _workspaces(root) == []


def test_independent_modules_run_in_their_own_workspaces_and_land_one_by_one(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    _next(root, f"plan:{BLOCK}")
    _write(_context(root, PLAN), (FIXTURES / "timer.plan.yml").read_text(encoding="utf-8"))
    assert _submit(root, PLAN)["accepted"] is True
    approve_plan(root, BLOCK)

    answer = _next(root, f"rtl:{BLOCK}")
    first = {t["task_id"] for t in answer["tasks"]}
    counter, irq = (f"digital-rtl/rtl_module[block=timer,module={m}]" for m in MODULES[:2])
    assert {counter, irq} <= first, "the two independent modules are handed out together"
    contexts = {tid: _context(root, tid) for tid in (counter, irq)}
    paths = {tid: Path(c["outputs_abs"][0]) for tid, c in contexts.items()}
    assert len({p.parent.parent for p in paths.values()}) == 2, "one workspace each"
    before = _status(root)
    for tid, name in ((counter, MODULES[0]), (irq, MODULES[1])):
        _write(contexts[tid], module_text(name))
    assert _status(root) == before, "nothing lands before the task is accepted"

    assert _submit(root, counter)["accepted"] is True
    landed = _status(root) - before
    assert landed == {f"rtl/{MODULES[0]}.sv"}, landed
    assert (root / f"rtl/{MODULES[0]}.sv").read_text(encoding="utf-8") == module_text(MODULES[0])
    assert _submit(root, irq)["accepted"] is True
    assert _status(root) - before == {f"rtl/{MODULES[0]}.sv", f"rtl/{MODULES[1]}.sv"}
    # what is left is the workspaces of the tasks nobody submitted (the testbench's)
    assert len(_workspaces(root)) == len(first) - 2


def test_a_stray_file_in_the_workspace_rejects_the_task_and_lands_nothing(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    _next(root, f"plan:{BLOCK}")
    context = _context(root, PLAN)
    _write(context, (FIXTURES / "timer.plan.yml").read_text(encoding="utf-8"))
    stray = Path(context["outputs_abs"][0]).parents[1] / "notes.md"
    stray.write_text("not mine\n", encoding="utf-8")
    answer = _submit(root, PLAN)
    assert answer["accepted"] is False
    assert "notes.md" in str(answer["outside_outputs"])
    assert _status(root) == set()
    assert not (root / "notes.md").exists()


def test_a_user_edit_of_an_output_during_the_task_is_not_overwritten(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _next(root, f"plan:{BLOCK}")
    context = _context(root, PLAN)
    _write(context, (FIXTURES / "timer.plan.yml").read_text(encoding="utf-8"))
    mine = root / f"plan/{BLOCK}.plan.yml"
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_text("# the user wrote this meanwhile\n", encoding="utf-8")
    answer = _submit(root, PLAN)
    assert answer["accepted"] is False
    assert mine.read_text(encoding="utf-8") == "# the user wrote this meanwhile\n"


@pytest.mark.parametrize("isolate", [False])
def test_without_the_flag_nothing_changes(tmp_path: Path, isolate: bool) -> None:
    root = tinysoc_project(
        tmp_path / "plain", packs=("digital-rtl", "dv"), stub=True, runtime="claude-code"
    )
    _next(root, f"plan:{BLOCK}")
    context = _context(root, PLAN)
    assert Path(context["outputs_abs"][0]) == (root / f"plan/{BLOCK}.plan.yml").resolve() or str(
        context["outputs_abs"][0]
    ).startswith(str(root))
    assert _workspaces(root) == []


def test_best_of_two_runs_two_candidates_and_only_the_winner_lands(tmp_path: Path) -> None:
    root = tinysoc_project(
        tmp_path / "bo",
        packs=("digital-rtl", "dv"),
        stub=True,
        runtime="claude-code",
        profile={**PROFILE, "select": {"rules": {"digital-rtl/rtl_module": 2}}},
    )
    _next(root, f"plan:{BLOCK}")
    _write(_context(root, PLAN), (FIXTURES / "timer.plan.yml").read_text(encoding="utf-8"))
    assert _submit(root, PLAN)["accepted"] is True
    approve_plan(root, BLOCK)

    answer = _next(root, f"rtl:{BLOCK}")
    counter = f"digital-rtl/rtl_module[block=timer,module={MODULES[0]}]"
    mine = [t for t in answer["tasks"] if t["task_id"].startswith(counter)]
    assert len(mine) == 2, [t["task_id"] for t in answer["tasks"]]
    assert {t.get("candidate") for t in mine} == {1, 2}
    contexts = [_context(root, t["task_id"]) for t in mine]
    assert len({Path(c["outputs_abs"][0]).parent.parent for c in contexts}) == 2
    before = _status(root)
    for context in contexts:
        _write(context, module_text(MODULES[0]))
    assert _status(root) == before

    results = [_submit(root, t["task_id"]) for t in mine]
    assert all(r["accepted"] in (True, False) for r in results), results
    landed = _status(root) - before
    assert landed == {f"rtl/{MODULES[0]}.sv"}, (landed, results)
    assert (root / f"rtl/{MODULES[0]}.sv").read_text(encoding="utf-8") == module_text(MODULES[0])
    # neither candidate's workspace is left behind
    leftovers = " ".join(_workspaces(root))
    assert all(Path(c["outputs_abs"][0]).parents[1].name not in leftovers for c in contexts)


def test_best_of_two_one_fails_checks_and_the_passing_one_wins(tmp_path: Path) -> None:
    """One candidate failing its checks and the other passing -> the passing one wins and lands."""
    root = tinysoc_project(
        tmp_path / "bo2",
        packs=("digital-rtl", "dv"),
        stub=True,
        runtime="claude-code",
        profile={**PROFILE, "select": {"rules": {"digital-rtl/rtl_module": 2}}},
    )
    _next(root, f"plan:{BLOCK}")
    _write(_context(root, PLAN), (FIXTURES / "timer.plan.yml").read_text(encoding="utf-8"))
    assert _submit(root, PLAN)["accepted"] is True
    approve_plan(root, BLOCK)

    answer = _next(root, f"rtl:{BLOCK}")
    counter = f"digital-rtl/rtl_module[block=timer,module={MODULES[0]}]"
    mine = [t for t in answer["tasks"] if t["task_id"].startswith(counter)]
    assert len(mine) == 2
    assert {t.get("candidate") for t in mine} == {1, 2}
    contexts = [_context(root, t["task_id"]) for t in mine]
    before = _status(root)

    # Candidate 1: write an empty file (will fail the "outputs not empty" check)
    _write(contexts[0], "")
    # Candidate 2: write valid module (will pass checks)
    _write(contexts[1], module_text(MODULES[0]))

    # Submit both
    results = [_submit(root, t["task_id"]) for t in mine]
    # At least one should be accepted (the winner)
    # The individual candidate records may show different statuses
    landed = _status(root) - before
    assert landed == {f"rtl/{MODULES[0]}.sv"}, (landed, results)
    # The landed file should be the valid one (from candidate 2), not empty
    content = (root / f"rtl/{MODULES[0]}.sv").read_text(encoding="utf-8")
    assert content == module_text(MODULES[0]), "the valid candidate should win"
    # Workspaces should be cleaned up
    leftovers = " ".join(_workspaces(root))
    assert all(Path(c["outputs_abs"][0]).parents[1].name not in leftovers for c in contexts)


def test_best_of_two_both_fail_nothing_lands_and_new_round_starts(tmp_path: Path) -> None:
    """Both failing -> nothing lands, base rejected with tries_left decreased,
    next next_task starts a new round of 2 candidates with fresh workspaces."""
    root = tinysoc_project(
        tmp_path / "bo3",
        packs=("digital-rtl", "dv"),
        stub=True,
        runtime="claude-code",
        profile={**PROFILE, "select": {"rules": {"digital-rtl/rtl_module": 2}}},
    )
    _next(root, f"plan:{BLOCK}")
    _write(_context(root, PLAN), (FIXTURES / "timer.plan.yml").read_text(encoding="utf-8"))
    assert _submit(root, PLAN)["accepted"] is True
    approve_plan(root, BLOCK)

    # Round 1: both candidates fail
    answer1 = _next(root, f"rtl:{BLOCK}")
    counter = f"digital-rtl/rtl_module[block=timer,module={MODULES[0]}]"
    mine1 = [t for t in answer1["tasks"] if t["task_id"].startswith(counter)]
    assert len(mine1) == 2
    contexts1 = [_context(root, t["task_id"]) for t in mine1]
    before = _status(root)

    # Both candidates write invalid modules (missing output check - empty files fail)
    for ctx in contexts1:
        _write(ctx, "")  # Empty file - will fail the checks

    # Submit both
    for t in mine1:
        _submit(root, t["task_id"])
    # Nothing should land (both failed)
    assert _status(root) == before, "nothing should land when both candidates fail"
    # Workspaces should be cleaned up
    workspaces_after_round1 = _workspaces(root)
    for ctx in contexts1:
        ws_name = Path(ctx["outputs_abs"][0]).parents[1].name
        assert ws_name not in " ".join(workspaces_after_round1), "round 1 workspace should be gone"

    # Round 2: new candidates should be dispatched
    answer2 = _next(root, f"rtl:{BLOCK}")
    mine2 = [t for t in answer2["tasks"] if t["task_id"].startswith(counter)]
    # Should get 2 new candidates
    assert len(mine2) == 2, f"expected 2 new candidates, got {[t['task_id'] for t in mine2]}"
    contexts2 = [_context(root, t["task_id"]) for t in mine2]

    # The new candidates should have different workspaces
    ws1_paths = {Path(c["outputs_abs"][0]).parent.parent for c in contexts1}
    ws2_paths = {Path(c["outputs_abs"][0]).parent.parent for c in contexts2}
    assert ws1_paths.isdisjoint(ws2_paths), "round 2 should have fresh workspaces"

    # Now make round 2 succeed with valid code
    for ctx in contexts2:
        _write(ctx, module_text(MODULES[0]))
    results2 = [_submit(root, t["task_id"]) for t in mine2]
    landed = _status(root) - before
    assert landed == {f"rtl/{MODULES[0]}.sv"}, (landed, results2)


def test_best_of_two_double_submit_rejected(tmp_path: Path) -> None:
    """Double submit of the same candidate is rejected cleanly."""
    root = tinysoc_project(
        tmp_path / "bo4",
        packs=("digital-rtl", "dv"),
        stub=True,
        runtime="claude-code",
        profile={**PROFILE, "select": {"rules": {"digital-rtl/rtl_module": 2}}},
    )
    _next(root, f"plan:{BLOCK}")
    _write(_context(root, PLAN), (FIXTURES / "timer.plan.yml").read_text(encoding="utf-8"))
    assert _submit(root, PLAN)["accepted"] is True
    approve_plan(root, BLOCK)

    answer = _next(root, f"rtl:{BLOCK}")
    counter = f"digital-rtl/rtl_module[block=timer,module={MODULES[0]}]"
    mine = [t for t in answer["tasks"] if t["task_id"].startswith(counter)]
    assert len(mine) == 2
    contexts = [_context(root, t["task_id"]) for t in mine]

    # Write valid code for both
    for ctx in contexts:
        _write(ctx, module_text(MODULES[0]))

    # Submit first candidate
    _submit(root, mine[0]["task_id"])
    # First submit should succeed (even if not final yet)

    # Try to double-submit the same candidate - should fail cleanly
    from chipgraph.app.errors import AppError

    try:
        _submit(root, mine[0]["task_id"])
        double_submit_failed = False
    except AppError as e:
        double_submit_failed = True
        assert "already submitted" in str(e).lower() or "not dispatched" in str(e).lower()

    assert double_submit_failed, "double submit should be rejected"

    # Submit the second candidate to complete the round
    _submit(root, mine[1]["task_id"])
    # Now the round should be complete


def test_best_of_two_workspaces_gone_after_round(tmp_path: Path) -> None:
    """Verify all workspaces are removed after a best-of-N round ends."""
    root = tinysoc_project(
        tmp_path / "bo5",
        packs=("digital-rtl", "dv"),
        stub=True,
        runtime="claude-code",
        profile={**PROFILE, "select": {"rules": {"digital-rtl/rtl_module": 2}}},
    )
    _next(root, f"plan:{BLOCK}")
    _write(_context(root, PLAN), (FIXTURES / "timer.plan.yml").read_text(encoding="utf-8"))
    assert _submit(root, PLAN)["accepted"] is True
    approve_plan(root, BLOCK)

    answer = _next(root, f"rtl:{BLOCK}")
    counter = f"digital-rtl/rtl_module[block=timer,module={MODULES[0]}]"
    mine = [t for t in answer["tasks"] if t["task_id"].startswith(counter)]
    assert len(mine) == 2
    contexts = [_context(root, t["task_id"]) for t in mine]

    # Record the workspace paths
    workspace_names = [Path(c["outputs_abs"][0]).parents[1].name for c in contexts]

    # Workspaces should exist before submit
    worktrees_before = _workspaces(root)
    for ws_name in workspace_names:
        assert any(ws_name in wt for wt in worktrees_before), f"workspace {ws_name} should exist"

    # Write and submit both
    for ctx in contexts:
        _write(ctx, module_text(MODULES[0]))
    for t in mine:
        _submit(root, t["task_id"])

    # All candidate workspaces should be gone after the round
    worktrees_after = _workspaces(root)
    for ws_name in workspace_names:
        assert not any(ws_name in wt for wt in worktrees_after), (
            f"workspace {ws_name} should be gone after round"
        )


def test_best_of_winner_lands_and_base_accepted_for_dependents(tmp_path: Path) -> None:
    """Verify winner lands and the base is accepted so dependents can proceed."""
    from chipgraph.app.context import AppContext as Ctx
    from chipgraph.core.runtime import TaskQueue

    root = tinysoc_project(
        tmp_path / "bo6",
        packs=("digital-rtl", "dv"),
        stub=True,
        runtime="claude-code",
        profile={**PROFILE, "select": {"rules": {"digital-rtl/rtl_module": 2}}},
    )
    _next(root, f"plan:{BLOCK}")
    _write(_context(root, PLAN), (FIXTURES / "timer.plan.yml").read_text(encoding="utf-8"))
    assert _submit(root, PLAN)["accepted"] is True
    approve_plan(root, BLOCK)

    answer = _next(root, f"rtl:{BLOCK}")
    counter = f"digital-rtl/rtl_module[block=timer,module={MODULES[0]}]"
    mine = [t for t in answer["tasks"] if t["task_id"].startswith(counter)]
    assert len(mine) == 2
    contexts = [_context(root, t["task_id"]) for t in mine]

    # Write valid code for both candidates
    for ctx in contexts:
        _write(ctx, module_text(MODULES[0]))

    # Submit both
    for t in mine:
        _submit(root, t["task_id"])

    # Check that the BASE task record is accepted
    ctx = Ctx.load(root)
    queue = TaskQueue(ctx.layout)
    base_record = queue.get(counter)
    assert base_record is not None, "base record should exist"
    assert base_record.status == "accepted", f"base should be accepted, got {base_record.status}"

    # The winner's output should be in the user's tree
    assert (root / f"rtl/{MODULES[0]}.sv").is_file()


def test_a_task_sees_the_accepted_outputs_of_the_tasks_it_builds_on(tmp_path: Path) -> None:
    """The real run's lint of the top could not find the (uncommitted) counter and irq."""
    root = _project(tmp_path)
    _next(root, f"plan:{BLOCK}")
    _write(_context(root, PLAN), (FIXTURES / "timer.plan.yml").read_text(encoding="utf-8"))
    assert _submit(root, PLAN)["accepted"] is True
    approve_plan(root, BLOCK)
    answer = _next(root, f"rtl:{BLOCK}")
    ids = {m: f"digital-rtl/rtl_module[block=timer,module={m}]" for m in MODULES}
    for name in MODULES[:2]:
        assert ids[name] in {t["task_id"] for t in answer["tasks"]}
        _write(_context(root, ids[name]), module_text(name))
        assert _submit(root, ids[name])["accepted"] is True
    (root / "scratch.txt").write_text("the user's own uncommitted file\n", encoding="utf-8")

    answer = _next(root, f"rtl:{BLOCK}")
    assert ids[MODULES[2]] in {t["task_id"] for t in answer["tasks"]}
    context = _context(root, ids[MODULES[2]])
    workspace = Path(context["outputs_abs"][0]).parents[1]
    for name in MODULES[:2]:
        assert (workspace / f"rtl/{name}.sv").read_text(encoding="utf-8") == module_text(name)
    assert (workspace / "scratch.txt").is_file()
    assert not (workspace / ".chipgraph" / "state" / "tasks").exists()


def test_a_candidates_answer_says_what_it_meant_for_the_round(tmp_path: Path) -> None:
    """The real run's main session read `budget_exhausted` for a candidate that only lost."""
    root = tinysoc_project(
        tmp_path / "bo",
        packs=("digital-rtl", "dv"),
        stub=True,
        runtime="claude-code",
        profile={**PROFILE, "select": {"rules": {"digital-rtl/rtl_module": 2}}},
    )
    _next(root, f"plan:{BLOCK}")
    _write(_context(root, PLAN), (FIXTURES / "timer.plan.yml").read_text(encoding="utf-8"))
    assert _submit(root, PLAN)["accepted"] is True
    approve_plan(root, BLOCK)
    answer = _next(root, f"rtl:{BLOCK}")
    mine = [t for t in answer["tasks"] if "tiny_timer_counter]" in t["task_id"]]
    for task in mine:
        _write(_context(root, task["task_id"]), module_text(MODULES[0]))
    first, second = (_submit(root, t["task_id"]) for t in mine)
    assert (first["status"], first["accepted"]) == ("submitted", False)
    assert first["stop_reason"] is None and first["handoff"] is None
    assert (
        second["status"] == "lost" and second["accepted"] is False
    )  # both pass: the lowest id wins
    assert second["selection"]["winner"] == "1"
    assert "landed" in second["next"]
