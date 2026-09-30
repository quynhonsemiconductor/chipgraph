"""The agent task queue and its budget (`chipgraph.core.runtime`, M1-11)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from chipgraph.core.contracts import AgentResult, ArtifactRef, Budget, RuleInstance, RuleSpec
from chipgraph.core.plugin_api.types import AgentTask
from chipgraph.core.runtime import (
    DEFAULT_TOOL_CALL_CAP,
    AgentRuntimeExecutor,
    AgentTaskRecord,
    QueueError,
    TaskQueue,
    outcome_for,
    task_for,
)
from chipgraph.core.state.layout import StateLayout

TASK = "demo/write_a[]"


def _task(tries: int = 2, escalate: str | None = None) -> AgentTask:
    instance = RuleInstance(
        rule_id="demo/write_a",
        outputs=(ArtifactRef(kind="rtl", path="rtl/a.sv"),),
        instance_id=TASK,
    )
    budget = Budget(tries=tries, tier="small", escalate=escalate)  # type: ignore[arg-type]
    return AgentTask(
        instance=instance,
        role="author",
        skills=("lang/sv",),
        allowed_writes=("rtl/a.sv",),
        budget=budget,
    )


@pytest.fixture
def queue(tmp_path: Path) -> TaskQueue:
    return TaskQueue(StateLayout(tmp_path))


def _failed() -> AgentResult:
    return AgentResult(status="failed")


def test_enqueue_is_idempotent(queue: TaskQueue, tmp_path: Path) -> None:
    first = queue.enqueue(_task(), inputs_hash="h1", output_hashes={})
    assert first.status == "ready"
    assert (first.tries, first.tier, first.role, first.skills) == (
        2,
        "small",
        "author",
        ("lang/sv",),
    )
    assert first.allowed_writes == ("rtl/a.sv",)
    assert first.tool_call_cap == DEFAULT_TOOL_CALL_CAP == 80
    again = queue.enqueue(_task(), inputs_hash="h1", output_hashes={})
    assert again == first
    assert [r.task_id for r in queue.all()] == [TASK]
    # State lives under the state directory, never in the repo tree.
    [path] = (tmp_path / ".chipgraph" / "state" / "runtime" / "tasks").glob("*.json")
    assert AgentTaskRecord.model_validate_json(path.read_text()) == first


def test_dispatch_records_baseline_and_run(queue: TaskQueue) -> None:
    queue.enqueue(_task(), inputs_hash="h1", output_hashes={})
    record = queue.dispatch(TASK, run_id="r1", baseline={"b.txt": "x", "a.txt": "y"})
    assert (record.status, record.dispatches, record.run_id) == ("dispatched", 1, "r1")
    assert queue.baseline(TASK) == {"a.txt": "y", "b.txt": "x"}
    # Enqueue again while dispatched: unchanged.
    assert queue.enqueue(_task(), inputs_hash="h1", output_hashes={}).status == "dispatched"
    with pytest.raises(QueueError, match="cannot dispatch"):
        queue.dispatch(TASK, run_id="r2", baseline={})


def test_accept(queue: TaskQueue) -> None:
    queue.enqueue(_task(), inputs_hash="h1", output_hashes={})
    queue.dispatch(TASK, run_id="r1", baseline={})
    queue.mark_submitted(TASK)
    done = AgentResult(status="done", files_written=("rtl/a.sv",))
    record = queue.accept(TASK, result=done, output_hashes={"rtl/a.sv": "abc"})
    assert record.status == "accepted"
    assert record.accepted_hashes == {"rtl/a.sv": "abc"}
    assert record.last_result == done
    # Still accepted while inputs and outputs match; reopened when either changes.
    assert queue.enqueue(_task(), inputs_hash="h1", output_hashes={"rtl/a.sv": "abc"}) == record
    reopened = queue.enqueue(_task(), inputs_hash="h1", output_hashes={"rtl/a.sv": "zzz"})
    assert (reopened.status, reopened.attempts, reopened.dispatches) == ("ready", 0, 1)


def test_reject_counts_attempts_and_exhausts_the_budget(queue: TaskQueue) -> None:
    queue.enqueue(_task(tries=2, escalate="large"), inputs_hash="h1", output_hashes={})
    assert queue.get(TASK) is not None and queue.require(TASK).current_tier == "small"

    queue.dispatch(TASK, run_id="r1", baseline={})
    queue.mark_submitted(TASK)
    first = queue.reject(TASK, result=_failed(), reasons=("lint failed",), outside=("README.md",))
    assert (first.status, first.attempts) == ("rejected", 1)
    assert first.reasons == ("lint failed",) and first.outside == ("README.md",)
    assert first.last_result is not None and first.last_result.status == "failed"
    assert first.current_tier == "large"  # escalate after a rejection

    # A rejected task is dispatched again.
    queue.dispatch(TASK, run_id="r2", baseline={})
    queue.mark_submitted(TASK)
    second = queue.reject(TASK, result=_failed(), reasons=("lint failed again",))
    assert (second.status, second.attempts) == ("budget_exhausted", 2)
    assert second.last_result is not None and second.last_result.status == "budget_exhausted"
    with pytest.raises(QueueError):
        queue.dispatch(TASK, run_id="r3", baseline={})

    # Exhausted stays exhausted until the inputs change.
    assert queue.enqueue(_task(), inputs_hash="h1", output_hashes={}).status == "budget_exhausted"
    assert queue.enqueue(_task(), inputs_hash="h2", output_hashes={}).status == "ready"


def test_needs_human(queue: TaskQueue) -> None:
    queue.enqueue(_task(), inputs_hash="h1", output_hashes={})
    queue.dispatch(TASK, run_id="r1", baseline={})
    question = AgentResult(status="needs_human", open_questions=("which clock?",))
    record = queue.needs_human(TASK, result=question)
    assert (record.status, record.attempts, record.reasons) == ("needs_human", 0, ("which clock?",))
    with pytest.raises(QueueError, match="needs_human"):
        queue.needs_human(TASK, result=_failed())


def test_transitions_are_checked(queue: TaskQueue) -> None:
    with pytest.raises(QueueError, match="no agent task"):
        queue.require(TASK)
    queue.enqueue(_task(), inputs_hash="h1", output_hashes={})
    for bad in (
        lambda: queue.mark_submitted(TASK),
        lambda: queue.accept(TASK, result=_failed(), output_hashes={}),
        lambda: queue.reject(TASK, result=_failed(), reasons=()),
        lambda: queue.unsubmit(TASK),
    ):
        with pytest.raises(QueueError):
            bad()
    queue.dispatch(TASK, run_id="r1", baseline={})
    queue.mark_submitted(TASK)
    assert queue.unsubmit(TASK).status == "dispatched"


def test_tool_calls_count_the_current_dispatch_only(queue: TaskQueue) -> None:
    queue.enqueue(_task(), inputs_hash="h1", output_hashes={})
    queue.dispatch(TASK, run_id="r1", baseline={})
    queue.agents_dir.mkdir(parents=True)
    for name, dispatch, calls in (("a1", 1, 5), ("a2", 1, 2), ("old", 0 + 1, 0)):
        (queue.agents_dir / f"{name}.json").write_text(
            f'{{"agent_id": "{name}", "task_id": "{TASK}", "dispatch": {dispatch}, '
            f'"tool_calls": {calls}}}'
        )
    assert queue.tool_calls(TASK) == 7
    (queue.agents_dir / "bad.json").write_text("{}")
    with pytest.raises(QueueError, match="broken agent binding"):
        queue.bindings()


# --- the executor ---------------------------------------------------------------------


class _FakeRuntime:
    name = "fake"

    def __init__(self, result: AgentResult) -> None:
        self.result = result
        self.tasks: list[AgentTask] = []

    async def run_task(self, task: AgentTask) -> AgentResult:
        self.tasks.append(task)
        return self.result


def _rule() -> RuleSpec:
    return RuleSpec(
        id="demo/write_a",
        kind="agent",
        role="author",
        skills=("lang/sv",),
        outputs=("rtl/a.sv",),
        budget=Budget(tries=3),
    )


def test_executor_hands_the_instance_to_the_runtime() -> None:
    runtime = _FakeRuntime(AgentResult(status="done"))
    instance = _task().instance
    outcome = asyncio.run(AgentRuntimeExecutor(runtime).execute(_rule(), instance))
    assert outcome.ok
    [task] = runtime.tasks
    assert task.allowed_writes == ("rtl/a.sv",)
    assert (task.role, task.skills, task.budget.tries) == ("author", ("lang/sv",), 3)
    assert task_for(_rule(), instance) == task


def test_outcome_mapping() -> None:
    waiting = outcome_for(
        AgentResult(status="needs_human", open_questions=("run the plugin",)), task_id=TASK
    )
    assert (waiting.ok, waiting.failure_label, waiting.message) == (
        False,
        "planning",
        "run the plugin",
    )
    exhausted = outcome_for(AgentResult(status="budget_exhausted"), task_id=TASK)
    assert (exhausted.ok, exhausted.failure_label) == (False, "verification")
    assert "budget_exhausted" in exhausted.message
    assert outcome_for(AgentResult(status="failed"), task_id=TASK).failure_label == "verification"
