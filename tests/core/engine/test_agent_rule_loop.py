"""M2-02a: the agent rule loop under the scheduler, with a fake runtime and fake checks.

Write -> check at once -> triage -> fix within the budget; HANDOFF on exhaustion; the
loop is bounded (runtime calls <= tries + max_infra_retries) whatever the runtime and
checks do.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agent_rule_helpers import AGENT, NEXT, OUTPUT, ContentChecks, Step, project

from chipgraph.core.contracts import Budget
from chipgraph.core.contracts.event import Event
from chipgraph.core.engine.scheduler import RunSummary
from chipgraph.core.state.journal import read


def _run(p: object, target: str = "*") -> RunSummary:
    return asyncio.run(p.scheduler.run(target))  # type: ignore[attr-defined]


def _events(p: object, run_id: str) -> tuple[Event, ...]:
    return read(p.layout.journal(run_id)).events  # type: ignore[attr-defined]


def _of(events: tuple[Event, ...], type_: str, iid: str = AGENT) -> list[Event]:
    return [e for e in events if e.type == type_ and e.rule_instance == iid]


def _handoff(p: object, run_id: str) -> str:
    return (p.layout.run_dir(run_id) / "HANDOFF.md").read_text()  # type: ignore[attr-defined]


# --- pass, fail then fixed --------------------------------------------------------------


def test_pass_on_the_first_try(tmp_path: Path) -> None:
    p = project(tmp_path, [Step(content="good", tokens=10, cost=0.01)])
    summary = _run(p)

    assert set(summary.done) == {AGENT, NEXT}
    assert p.runtime.calls == 1
    [task] = p.runtime.tasks
    assert task.context["try"] == "1"
    assert "feedback" not in task.context
    assert task.budget.tier == "medium"  # the author role's default tier
    assert task.allowed_writes == (OUTPUT,)
    # the rule's check ran once: by the loop, not again by the scheduler
    assert p.checks.calls == ["lint"]
    events = _events(p, summary.run_id)
    assert len(_of(events, "check_result")) == 1
    [turn] = _of(events, "agent_turn")
    assert turn.payload["counted"] is True and turn.payload["label"] is None
    [done] = _of(events, "rule_done")
    assert done.payload["agent"]["status"] == "done"
    assert done.payload["agent"]["tries"] == 1
    result = p.executor.results[AGENT]
    assert result.status == "done"
    assert (result.tokens, result.cost) == (10, 0.01)


def test_fail_then_fixed_on_the_second_try(tmp_path: Path) -> None:
    p = project(
        tmp_path,
        [Step(content="bad one", assumptions=("active-low reset",)), Step(content="good")],
    )
    summary = _run(p)

    assert set(summary.done) == {AGENT, NEXT}
    assert p.runtime.calls == 2
    first, second = p.runtime.tasks
    assert (first.budget.tier, second.budget.tier) == ("medium", "large")  # escalated
    assert second.context["try"] == "2"
    feedback = second.context["feedback"]
    assert "check `lint`: fail" in feedback
    assert f"{OUTPUT}:3" in feedback
    assert "(verification)" in feedback
    assert json.loads(second.context["previous_failures"]) == ["lint"]
    turns = _of(_events(p, summary.run_id), "agent_turn")
    assert [(t.payload["try"], t.payload["label"]) for t in turns] == [
        (1, "verification"),
        (2, None),
    ]
    assert [t.payload["tier"] for t in turns] == ["medium", "large"]
    assert p.executor.results[AGENT].assumptions == ("active-low reset",)


def test_no_escalation_without_an_escalate_tier(tmp_path: Path) -> None:
    p = project(
        tmp_path,
        [Step(content="bad"), Step(content="good")],
        budget=Budget(tries=3, tier="small"),  # a rule-set ladder: no escalate
    )
    _run(p)
    assert [t.budget.tier for t in p.runtime.tasks] == ["small", "small"]


# --- budget exhausted -> stop, HANDOFF, dependents blocked ------------------------------


def _varying_bad(n: int, _task: object) -> Step:
    return Step(content=f"bad {n}")


def test_tries_exhausted_stops_writes_handoff_and_blocks_dependents(tmp_path: Path) -> None:
    p = project(tmp_path, _varying_bad, budget=Budget(tries=3))
    summary = _run(p)

    assert summary.failed == (AGENT,)
    assert summary.blocked == (NEXT,)
    assert p.runtime.calls == 3  # exactly `tries` runs, no more
    assert p.gen.calls == {}  # the dependent never ran
    events = _events(p, summary.run_id)
    [fail] = _of(events, "rule_fail")
    assert fail.failure_label == "verification"
    agent = fail.payload["agent"]
    assert (agent["status"], agent["reason"], agent["tries"]) == ("budget_exhausted", "tries", 3)
    assert agent["failed_checks"] == ["lint"]
    assert len(_of(events, "agent_turn")) == 3
    assert len(_of(events, "check_result")) == 3
    assert p.executor.results[AGENT].status == "budget_exhausted"

    handoff = _handoff(p, summary.run_id)
    assert f"`{AGENT}` [verification]: agent budget exhausted (tries) after 3 of 3 tries" in handoff
    assert "reason `tries`" in handoff
    assert "last failures [verification]:" in handoff
    assert "lint: fail" in handoff
    assert f"## Blocked\n\n1 blocked:\n- {NEXT}" in handoff
    assert f"`chipgraph rewind {AGENT}`" in handoff


def test_always_done_with_failing_checks_is_bounded_by_tries(tmp_path: Path) -> None:
    # Same output and failures every time; with stagnation off only `tries` stops it.
    p = project(tmp_path, [Step(content="bad")], budget=Budget(tries=4), stagnation=False)
    summary = _run(p)
    assert summary.failed == (AGENT,)
    assert p.runtime.calls == 4
    assert p.runtime.calls <= p.executor.max_calls(p.scheduler.graph.rules["p/write"])


def test_token_cap_stops_early(tmp_path: Path) -> None:
    p = project(tmp_path, _tokens_step, budget=Budget(tries=5, tokens=100))
    summary = _run(p)
    assert summary.failed == (AGENT,)
    assert p.runtime.calls == 2  # 60 + 60 >= 100
    [fail] = _of(_events(p, summary.run_id), "rule_fail")
    assert fail.payload["agent"]["reason"] == "tokens"
    assert p.executor.results[AGENT].tokens == 120
    assert "token cap" in _handoff(p, summary.run_id)


def _tokens_step(n: int, _task: object) -> Step:
    return Step(content=f"bad {n}", tokens=60)


def _cost_step(n: int, _task: object) -> Step:
    return Step(content=f"bad {n}", cost=0.03)


def test_cost_cap_stops_early(tmp_path: Path) -> None:
    p = project(tmp_path, _cost_step, budget=Budget(tries=5), max_cost=0.05)
    summary = _run(p)
    assert p.runtime.calls == 2
    [fail] = _of(_events(p, summary.run_id), "rule_fail")
    assert fail.payload["agent"]["reason"] == "cost"
    assert p.executor.results[AGENT].cost == 0.06


def test_stagnation_stops_early(tmp_path: Path) -> None:
    p = project(tmp_path, [Step(content="bad")], budget=Budget(tries=5))
    summary = _run(p)
    assert p.runtime.calls == 2  # the second try changed nothing
    [fail] = _of(_events(p, summary.run_id), "rule_fail")
    assert fail.payload["agent"]["reason"] == "stagnation"
    assert p.executor.results[AGENT].status == "budget_exhausted"
    assert "stopped early" in _handoff(p, summary.run_id)


# --- infra: retried without a try, bounded ----------------------------------------------


def test_runtime_error_is_retried_without_using_a_try(tmp_path: Path) -> None:
    p = project(
        tmp_path,
        [Step(raises=ConnectionError("reset by peer")), Step(content="bad"), Step(content="good")],
        budget=Budget(tries=2),
    )
    summary = _run(p)
    assert set(summary.done) == {AGENT, NEXT}
    assert p.runtime.calls == 3  # 1 infra retry + 2 tries, with tries=2
    turns = _of(_events(p, summary.run_id), "agent_turn")
    assert [(t.payload["counted"], t.payload["label"]) for t in turns] == [
        (False, "infra"),
        (True, "verification"),
        (True, None),
    ]
    assert "ConnectionError" in p.runtime.tasks[1].context["infra_retry"]
    assert p.runtime.tasks[1].context["try"] == "1"  # the retry is still try 1
    done = _of(_events(p, summary.run_id), "rule_done")[0]
    assert (done.payload["agent"]["tries"], done.payload["agent"]["infra_failures"]) == (2, 1)


def test_check_error_is_retried_without_a_try_and_bounded(tmp_path: Path) -> None:
    checks = ContentChecks(tmp_path, script={"lint": ["error"]})
    p = project(tmp_path, [Step(content="good")], budget=Budget(tries=3), checks=checks)
    summary = _run(p)
    assert summary.failed == (AGENT,)
    assert p.runtime.calls == 3  # the first call + max_infra_retries (2)
    [fail] = _of(_events(p, summary.run_id), "rule_fail")
    assert fail.failure_label == "infra"
    agent = fail.payload["agent"]
    assert (agent["reason"], agent["tries"], agent["infra_failures"]) == ("infra", 0, 3)
    assert p.executor.results[AGENT].status == "failed"
    text = _handoff(p, summary.run_id)
    assert "chipgraph doctor" in text
    # The stop is remembered, so `resume` would not run the instance again: rewind it.
    assert f"`chipgraph rewind {AGENT}`" in text
    assert "chipgraph resume" not in text.split("## Next step")[1]


def test_infra_then_pass_uses_one_try(tmp_path: Path) -> None:
    checks = ContentChecks(tmp_path, script={"lint": ["error", "error", "pass"]})
    p = project(tmp_path, [Step(content="good")], budget=Budget(tries=1), checks=checks)
    summary = _run(p)
    assert AGENT in summary.done
    assert p.runtime.calls == 3 == 1 + 2  # tries + max_infra_retries


def test_errors_never_run_past_the_bound(tmp_path: Path) -> None:
    p = project(
        tmp_path,
        [Step(raises=TimeoutError("slow"))],
        budget=Budget(tries=2),
        max_infra_retries=4,
    )
    _run(p)
    assert p.runtime.calls == 5  # 1 + 4 retries
    assert p.runtime.calls <= 2 + 4


# --- needs_human ------------------------------------------------------------------------


def test_needs_human_stops_at_once_with_the_questions(tmp_path: Path) -> None:
    p = project(tmp_path, [Step(content=None, status="needs_human", questions=("which reset?",))])
    summary = _run(p)
    assert summary.failed == (AGENT,)
    assert summary.blocked == (NEXT,)
    assert p.runtime.calls == 1
    [fail] = _of(_events(p, summary.run_id), "rule_fail")
    assert fail.failure_label == "planning"
    assert fail.payload["agent"]["tries"] == 0  # no try used
    assert fail.payload["open_questions"] == ["which reset?"]
    result = p.executor.results[AGENT]
    assert (result.status, result.open_questions) == ("needs_human", ("which reset?",))
    handoff = _handoff(p, summary.run_id)
    assert "## Open questions\n\n- which reset?" in handoff
    assert "answer the open questions" in handoff


def test_needs_human_after_a_failed_try(tmp_path: Path) -> None:
    p = project(
        tmp_path,
        [Step(content="bad"), Step(content=None, status="needs_human", questions=("q?",))],
    )
    summary = _run(p)
    [fail] = _of(_events(p, summary.run_id), "rule_fail")
    assert fail.payload["agent"]["tries"] == 1
    assert p.runtime.calls == 2


# --- labels through the loop ------------------------------------------------------------


def test_missing_output_is_context(tmp_path: Path) -> None:
    p = project(tmp_path, [Step(content=None)], budget=Budget(tries=1), rule_checks=())
    summary = _run(p)
    [fail] = _of(_events(p, summary.run_id), "rule_fail")
    assert fail.failure_label == "context"
    assert fail.payload["agent"]["failed_checks"] == ["agent.outputs"]


def test_write_outside_outputs_is_constraint(tmp_path: Path) -> None:
    p = project(
        tmp_path,
        [Step(content="good", files=(OUTPUT, "rtl/extra.sv"))],
        budget=Budget(tries=1),
    )
    summary = _run(p)
    [fail] = _of(_events(p, summary.run_id), "rule_fail")
    assert fail.failure_label == "constraint"
    assert fail.payload["agent"]["failed_checks"] == ["agent.writes"]


def test_spec_ambiguity_is_planning_and_stops(tmp_path: Path) -> None:
    checks = ContentChecks(tmp_path, script={"lint": ["fail"]}, issue_rule="spec.ambiguous")
    p = project(tmp_path, [Step(content="x")], budget=Budget(tries=3), checks=checks)
    summary = _run(p)
    assert p.runtime.calls == 1  # retrying would not fix the spec
    [fail] = _of(_events(p, summary.run_id), "rule_fail")
    assert fail.failure_label == "planning"
    assert fail.payload["agent"]["reason"] == "planning"
    result = p.executor.results[AGENT]
    assert result.status == "needs_human"
    assert any("spec.ambiguous" in q for q in result.open_questions)


def test_project_rule_check_is_constraint(tmp_path: Path) -> None:
    checks = ContentChecks(tmp_path, script={"naming": ["fail"]})
    p = project(
        tmp_path, _varying_bad, budget=Budget(tries=2), rule_checks=("naming",), checks=checks
    )
    summary = _run(p)
    [fail] = _of(_events(p, summary.run_id), "rule_fail")
    assert fail.failure_label == "constraint"
    assert "(constraint)" in p.runtime.tasks[1].context["feedback"]


# --- resume after a stop ----------------------------------------------------------------


def test_resume_does_not_restart_an_exhausted_instance(tmp_path: Path) -> None:
    p = project(tmp_path, _varying_bad, budget=Budget(tries=2))
    first = _run(p)
    assert p.runtime.calls == 2

    resumed = asyncio.run(p.scheduler.resume(first.run_id))
    assert resumed.failed == (AGENT,)
    assert resumed.blocked == (NEXT,)
    assert p.runtime.calls == 2  # not called again
    fails = _of(_events(p, first.run_id), "rule_fail")
    assert fails[-1].payload["agent"]["stopped_earlier"] is True
    assert "stopped in an earlier run" in fails[-1].payload["message"]
    assert "stopped in an earlier run" in _handoff(p, first.run_id)

    # a new run does not restart it either
    _run(p)
    assert p.runtime.calls == 2


def test_a_changed_input_gives_a_fresh_budget(tmp_path: Path) -> None:
    p = project(tmp_path, _varying_bad, budget=Budget(tries=2))
    _run(p)
    (tmp_path / "spec.md").write_text("spec v2\n")
    _run(p)
    assert p.runtime.calls == 4


def test_rewind_gives_a_fresh_budget(tmp_path: Path) -> None:
    p = project(tmp_path, _varying_bad, budget=Budget(tries=2))
    _run(p)
    assert AGENT in p.scheduler.rewind(AGENT)
    _run(p)
    assert p.runtime.calls == 4


def test_an_infra_stop_is_retried_on_resume(tmp_path: Path) -> None:
    checks = ContentChecks(tmp_path, script={"lint": ["error", "error", "error", "pass"]})
    p = project(tmp_path, [Step(content="good")], checks=checks)
    first = _run(p)
    assert first.failed == (AGENT,)
    assert p.runtime.calls == 3
    resumed = asyncio.run(p.scheduler.resume(first.run_id))
    assert set(resumed.done) == {AGENT, NEXT}
    assert p.runtime.calls == 4


def test_without_a_layout_nothing_is_remembered(tmp_path: Path) -> None:
    p = project(tmp_path, _varying_bad, budget=Budget(tries=2), remember=False)
    _run(p)
    _run(p)
    assert p.runtime.calls == 4


# --- journal ----------------------------------------------------------------------------


def test_journal_has_one_agent_turn_per_call_and_the_label_on_rule_fail(tmp_path: Path) -> None:
    p = project(
        tmp_path,
        [
            Step(raises=OSError("disk")),
            Step(content="bad 1", tokens=5, cost=0.001),
            Step(content="bad 2", tokens=7),
        ],
        budget=Budget(tries=2),
    )
    summary = _run(p)
    events = _events(p, summary.run_id)
    turns = _of(events, "agent_turn")
    assert [t.payload["call"] for t in turns] == [1, 2, 3]
    assert [t.payload["try"] for t in turns] == [1, 1, 2]
    assert [t.payload["tokens"] for t in turns] == [None, 5, 7]
    assert all(t.failure_label is None for t in turns)
    expected = {"phase", "call", "try", "counted", "tier", "status", "label", "failed_checks"}
    assert expected <= set(turns[0].payload)
    assert turns[0].payload["failed_checks"] == ["agent.runtime"]
    [fail] = _of(events, "rule_fail")
    assert fail.failure_label == "verification"
    assert fail.payload["agent"]["tokens"] == 12
    assert fail.payload["result"]["status"] == "budget_exhausted"
    order = [e.type for e in events if e.rule_instance == AGENT]
    assert order[0] == "rule_start" and order[-1] == "rule_fail"
