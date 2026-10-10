"""M2-02b: the task record's additive budget fields (`budget_state`, `max_dispatches`).

Old records (written before the fields existed) still load and dispatch; `dispatch`
counts the budget cycle and refuses past `max_dispatches`; `reject` may leave a try
unused (infra) or stop early; `reopen` starts a fresh cycle.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chipgraph.core.contracts import AgentResult, ArtifactRef, Budget, RuleInstance
from chipgraph.core.plugin_api.types import AgentTask
from chipgraph.core.runtime import QueueError, TaskQueue
from chipgraph.core.runtime.queue import TaskBudgetState, state_key
from chipgraph.core.state.layout import StateLayout

TASK = "demo/a[]"


def _task(tries: int = 2) -> AgentTask:
    instance = RuleInstance(
        rule_id="demo/a",
        outputs=(ArtifactRef(kind="rtl", path="rtl/a.sv"),),
        instance_id=TASK,
    )
    return AgentTask(
        instance=instance, role="author", allowed_writes=("rtl/a.sv",), budget=Budget(tries=tries)
    )


def _queue(tmp_path: Path) -> TaskQueue:
    return TaskQueue(StateLayout(tmp_path))


OLD_RECORD = {
    "schema_version": 1,
    "task_id": TASK,
    "rule_id": "demo/a",
    "role": "author",
    "skills": [],
    "allowed_writes": ["rtl/a.sv"],
    "tries": 3,
    "tier": "medium",
    "escalate": "large",
    "denied_reads": [],
    "status": "rejected",
    "attempts": 1,
    "dispatches": 1,
    "tool_call_cap": 80,
    "inputs_hash": "0",
    "run_id": "r1",
    "accepted_hashes": {},
    "reasons": ["check lint fail: x"],
    "outside": [],
    "last_result": None,
    "updated_at": "2026-10-01T00:00:00Z",
}
"""A record as M1-11/M2-01 wrote it: no `budget_state`, no `max_dispatches`."""


def test_an_old_record_loads_and_its_counters_stand_for_the_budget(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    queue.tasks_dir.mkdir(parents=True)
    (queue.tasks_dir / f"{state_key(TASK)}.json").write_text(json.dumps(OLD_RECORD))
    old = queue.require(TASK)
    assert old.budget_state is None and old.max_dispatches is None
    assert (old.state.dispatches, old.state.tries_used, old.state.failed_tries) == (1, 1, 1)
    assert old.current_tier == "large"

    record = queue.dispatch(TASK, run_id="r2", baseline={})
    assert record.budget_state is not None and record.budget_state.dispatches == 2
    assert record.state.tries_used == 1
    assert [r.task_id for r in queue.all()] == [TASK]


def test_dispatch_counts_the_cycle_and_stops_at_max_dispatches(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    queue.enqueue(_task(), inputs_hash="0", output_hashes={}, max_dispatches=2)
    for n in (1, 2):
        record = queue.dispatch(TASK, run_id="r", baseline={})
        assert record.state.dispatches == n
        queue.mark_submitted(TASK)
        failed = AgentResult(status="failed")
        queue.reject(TASK, result=failed, reasons=("x",), counted=False, exhausted=False)
    assert queue.require(TASK).status == "rejected"
    with pytest.raises(QueueError, match="limit"):
        queue.dispatch(TASK, run_id="r", baseline={})
    assert queue.require(TASK).dispatches == 2


def test_reject_without_a_try_or_stopping_early(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    queue.enqueue(_task(tries=3), inputs_hash="0", output_hashes={})
    queue.dispatch(TASK, run_id="r", baseline={})
    queue.mark_submitted(TASK)
    state = TaskBudgetState(dispatches=1, infra_failures=1, label="infra", redo="again")
    infra = queue.reject(
        TASK,
        result=AgentResult(status="failed"),
        reasons=("err",),
        counted=False,
        budget_state=state,
    )
    assert (infra.status, infra.attempts, infra.current_tier) == ("rejected", 0, "medium")
    assert infra.budget_state == state

    queue.dispatch(TASK, run_id="r", baseline={})
    queue.mark_submitted(TASK)
    early = queue.reject(
        TASK, result=AgentResult(status="failed"), reasons=("same",), exhausted=True
    )
    assert (early.status, early.attempts) == ("budget_exhausted", 1)
    assert early.last_result is not None and early.last_result.status == "budget_exhausted"


def test_reopen_starts_a_fresh_cycle_and_keeps_the_dispatch_count(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    queue.enqueue(_task(tries=1), inputs_hash="0", output_hashes={}, max_dispatches=1)
    queue.dispatch(TASK, run_id="r", baseline={})
    queue.mark_submitted(TASK)
    queue.reject(TASK, result=AgentResult(status="failed"), reasons=("x",))
    assert queue.require(TASK).status == "budget_exhausted"

    reopened = queue.reopen(TASK)
    assert reopened is not None and reopened.status == "ready"
    assert (reopened.attempts, reopened.budget_state, reopened.reasons) == (0, None, ())
    assert reopened.dispatches == 1
    assert queue.dispatch(TASK, run_id="r2", baseline={}).state.dispatches == 1
    assert queue.reopen(TASK) is None  # dispatched: left alone
    assert queue.reopen("demo/none[]") is None
