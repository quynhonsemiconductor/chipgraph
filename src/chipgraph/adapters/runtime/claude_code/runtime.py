"""`ClaudeCodeRuntime`: the `AgentRuntime` for `runtime: claude-code` (DESIGN.md 5.5, D35).

chipgraph never calls a model in this runtime. `run_task` only puts the task in the
queue (`chipgraph.core.runtime.TaskQueue`) and reports where it is:

- accepted, and its outputs still hash as accepted -> `done`;
- `budget_exhausted` -> `budget_exhausted`, with the last rejection as the message;
- `needs_human` -> `needs_human`, with the agent's (or the engine's) open questions;
- anything else -> `needs_human` with one question: run the plugin command in Claude
  Code, which fetches the task with `next_task`, runs it in a role subagent, and hands
  it back with `submit` (see `chipgraph.adapters.runtime.claude_code.service`).

So a `chipgraph build` stops at an agent rule like it stops at a `human` rule, and goes
on once the task is accepted.

`ClaudeCodeExecutor` is the build's executor for agent rules in this runtime. A task
whose loop stopped on a submit (its budget ran out, or the checks point at the spec:
`chipgraph.adapters.runtime.claude_code.loop`) fails with the label of its last failure
and the loop's `agent` payload, so HANDOFF.md says how many tries were used, why it
stopped and what failed last. It stays stopped until an input changes or
`chipgraph rewind` reopens it.
"""

from __future__ import annotations

from chipgraph.adapters.runtime.claude_code.loop import (
    MAX_INFRA_RETRIES,
    max_dispatches,
    stop_info,
    stop_message,
)
from chipgraph.core.contracts import AgentResult, RuleInstance, RuleSpec
from chipgraph.core.engine.scheduler import ExecOutcome
from chipgraph.core.plugin_api.types import AgentTask
from chipgraph.core.runtime import (
    DEFAULT_TOOL_CALL_CAP,
    AgentRuntimeExecutor,
    AgentTaskRecord,
    TaskQueue,
    instance_inputs_hash,
    outcome_for,
    task_for,
)
from chipgraph.core.state.artifacts import ArtifactStore

PLUGIN_NAME = "chipgraph"
"""The Claude Code plugin's name: agents are `chipgraph:<role>`, commands `/chipgraph:<name>`."""

RUN_COMMAND = f"/{PLUGIN_NAME}:run"
"""The plugin command that runs the task loop."""


def output_hashes(store: ArtifactStore, task: AgentTask) -> dict[str, str]:
    """Current content hash of each of the task's outputs that exists, keyed by path."""
    refs = [ref for ref in task.instance.outputs if ref.path is not None]
    current = store.current_hashes(refs)
    return {
        ref.path: current[f"{ref.repo}:{ref.path}"]
        for ref in refs
        if ref.path is not None and f"{ref.repo}:{ref.path}" in current
    }


def result_for(record: AgentTaskRecord) -> AgentResult:
    """What the queue record means for the build, as an `AgentResult`."""
    if record.status == "accepted":
        return AgentResult(status="done", files_written=record.allowed_writes)
    if record.status == "budget_exhausted":
        last = "; ".join(record.reasons) or "no reason recorded"
        return AgentResult(
            status="budget_exhausted",
            open_questions=(
                f"agent task {record.task_id!r} used all {record.tries} tries "
                f"(last rejection: {last}); fix its inputs or raise budget.tries, "
                "then build again (see HANDOFF.md)",
            ),
        )
    if record.status == "needs_human":
        questions = record.last_result.open_questions if record.last_result else ()
        return AgentResult(
            status="needs_human",
            open_questions=questions or (f"agent task {record.task_id!r} needs a person",),
        )
    return AgentResult(
        status="needs_human",
        open_questions=(
            f"waiting for an agent: run `{RUN_COMMAND}` in Claude Code "
            f"(task {record.task_id!r} is {record.status})",
        ),
    )


class ClaudeCodeRuntime:
    """Queues agent tasks for the user's Claude Code session; never calls a model."""

    name = "claude-code"

    def __init__(
        self,
        queue: TaskQueue,
        store: ArtifactStore,
        *,
        tool_call_cap: int = DEFAULT_TOOL_CALL_CAP,
        max_infra_retries: int = MAX_INFRA_RETRIES,
    ) -> None:
        self.queue = queue
        self.store = store
        self.tool_call_cap = tool_call_cap
        self.max_infra_retries = max_infra_retries

    def enqueue(self, task: AgentTask) -> AgentTaskRecord:
        """Queue `task` (idempotent, see `TaskQueue.enqueue`) and return its record."""
        return self.queue.enqueue(
            task,
            inputs_hash=instance_inputs_hash(self.store, task.instance),
            output_hashes=output_hashes(self.store, task),
            tool_call_cap=self.tool_call_cap,
            max_dispatches=max_dispatches(task.budget.tries, self.max_infra_retries),
        )

    async def run_task(self, task: AgentTask) -> AgentResult:
        return result_for(self.enqueue(task))


def stopped_outcome(record: AgentTaskRecord) -> ExecOutcome | None:
    """The outcome of a task whose loop stopped on a submit, or `None` if it did not."""
    state = record.budget_state
    if state is None or state.stop is None or state.label is None:
        return None
    if record.status not in ("budget_exhausted", "needs_human"):
        return None
    info = stop_info(record, stopped_earlier=True)
    message = (
        f"{stop_message(info)} (stopped in an earlier run; see HANDOFF.md; change an "
        f"input or run `chipgraph rewind {record.task_id}` to try again)"
    )
    payload: dict[str, object] = {"agent": info}
    questions = record.last_result.open_questions if record.last_result else ()
    if questions:
        payload["open_questions"] = list(questions)
    return ExecOutcome(ok=False, failure_label=state.label, message=message, payload=payload)


class ClaudeCodeExecutor(AgentRuntimeExecutor):
    """The executor for `kind: agent` rules in runtime claude-code.

    Like `AgentRuntimeExecutor` over a `ClaudeCodeRuntime`, except that a task whose
    loop stopped reports its label and the loop's `agent` payload (`stopped_outcome`),
    and that `Scheduler.rewind` reopens a stopped task with a fresh budget (`forget`).
    """

    def __init__(self, runtime: ClaudeCodeRuntime) -> None:
        super().__init__(runtime)
        self.cc_runtime = runtime

    async def execute(self, rule: RuleSpec, instance: RuleInstance) -> ExecOutcome:
        try:
            task = task_for(rule, instance)
        except ValueError as exc:
            return ExecOutcome(ok=False, failure_label="planning", message=str(exc))
        record = self.cc_runtime.enqueue(task)
        stopped = stopped_outcome(record)
        if stopped is not None:
            return stopped
        return outcome_for(result_for(record), task_id=instance.instance_id)

    def forget(self, instance_id: str) -> None:
        """Reopen a stopped task (`chipgraph rewind`): a person said to try again."""
        self.cc_runtime.queue.reopen(instance_id)


__all__ = [
    "PLUGIN_NAME",
    "RUN_COMMAND",
    "ClaudeCodeExecutor",
    "ClaudeCodeRuntime",
    "output_hashes",
    "result_for",
    "stopped_outcome",
]
