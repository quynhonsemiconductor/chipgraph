"""M2-02b: the agent loop in runtime claude-code, in process, on the acceptance fixture
(a tinysoc copy with a `fixable` and a `hopeless` author task, `agent_loop_helpers`).

No model: the test plays the subagent's part by writing the output file itself. Covered:
fail then fixed (label, redo text with `file:line`, escalated tier, the redo text in the
next context), exhaustion (exactly `tries` dispatches, HANDOFF.md at once, dependents
blocked, `next_task` stops and never hands it out again), infra failures (no try used,
bounded), stagnation, `needs_human` unchanged, the journal events, rewind.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from agent_loop_helpers import (
    TIERS,
    fx,
    get_context,
    next_task,
    record,
    submit,
    task_ids,
    write,
)
from mcp import Client

from chipgraph.app.build import make_scheduler
from chipgraph.app.context import AppContext
from chipgraph.core.state import journal as journal_mod
from chipgraph.core.state.handoff import AgentStopInfo
from chipgraph.core.state.layout import StateLayout
from chipgraph.mcp.server import build_server

FIXED = f"{fx.MARKER}\ntiny_timer counts clock cycles.\n"
FLAKY_TASK = "loop/flaky[]"
FLAKY_OUTPUT = "doc/loop/flaky.md"
FLAKY_RULE: dict[str, Any] = {
    "rule": "flaky",
    "kind": "agent",
    "role": "author",
    "inputs": [{"path": fx.FIXABLE_SPEC}],
    "outputs": [FLAKY_OUTPUT],
    "checks": ["loop_no_tool"],
    "budget": {"tries": 2},
}
NO_TOOL = {"use": "cmd", "cmd": ["chipgraph-no-such-tool-m2-02b"]}


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("loop") / "tinysoc"
    return fx.make_loop_project(
        root, tiers=TIERS, extra_rules=[FLAKY_RULE], extra_adapters={"loop_no_tool": NO_TOOL}
    )


@pytest.fixture
def project(base: Path, tmp_path: Path) -> Path:
    dest = tmp_path / "tinysoc"
    shutil.copytree(base, dest, symlinks=True, ignore=shutil.ignore_patterns("*.lock"))
    return dest


def _events(root: Path, run_id: str) -> list[Any]:
    return journal_mod.read(StateLayout(root).journal(run_id)).events


def test_fail_then_fixed_on_the_second_try(project: Path) -> None:
    first = next_task(project, "loop/fixable")
    [task] = first["tasks"]
    assert (task["tier"], task["model"], task["try"], task["tries_left"]) == (
        "medium",
        "haiku",
        1,
        3,
    )
    write(project, fx.FIXABLE_OUTPUT, "tiny_timer counts.\n")
    rejected = submit(project, fx.FIXABLE_TASK)
    assert rejected["status"] == "rejected" and rejected["accepted"] is False
    assert rejected["label"] == "verification" and rejected["counted"] is True
    [failed] = rejected["failed_checks"]
    assert failed["check_id"] == "loop_fixable_check" and failed["status"] == "fail"
    assert failed["issues"][0]["at"] == f"{fx.FIXABLE_OUTPUT}:1"
    assert fx.MARKER in failed["issues"][0]["msg"]
    redo = rejected["redo"]
    assert f"{fx.FIXABLE_OUTPUT}:1:" in redo and fx.MARKER in redo
    assert "(verification)" in redo and "What to fix" in redo
    assert (rejected["next_tier"], rejected["next_model"]) == ("large", "sonnet")
    assert rejected["tries_left"] == 2
    assert rejected["budget"] == {
        "used": 1,
        "allowed": 3,
        "infra_retries": 0,
        "max_infra_retries": 2,
        "dispatches": 1,
        "max_dispatches": 5,
    }
    assert rejected["stop_reason"] is None and rejected["handoff"] is None

    again = next_task(project, "loop/fixable")
    [task] = again["tasks"]
    assert (task["tier"], task["model"], task["try"]) == ("large", "sonnet", 2)
    assert task["previous_label"] == "verification"
    context = get_context(project, fx.FIXABLE_TASK)
    assert context["previous_rejection"] == [redo]
    assert context["previous_label"] == "verification"

    write(project, fx.FIXABLE_OUTPUT, FIXED)
    accepted = submit(project, fx.FIXABLE_TASK)
    assert accepted["status"] == "accepted" and accepted["accepted"] is True
    assert accepted["label"] is None and accepted["redo"] is None
    assert accepted["budget"]["used"] == 2
    assert record(project, fx.FIXABLE_TASK).dispatches == 2

    # One agent_turn per submit with try, tier, label and failed checks.
    turns = [
        e.payload
        for run in (first["run_id"], again["run_id"])
        for e in _events(project, run)
        if e.type == "agent_turn" and e.payload.get("phase") == "submit"
    ]
    assert [(t["try"], t["tier"], t["label"], t["failed_checks"]) for t in turns] == [
        (1, "medium", "verification", ["loop_fixable_check"]),
        (2, "large", None, []),
    ]


def _exhaust_hopeless(project: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Accept `fixable`, fail `hopeless` twice (different outputs). Returns the dispatch
    answer of the last try and its submit answer."""
    first = next_task(project)
    assert sorted(task_ids(first)) == [fx.FIXABLE_TASK, fx.HOPELESS_TASK]
    write(project, fx.FIXABLE_OUTPUT, FIXED)
    write(project, fx.HOPELESS_OUTPUT, "tiny_gpio\n")
    assert submit(project, fx.FIXABLE_TASK)["accepted"] is True
    assert submit(project, fx.HOPELESS_TASK)["status"] == "rejected"
    second = next_task(project)
    assert task_ids(second) == [fx.HOPELESS_TASK]
    write(project, fx.HOPELESS_OUTPUT, "the tiny_gpio block\n")
    return second, submit(project, fx.HOPELESS_TASK)


def test_exhausted_after_exactly_tries_dispatches_writes_handoff(project: Path) -> None:
    second, last = _exhaust_hopeless(project)
    assert last["status"] == "budget_exhausted" and last["stop_reason"] == "tries"
    assert last["label"] == "verification" and last["tries_left"] == 0
    assert last["next_tier"] is None and last["next_model"] is None
    assert record(project, fx.HOPELESS_TASK).dispatches == fx.HOPELESS_TRIES

    # HANDOFF.md of the run that dispatched the last try, written at once.
    handoff = Path(last["handoff"])
    assert handoff == StateLayout(project).run_dir(second["run_id"]) / "HANDOFF.md"
    text = handoff.read_text()
    assert f"`{fx.HOPELESS_TASK}` [verification]" in text
    assert "reason `tries`, 2 of 2 tries" in text
    assert "last failures [verification]" in text
    assert f"{fx.HOPELESS_OUTPUT}:1: the first line must be the signed-off" in text
    assert f"- {fx.DONE_TASK}" in text  # the dependent, blocked
    assert f"`chipgraph rewind {fx.HOPELESS_TASK}`" in text

    # The rule_fail event carries the label and the loop's summary.
    [fail] = [
        e
        for e in _events(project, second["run_id"])
        if e.type == "rule_fail" and e.rule_instance == fx.HOPELESS_TASK and "agent" in e.payload
    ]
    assert fail.failure_label == "verification"
    info = AgentStopInfo.model_validate(fail.payload["agent"])
    assert (info.status, info.reason, info.tries, info.max_tries) == (
        "budget_exhausted",
        "tries",
        2,
        2,
    )
    assert info.failed_checks == ("loop_hopeless_check",)


def test_after_exhaustion_next_task_stops_and_never_dispatches_again(project: Path) -> None:
    _exhaust_hopeless(project)
    for _ in range(3):
        answer = next_task(project)
        assert answer["tasks"] == [] and answer["in_progress"] == []
        assert answer["done"] is False and answer["stopped"] is True
        assert Path(answer["handoff"]).is_file()
        blocked = {b["instance"]: b for b in answer["blocked"]}
        assert set(blocked) == {fx.HOPELESS_TASK, fx.DONE_TASK}
        hopeless = blocked[fx.HOPELESS_TASK]
        assert hopeless["status"] == "budget_exhausted"
        assert hopeless["label"] == "verification"
        assert hopeless["handoff"] == answer["handoff"]
        assert "budget exhausted (tries) after 2 of 2 tries" in hopeless["reason"]
        assert blocked[fx.DONE_TASK]["blocked_by"] == [fx.HOPELESS_TASK]
        assert "agent: budget_exhausted, reason `tries`" in Path(answer["handoff"]).read_text()
    assert record(project, fx.HOPELESS_TASK).dispatches == fx.HOPELESS_TRIES
    assert not (project / "build" / "loop.done.json").exists()  # the dependent never ran


def test_infra_failures_use_no_try_and_are_bounded(project: Path) -> None:
    answers = []
    for n in range(1, 4):
        dispatched = next_task(project, "loop/flaky")
        assert task_ids(dispatched) == [FLAKY_TASK], (n, dispatched["blocked"])
        assert dispatched["tasks"][0]["tier"] == "medium"  # no escalation on infra
        write(project, FLAKY_OUTPUT, f"note {n}\n")
        answers.append(submit(project, FLAKY_TASK))
    first, second, third = answers
    for n, answer in enumerate((first, second), start=1):
        assert answer["status"] == "rejected" and answer["label"] == "infra"
        assert answer["counted"] is False and answer["attempts"] == 0
        assert answer["budget"]["infra_retries"] == n and answer["tries_left"] == 2
        assert "infrastructure error" in answer["redo"]
        assert answer["next_tier"] == "medium"
    assert third["status"] == "budget_exhausted" and third["stop_reason"] == "infra"
    assert third["budget"]["infra_retries"] == 3 and third["attempts"] == 0
    assert "infrastructure errors persisted" in Path(third["handoff"]).read_text()

    after = next_task(project, "loop/flaky")
    assert after["tasks"] == [] and after["stopped"] is True
    # tries (2) + max_infra_retries (2) bound it; it stopped at 3, never past the bound.
    assert record(project, FLAKY_TASK).dispatches == 3


def test_same_outputs_and_failures_twice_stop_early(project: Path) -> None:
    next_task(project, "loop/fixable")
    write(project, fx.FIXABLE_OUTPUT, "same\n")
    assert submit(project, fx.FIXABLE_TASK)["status"] == "rejected"
    next_task(project, "loop/fixable")
    write(project, fx.FIXABLE_OUTPUT, "same\n")
    answer = submit(project, fx.FIXABLE_TASK)
    assert answer["status"] == "budget_exhausted"
    assert answer["stop_reason"] == "stagnation" and answer["budget"]["used"] == 2
    assert "same outputs and failures" in Path(answer["handoff"]).read_text()
    assert next_task(project, "loop/fixable")["tasks"] == []


def test_needs_human_is_unchanged(project: Path) -> None:
    next_task(project, "loop/fixable")
    answer = submit(
        project, fx.FIXABLE_TASK, status="needs_human", open_questions=["which register?"]
    )
    assert answer["status"] == "needs_human" and answer["attempts"] == 0
    assert answer["counted"] is False and answer["budget"]["used"] == 0
    assert answer["handoff"] is None and answer["redo"] is None
    after = next_task(project, "loop/fixable")
    assert after["tasks"] == [] and "which register?" in after["waiting"]
    [blocked] = after["blocked"]
    assert blocked["status"] == "needs_human" and blocked["label"] == "planning"


def test_a_missing_output_is_context_and_names_the_file(project: Path) -> None:
    next_task(project, "loop/fixable")
    answer = submit(project, fx.FIXABLE_TASK)
    assert answer["status"] == "rejected" and answer["label"] == "context"
    assert answer["missing"] == [fx.FIXABLE_OUTPUT]
    assert f"output {fx.FIXABLE_OUTPUT} was not written" in answer["redo"]


def test_rewind_reopens_an_exhausted_task_with_a_fresh_budget(project: Path) -> None:
    _exhaust_hopeless(project)
    make_scheduler(AppContext.load(project), "*").rewind(fx.HOPELESS_TASK)
    reopened = record(project, fx.HOPELESS_TASK)
    assert reopened.status == "ready" and reopened.budget_state is None
    answer = next_task(project)
    [task] = answer["tasks"]
    assert task["task_id"] == fx.HOPELESS_TASK and task["try"] == 1
    assert task["tier"] == "medium"
    assert get_context(project, fx.HOPELESS_TASK)["previous_rejection"] == []


def test_submit_answer_is_json(project: Path) -> None:
    next_task(project, "loop/fixable")
    write(project, fx.FIXABLE_OUTPUT, "x\n")
    json.dumps(submit(project, fx.FIXABLE_TASK))


SUBMIT_KEYS = {
    # M1-11 keys, unchanged
    "task_id",
    "accepted",
    "status",
    "attempts",
    "tries",
    "reasons",
    "outside_outputs",
    "missing",
    "checks",
    "result",
    "next",
    # M2-02b, additive
    "label",
    "counted",
    "failed_checks",
    "redo",
    "next_tier",
    "next_model",
    "tries_left",
    "budget",
    "stop_reason",
    "handoff",
}


def test_the_mcp_tools_return_the_new_keys(project: Path) -> None:
    async def _call(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        async with Client(build_server(project)) as client:
            result = await client.call_tool(tool, args)
        assert not result.is_error, result.content
        assert isinstance(result.structured_content, dict)
        return result.structured_content

    handed = asyncio.run(_call("next_task", {"target": "loop/fixable"}))
    assert {"tasks", "in_progress", "done", "stopped", "waiting", "blocked", "handoff"} <= set(
        handed
    )
    [task] = handed["tasks"]
    assert {"try", "tries_left", "max_dispatches", "previous_label"} <= set(task)
    write(project, fx.FIXABLE_OUTPUT, "no marker\n")
    answer = asyncio.run(_call("submit", {"task_id": fx.FIXABLE_TASK}))
    assert set(answer) == SUBMIT_KEYS
    assert answer["status"] in ("accepted", "rejected", "budget_exhausted", "needs_human")
    assert set(answer["budget"]) >= {"used", "allowed", "infra_retries"}
