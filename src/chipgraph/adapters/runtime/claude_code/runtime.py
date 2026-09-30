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
"""

from __future__ import annotations

from chipgraph.core.contracts import AgentResult
from chipgraph.core.plugin_api.types import AgentTask
from chipgraph.core.runtime import (
    DEFAULT_TOOL_CALL_CAP,
    AgentTaskRecord,
    TaskQueue,
    instance_inputs_hash,
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
    ) -> None:
        self.queue = queue
        self.store = store
        self.tool_call_cap = tool_call_cap

    async def run_task(self, task: AgentTask) -> AgentResult:
        record = self.queue.enqueue(
            task,
            inputs_hash=instance_inputs_hash(self.store, task.instance),
            output_hashes=output_hashes(self.store, task),
            tool_call_cap=self.tool_call_cap,
        )
        return result_for(record)


__all__ = ["PLUGIN_NAME", "RUN_COMMAND", "ClaudeCodeRuntime", "output_hashes", "result_for"]
