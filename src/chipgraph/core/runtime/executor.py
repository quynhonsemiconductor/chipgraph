"""`AgentRuntimeExecutor`: runs `kind: agent` rules through an `AgentRuntime` (DESIGN.md 5.2).

The scheduler knows `Executor`s; agent runtimes implement `AgentRuntime.run_task`. This
executor turns a rule instance into an `AgentTask` (role, skills, budget, and the
instance's concrete outputs as the only files the agent may write) and maps the
runtime's `AgentResult` back to an `ExecOutcome`:

- `done` -> ok; the scheduler then runs the rule's checks and records the outputs.
- `needs_human` -> not ok (`planning`): the open questions are the message. A runtime
  that runs outside the engine uses this for "the task is waiting for an agent" too.
- `budget_exhausted` / `failed` -> not ok (`verification`).
"""

from __future__ import annotations

from chipgraph.core.contracts import AgentResult, RuleInstance, RuleSpec
from chipgraph.core.contracts.types import FailureLabel
from chipgraph.core.engine.scheduler import ExecOutcome
from chipgraph.core.plugin_api.protocols import AgentRuntime
from chipgraph.core.plugin_api.types import AgentTask
from chipgraph.core.state.artifacts import ArtifactStore, hash_inputs


def instance_inputs_hash(store: ArtifactStore, instance: RuleInstance) -> str:
    """A hash over the instance's inputs, the same way the scheduler's records hash them.

    File inputs hash their content (a missing file counts as empty); model inputs are
    keyed by their model key only, since the Design Model hashes its own entities.
    """
    path_refs = [ref for ref in instance.inputs if ref.path is not None]
    current = store.current_hashes(path_refs)
    pairs = []
    for ref in instance.inputs:
        locator = ref.path if ref.path is not None else ref.model_key
        key = f"{ref.repo}:{locator}"
        pairs.append((key, current.get(key, "")))
    return hash_inputs(pairs)


def task_for(rule: RuleSpec, instance: RuleInstance) -> AgentTask:
    """The `AgentTask` for one instance of an agent rule."""
    if rule.role is None:
        raise ValueError(f"rule {rule.id!r} has no role")
    writes = tuple(ref.path for ref in instance.outputs if ref.path is not None)
    return AgentTask(
        instance=instance,
        role=rule.role,
        skills=rule.skills,
        allowed_writes=writes,
        budget=rule.budget,
    )


def outcome_for(result: AgentResult, *, task_id: str) -> ExecOutcome:
    """Map an `AgentResult` to the scheduler's `ExecOutcome`."""
    if result.status == "done":
        return ExecOutcome(ok=True)
    label: FailureLabel = "planning" if result.status == "needs_human" else "verification"
    detail = "; ".join(result.open_questions)
    default = f"agent task {task_id!r} ended with status {result.status!r}"
    return ExecOutcome(ok=False, failure_label=label, message=detail or default)


class AgentRuntimeExecutor:
    """An `Executor` for `kind: agent` rules, backed by an `AgentRuntime`."""

    def __init__(self, runtime: AgentRuntime) -> None:
        self.runtime = runtime

    async def execute(self, rule: RuleSpec, instance: RuleInstance) -> ExecOutcome:
        try:
            task = task_for(rule, instance)
        except ValueError as exc:
            return ExecOutcome(ok=False, failure_label="planning", message=str(exc))
        result = await self.runtime.run_task(task)
        return outcome_for(result, task_id=instance.instance_id)


__all__ = ["AgentRuntimeExecutor", "instance_inputs_hash", "outcome_for", "task_for"]
