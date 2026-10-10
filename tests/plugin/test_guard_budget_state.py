"""M2-02b: the write guard accepts task records with the new optional budget fields and
still accepts records without them (`plugin/hooks/guard.py` validates every record)."""

from __future__ import annotations

import json
from pathlib import Path

from test_guard import GET_CONTEXT, bind, denied, event, no_decision, run_guard, write

from chipgraph.core.contracts import AgentResult, ArtifactRef, Budget, RuleInstance
from chipgraph.core.plugin_api.types import AgentTask
from chipgraph.core.runtime import TaskQueue
from chipgraph.core.runtime.queue import TaskBudgetState
from chipgraph.core.state.layout import StateLayout

TASK = "loop/a[]"


def _project(tmp_path: Path) -> tuple[Path, TaskQueue]:
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    queue = TaskQueue(StateLayout(project))
    instance = RuleInstance(
        rule_id="loop/a", outputs=(ArtifactRef(kind="rtl", path="rtl/a.sv"),), instance_id=TASK
    )
    task = AgentTask(
        instance=instance, role="author", allowed_writes=("rtl/a.sv",), budget=Budget(tries=2)
    )
    queue.enqueue(task, inputs_hash="0", output_hashes={}, max_dispatches=4)
    return project, queue


def test_records_with_the_budget_fields_are_accepted(tmp_path: Path) -> None:
    project, queue = _project(tmp_path)
    queue.dispatch(TASK, run_id="r1", baseline={})
    queue.mark_submitted(TASK)
    queue.reject(
        TASK,
        result=AgentResult(status="failed"),
        reasons=("x",),
        budget_state=TaskBudgetState(dispatches=1, tries_used=1, failed_tries=1, redo="fix"),
    )
    queue.dispatch(TASK, run_id="r2", baseline={})
    raw = json.loads(next(queue.tasks_dir.glob("*.json")).read_text())
    assert raw["max_dispatches"] == 4 and raw["budget_state"]["dispatches"] == 2

    bind(project, "agent-1", TASK)
    no_decision(run_guard(project, write(project, "rtl/a.sv", "agent-1")))
    assert "not an output" in denied(run_guard(project, write(project, "rtl/b.sv", "agent-1")))


def test_records_without_them_are_accepted_too(tmp_path: Path) -> None:
    project, queue = _project(tmp_path)
    queue.dispatch(TASK, run_id="r1", baseline={})
    path = next(queue.tasks_dir.glob("*.json"))
    raw = json.loads(path.read_text())
    del raw["max_dispatches"], raw["budget_state"]
    path.write_text(json.dumps(raw))

    no_decision(run_guard(project, event(GET_CONTEXT, agent="agent-2", task_id=TASK)))
    no_decision(run_guard(project, write(project, "rtl/a.sv", "agent-2")))
    assert queue.require(TASK).budget_state is None
