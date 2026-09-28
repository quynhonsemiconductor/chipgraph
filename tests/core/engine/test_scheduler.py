"""Tests for `chipgraph.core.engine.scheduler.Scheduler`: run, resume machinery within a
single process, gates, checks, failures, staleness-driven skipping, and rewind.

No pytest-asyncio plugin is available in this project, so async entry points are driven
with `asyncio.run()` from ordinary (sync) test functions.

The kill/resume-across-processes acceptance test lives in test_resume_kill.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from scheduler_fixtures import (
    FailingExecutor,
    FakeCheckRunner,
    FakeGateChecker,
    NoOutputExecutor,
    UppercaseExecutor,
    make_check_result,
)

from chipgraph.core.contracts import InputSpec, RuleInstance, RuleSpec, RunSpec
from chipgraph.core.engine.graph import StaticForeach, build_graph
from chipgraph.core.engine.records import RecordStore
from chipgraph.core.engine.scheduler import AgentStub, ExecOutcome, Scheduler
from chipgraph.core.state.artifacts import ArtifactStore, LabelRules
from chipgraph.core.state.journal import read, replay
from chipgraph.core.state.layout import StateLayout


def _rule(
    id_: str,
    outputs: tuple[str, ...],
    *,
    inputs: tuple[InputSpec, ...] = (),
    kind: str = "gen",
    checks: tuple[str, ...] = (),
    gate: str | None = None,
    foreach: str | None = None,
    role: str | None = None,
) -> RuleSpec:
    run = RunSpec(use="cmd") if kind == "gen" else None
    return RuleSpec(
        id=id_,
        kind=kind,
        outputs=outputs,
        inputs=inputs,
        checks=checks,
        gate=gate,
        foreach=foreach,
        role=role,
        run=run,
    )


def _chain_rules() -> list[RuleSpec]:
    return [
        _rule("p/a", ("a.txt",)),
        _rule("p/b", ("b.txt",), inputs=(InputSpec(source="path", selector="a.txt"),)),
        _rule("p/c", ("c.txt",), inputs=(InputSpec(source="path", selector="b.txt"),)),
    ]


def _make_scheduler(
    tmp_path: Path,
    rules: list[RuleSpec],
    executor: object,
    *,
    checks: object | None = None,
    gates: object | None = None,
    concurrency: int = 4,
    resolver: StaticForeach | None = None,
    executors: dict[str, object] | None = None,
) -> tuple[Scheduler, ArtifactStore, StateLayout]:
    graph = build_graph(rules, resolver or StaticForeach({}))
    store = ArtifactStore(tmp_path, LabelRules())
    layout = StateLayout(tmp_path)
    scheduler = Scheduler(
        graph,
        layout=layout,
        store=store,
        executors=executors if executors is not None else {"gen": executor},  # type: ignore[arg-type]
        checks=checks,  # type: ignore[arg-type]
        gates=gates,  # type: ignore[arg-type]
        concurrency=concurrency,
    )
    return scheduler, store, layout


# --- basic chain: order, skip-when-fresh, re-run on input change -------------------


def test_chain_runs_in_order_and_writes_outputs(tmp_path: Path) -> None:
    executor = UppercaseExecutor(tmp_path)
    scheduler, _store, _layout = _make_scheduler(tmp_path, _chain_rules(), executor)

    summary = asyncio.run(scheduler.run("*"))

    assert summary.ok
    assert set(summary.done) == {"p/a[]", "p/b[]", "p/c[]"}
    assert (tmp_path / "a.txt").read_text() == "P/A[]"
    assert (tmp_path / "b.txt").read_text() == "P/A[]"
    assert (tmp_path / "c.txt").read_text() == "P/A[]"
    assert executor.calls == {"p/a[]": 1, "p/b[]": 1, "p/c[]": 1}


def test_second_run_skips_everything_as_fresh(tmp_path: Path) -> None:
    executor = UppercaseExecutor(tmp_path)
    scheduler, _store, _layout = _make_scheduler(tmp_path, _chain_rules(), executor)
    asyncio.run(scheduler.run("*"))

    summary = asyncio.run(scheduler.run("*"))

    assert summary.ok
    assert set(summary.skipped_fresh) == {"p/a[]", "p/b[]", "p/c[]"}
    assert summary.done == ()
    assert executor.calls == {"p/a[]": 1, "p/b[]": 1, "p/c[]": 1}  # unchanged: not re-run


def test_editing_a_source_input_reruns_downstream_but_not_independent_branch(
    tmp_path: Path,
) -> None:
    (tmp_path / "src.txt").write_text("seed", encoding="utf-8")
    rules = [
        _rule("p/a", ("a.txt",), inputs=(InputSpec(source="path", selector="src.txt"),)),
        _rule("p/b", ("b.txt",), inputs=(InputSpec(source="path", selector="a.txt"),)),
        _rule("p/c", ("c.txt",), inputs=(InputSpec(source="path", selector="b.txt"),)),
        _rule("p/d", ("d.txt",)),  # independent of the a->b->c chain
    ]
    executor = UppercaseExecutor(tmp_path)
    scheduler, _store, _layout = _make_scheduler(tmp_path, rules, executor)
    asyncio.run(scheduler.run("*"))

    # 'src.txt' is an external source file (no rule produces it): editing it by hand is
    # a normal, expected input change, not a diverged output.
    (tmp_path / "src.txt").write_text("edited by hand", encoding="utf-8")

    summary = asyncio.run(scheduler.run("*"))

    assert summary.ok
    assert set(summary.done) == {"p/a[]", "p/b[]", "p/c[]"}
    assert summary.skipped_fresh == ("p/d[]",)
    assert executor.calls == {"p/a[]": 2, "p/b[]": 2, "p/c[]": 2, "p/d[]": 1}


# --- bounded concurrency -------------------------------------------------------------


def test_independent_branches_run_concurrently_bounded_by_concurrency(tmp_path: Path) -> None:
    resolver = StaticForeach({"instances": [{"i": str(i)} for i in range(6)]})
    rule = _rule("p/independent", ("out-{i}.txt",), foreach="instances")
    executor = UppercaseExecutor(tmp_path, delay_s=0.05)
    scheduler, _store, _layout = _make_scheduler(
        tmp_path, [rule], executor, concurrency=2, resolver=resolver
    )

    summary = asyncio.run(scheduler.run("*"))

    assert summary.ok
    assert len(summary.done) == 6
    assert executor.max_active <= 2
    assert executor.max_active >= 1


# --- gates ----------------------------------------------------------------------------


def test_gate_waiting_blocks_its_branch_but_not_an_independent_one(tmp_path: Path) -> None:
    rules = [
        _rule("p/gated", ("gated.txt",), gate="gate:{stage}"),
        _rule(
            "p/gated_child",
            ("gated_child.txt",),
            inputs=(InputSpec(source="path", selector="gated.txt"),),
        ),
        _rule("p/independent", ("independent.txt",)),
    ]
    executor = UppercaseExecutor(tmp_path)
    scheduler, _store, _layout = _make_scheduler(tmp_path, rules, executor)

    summary = asyncio.run(scheduler.run("*"))

    assert not summary.ok
    assert summary.waiting_gate == ("p/gated[]",)
    assert summary.blocked == ("p/gated_child[]",)
    assert summary.done == ("p/independent[]",)
    assert executor.calls == {"p/independent[]": 1}


def test_gate_approved_lets_the_rule_run(tmp_path: Path) -> None:
    rule = _rule("p/gated", ("gated.txt",), gate="gate:main")
    executor = UppercaseExecutor(tmp_path)
    gates = FakeGateChecker({"gate:main": "approved"})
    scheduler, _store, _layout = _make_scheduler(tmp_path, [rule], executor, gates=gates)

    summary = asyncio.run(scheduler.run("*"))

    assert summary.ok
    assert summary.done == ("p/gated[]",)


def test_run_writes_handoff_md_with_waiting_gate_next_step(tmp_path: Path) -> None:
    rule = _rule("p/gated", ("gated.txt",), gate="gate:main")
    executor = UppercaseExecutor(tmp_path)
    gates = FakeGateChecker({})
    scheduler, _store, layout = _make_scheduler(tmp_path, [rule], executor, gates=gates)

    summary = asyncio.run(scheduler.run("*"))

    assert summary.waiting_gate == ("p/gated[]",)
    handoff_path = layout.run_dir(summary.run_id) / "HANDOFF.md"
    assert handoff_path.is_file()
    content = handoff_path.read_text(encoding="utf-8")
    assert "chipgraph approve" in content
    assert "gate:main" in content


def test_resume_rewrites_handoff_md(tmp_path: Path) -> None:
    rule = _rule("p/a", ("a.txt",))
    executor = UppercaseExecutor(tmp_path)
    scheduler, _store, layout = _make_scheduler(tmp_path, [rule], executor)

    run1 = asyncio.run(scheduler.run("*"))
    handoff_path = layout.run_dir(run1.run_id) / "HANDOFF.md"
    assert handoff_path.is_file()

    resumed = asyncio.run(scheduler.resume(run1.run_id))
    assert resumed.ok
    assert handoff_path.is_file()
    content = handoff_path.read_text(encoding="utf-8")
    assert "nothing to do" in content


def test_gate_rejected_fails_with_planning_label(tmp_path: Path) -> None:
    rule = _rule("p/gated", ("gated.txt",), gate="gate:main")
    executor = UppercaseExecutor(tmp_path)
    gates = FakeGateChecker({"gate:main": "rejected"})
    scheduler, _store, layout = _make_scheduler(tmp_path, [rule], executor, gates=gates)

    summary = asyncio.run(scheduler.run("*"))

    assert summary.failed == ("p/gated[]",)
    events = read(layout.journal(summary.run_id)).events
    fail_events = [e for e in events if e.type == "rule_fail"]
    assert fail_events[0].failure_label == "planning"


# --- checks, missing outputs, executors ------------------------------------------------


def test_failing_check_fails_the_rule_and_blocks_downstream(tmp_path: Path) -> None:
    rules = [
        _rule("p/a", ("a.txt",), checks=("chk",)),
        _rule("p/b", ("b.txt",), inputs=(InputSpec(source="path", selector="a.txt"),)),
    ]
    executor = UppercaseExecutor(tmp_path)
    checks = FakeCheckRunner({"chk": make_check_result("chk", ok=False)})
    scheduler, _store, layout = _make_scheduler(tmp_path, rules, executor, checks=checks)

    summary = asyncio.run(scheduler.run("*"))

    assert summary.failed == ("p/a[]",)
    assert summary.blocked == ("p/b[]",)
    events = read(layout.journal(summary.run_id)).events
    fail_events = [e for e in events if e.type == "rule_fail"]
    assert fail_events[0].failure_label == "verification"
    assert checks.calls == ["chk"]

    run_stop = [e for e in events if e.type == "run_stop"][-1]
    assert run_stop.payload["blocked"] == ["p/b[]"]
    assert run_stop.payload["blocked_count"] == 1

    handoff_path = layout.run_dir(summary.run_id) / "HANDOFF.md"
    assert handoff_path.is_file()
    from chipgraph.core.state.handoff import build_handoff

    handoff = build_handoff(events, target="*")
    assert handoff.blocked == ("p/b[]",)


def test_missing_output_fails_verification(tmp_path: Path) -> None:
    rule = _rule("p/a", ("a.txt",))
    scheduler, _store, _layout = _make_scheduler(tmp_path, [rule], NoOutputExecutor())

    summary = asyncio.run(scheduler.run("*"))

    assert summary.failed == ("p/a[]",)


def test_no_executor_for_kind_fails_infra(tmp_path: Path) -> None:
    rule = _rule("p/a", ("a.txt",), kind="human")
    scheduler, _store, layout = _make_scheduler(tmp_path, [rule], None, executors={})

    summary = asyncio.run(scheduler.run("*"))

    assert summary.failed == ("p/a[]",)
    events = read(layout.journal(summary.run_id)).events
    fail_events = [e for e in events if e.type == "rule_fail"]
    assert fail_events[0].failure_label == "infra"


def test_agent_stub_fails_infra(tmp_path: Path) -> None:
    rule = _rule("p/a", ("a.txt",), kind="agent", role="rtl-writer")
    scheduler, _store, layout = _make_scheduler(
        tmp_path, [rule], None, executors={"agent": AgentStub()}
    )

    summary = asyncio.run(scheduler.run("*"))

    assert summary.failed == ("p/a[]",)
    events = read(layout.journal(summary.run_id)).events
    fail_events = [e for e in events if e.type == "rule_fail"]
    assert fail_events[0].failure_label == "infra"
    assert "M2" in fail_events[0].payload["message"]


def test_executor_failure_label_is_passed_through(tmp_path: Path) -> None:
    rule = _rule("p/a", ("a.txt",))
    executor = FailingExecutor(failure_label="context", message="bad context")
    scheduler, _store, layout = _make_scheduler(tmp_path, [rule], executor)

    summary = asyncio.run(scheduler.run("*"))

    assert summary.failed == ("p/a[]",)
    assert executor.calls == 1
    events = read(layout.journal(summary.run_id)).events
    fail_events = [e for e in events if e.type == "rule_fail"]
    assert fail_events[0].failure_label == "context"
    assert fail_events[0].payload["message"] == "bad context"


# --- diverged outputs and rewind -------------------------------------------------------


def test_diverged_output_fails_constraint_and_blocks_dependents(tmp_path: Path) -> None:
    executor = UppercaseExecutor(tmp_path)
    scheduler, _store, layout = _make_scheduler(tmp_path, _chain_rules(), executor)
    asyncio.run(scheduler.run("*"))

    (tmp_path / "a.txt").write_text("hand edited", encoding="utf-8")
    # 'a' has no inputs of its own, so this is purely an output edited by hand: diverged.

    summary = asyncio.run(scheduler.run("*"))

    assert summary.failed == ("p/a[]",)
    assert summary.blocked == ("p/b[]", "p/c[]")
    events = read(layout.journal(summary.run_id)).events
    fail_events = [e for e in events if e.type == "rule_fail"]
    assert fail_events[0].failure_label == "constraint"


def test_a_failed_rebuild_is_not_mistaken_for_a_hand_edit(tmp_path: Path) -> None:
    """The tool rewrites an output, its check fails, then the input is reverted: the next
    run rebuilds (`never_built`) instead of reporting the tool's leftover as `diverged`."""
    rules = [
        _rule(
            "p/a",
            ("a.txt",),
            inputs=(InputSpec(source="path", selector="in.txt"),),
            checks=("chk",),
        )
    ]
    source = tmp_path / "in.txt"
    source.write_text("x", encoding="utf-8")
    executor = UppercaseExecutor(tmp_path)
    checks = FakeCheckRunner({"chk": make_check_result("chk", ok=True)})
    scheduler, _store, _layout = _make_scheduler(tmp_path, rules, executor, checks=checks)
    assert asyncio.run(scheduler.run("*")).done == ("p/a[]",)

    source.write_text("y", encoding="utf-8")
    checks.results["chk"] = make_check_result("chk", ok=False)
    assert asyncio.run(scheduler.run("*")).failed == ("p/a[]",)
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "Y"

    source.write_text("x", encoding="utf-8")
    checks.results["chk"] = make_check_result("chk", ok=True)
    summary = asyncio.run(scheduler.run("*"))

    assert summary.done == ("p/a[]",)
    assert summary.failed == ()


class _HumanDone:
    """A `human` executor whose person has already written the outputs."""

    async def execute(self, rule: RuleSpec, instance: RuleInstance) -> ExecOutcome:
        return ExecOutcome(ok=True)


def test_editing_a_human_rule_output_rebuilds_it_and_its_dependents(tmp_path: Path) -> None:
    rules = [
        _rule("p/h", ("h.txt",), kind="human"),
        _rule("p/b", ("b.txt",), inputs=(InputSpec(source="path", selector="h.txt"),)),
    ]
    (tmp_path / "h.txt").write_text("spec v1", encoding="utf-8")
    executor = UppercaseExecutor(tmp_path)
    scheduler, _store, _layout = _make_scheduler(
        tmp_path, rules, executor, executors={"gen": executor, "human": _HumanDone()}
    )
    asyncio.run(scheduler.run("*"))

    (tmp_path / "h.txt").write_text("spec v2", encoding="utf-8")
    summary = asyncio.run(scheduler.run("*"))

    assert summary.failed == ()
    assert summary.done == ("p/b[]", "p/h[]")
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "SPEC V2"


def test_rewind_deletes_records_and_next_run_rebuilds(tmp_path: Path) -> None:
    executor = UppercaseExecutor(tmp_path)
    scheduler, _store, layout = _make_scheduler(tmp_path, _chain_rules(), executor)
    asyncio.run(scheduler.run("*"))

    rewound = scheduler.rewind("p/b[]")
    assert rewound == {"p/b[]", "p/c[]"}
    records = RecordStore(layout)
    assert records.get("p/a[]") is not None
    assert records.get("p/b[]") is None
    assert records.get("p/c[]") is None

    summary = asyncio.run(scheduler.run("*"))

    assert summary.ok
    assert set(summary.done) == {"p/b[]", "p/c[]"}
    assert summary.skipped_fresh == ("p/a[]",)
    assert executor.calls == {"p/a[]": 1, "p/b[]": 2, "p/c[]": 2}


# --- journal replay --------------------------------------------------------------------


def test_journal_sequence_is_valid_and_replay_matches_final_statuses(tmp_path: Path) -> None:
    executor = UppercaseExecutor(tmp_path)
    scheduler, _store, layout = _make_scheduler(tmp_path, _chain_rules(), executor)

    summary = asyncio.run(scheduler.run("*"))

    result = read(layout.journal(summary.run_id))
    assert result.truncated is False
    seqs = [e.seq for e in result.events]
    assert seqs == sorted(seqs)
    assert seqs == list(range(len(seqs)))
    assert len({e.run_id for e in result.events}) == 1

    state = replay(result.events)
    assert state.started is True
    assert state.stopped is True
    for iid in ("p/a[]", "p/b[]", "p/c[]"):
        assert state.rules[iid] == "done"


def test_gate_evaluator_satisfies_the_scheduler_gate_protocol(tmp_path: Path) -> None:
    from chipgraph.adapters.review.file import FileReview
    from chipgraph.core.engine import GateChecker, GateEvaluator
    from chipgraph.core.state.artifacts import ArtifactStore, LabelRules

    evaluator = GateEvaluator(FileReview(tmp_path / "d"), ArtifactStore(tmp_path, LabelRules()))
    assert isinstance(evaluator, GateChecker)
