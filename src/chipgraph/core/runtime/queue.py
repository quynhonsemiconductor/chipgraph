"""The agent task queue: one record per ready agent rule instance (DESIGN.md 5.5, D35).

An agent runtime that runs outside the engine's process (for example the user's own
agent harness, driven through tool calls) cannot be called like a function: the engine
hands out tasks, the harness does them, and the engine checks the result. This module
keeps that hand-over as state, under the state backend (never in the repo tree)::

    <state>/runtime/tasks/<sha256(task_id)>.json      AgentTaskRecord   (engine writes)
    <state>/runtime/baselines/<sha256(task_id)>.json  file hashes at dispatch
    <state>/runtime/agents/<sha256(agent_id)>.json    AgentBinding      (write guard writes)

A task moves ``ready -> dispatched -> submitted -> accepted | rejected |
budget_exhausted | needs_human``; a ``rejected`` task is dispatched again until its
``tries`` are used up. Each dispatch also records what the task's agent must not read
(``denied_reads``, from its role's read policy), for the harness's guard. Every file has
a single writer, so the engine and the harness's write guard (a separate process) never
overwrite each other's updates: the guard owns the agent bindings and their tool-call
counts, the engine owns everything else.

Pure Python over JSON files: no subprocess, no VCS, no knowledge of any harness.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from chipgraph.core.contracts import AgentResult, ModelTier, RuleId
from chipgraph.core.plugin_api.types import AgentTask
from chipgraph.core.runtime.roles import find_role, model_ladder
from chipgraph.core.state.layout import StateLayout

TaskStatus = Literal[
    "ready",
    "dispatched",
    "submitted",
    "accepted",
    "rejected",
    "budget_exhausted",
    "needs_human",
]
"""Where an agent task is in its life: see the module docstring for the transitions."""

OPEN_STATUSES: frozenset[TaskStatus] = frozenset({"ready", "rejected"})
"""Statuses a task can be dispatched from."""

ACTIVE_STATUSES: frozenset[TaskStatus] = frozenset({"dispatched", "submitted"})
"""Statuses in which an agent may be working on the task, or the engine is checking it."""

DEFAULT_TOOL_CALL_CAP = 80
"""Tool calls one dispatch of a task may make, over every agent bound to it.

A harness's own turn limit may not count the work of the agents it starts (spike S7:
the turn limit counts only the main session), so the engine keeps this budget itself,
per task.
"""


class QueueError(Exception):
    """An unknown task, or a transition its current status does not allow."""


def _now() -> datetime:
    return datetime.now(UTC)


def state_key(value: str) -> str:
    """The file name stem for a task id or an agent id: its sha256 hex digest."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AgentTaskRecord(BaseModel):
    """One agent rule instance handed to an out-of-process agent runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    task_id: str = Field(min_length=1, description="The rule instance id this task fulfils.")
    rule_id: RuleId = Field(description="The rule this task is an instance of.")
    role: str = Field(min_length=1, description="The agent role that runs this task.")
    skills: tuple[str, ...] = Field(default=(), description="Skill ids available to the role.")
    allowed_writes: tuple[str, ...] = Field(
        min_length=1, description="Repo-relative paths the agent may write: the outputs."
    )
    tries: int = Field(ge=1, description="Submissions allowed before the budget is exhausted.")
    tier: ModelTier = Field(default="medium", description="Model tier for the first attempt.")
    escalate: ModelTier | None = Field(
        default=None, description="Model tier for later attempts, if the rule escalates."
    )
    denied_reads: tuple[str, ...] = Field(
        default=(),
        description=(
            "Repo-relative paths and globs the agent must not read (its role's read policy "
            "resolved against the project); set on every dispatch. The guard refuses reads "
            "of them, and listings or searches that could reach them."
        ),
    )
    status: TaskStatus = Field(default="ready", description="Where the task is in its life.")
    attempts: int = Field(default=0, ge=0, description="Rejected submissions so far.")
    dispatches: int = Field(default=0, ge=0, description="How many times it was handed out.")
    tool_call_cap: int = Field(
        default=DEFAULT_TOOL_CALL_CAP, ge=1, description="Tool calls allowed per dispatch."
    )
    inputs_hash: str = Field(description="Hash of the instance's inputs when it was enqueued.")
    run_id: str | None = Field(default=None, description="The run that last dispatched it.")
    accepted_hashes: dict[str, str] = Field(
        default_factory=dict, description="Output path to content hash, when accepted."
    )
    reasons: tuple[str, ...] = Field(default=(), description="Why the last submit was rejected.")
    outside: tuple[str, ...] = Field(
        default=(), description="Files the last rejected attempt changed outside its outputs."
    )
    last_result: AgentResult | None = Field(default=None, description="The last agent result.")
    updated_at: AwareDatetime = Field(default_factory=_now, description="Last change.")

    @property
    def current_tier(self) -> ModelTier:
        """The model tier for the next attempt: `escalate` once an attempt was rejected."""
        if self.attempts >= 1 and self.escalate is not None:
            return self.escalate
        return self.tier


class AgentBinding(BaseModel):
    """Which task one agent works on, and how many tool calls it made (guard-owned).

    Written only by the harness's write guard, which binds an agent to a task when the
    agent asks for that task's context. The engine only reads it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    agent_id: str = Field(min_length=1, description="The harness's id for the agent.")
    agent_type: str = Field(default="", description="The agent definition it runs.")
    task_id: str = Field(min_length=1, description="The task the agent is bound to.")
    dispatch: int = Field(ge=1, description="The task's `dispatches` count when it bound.")
    tool_calls: int = Field(default=0, ge=0, description="Tool calls the agent made so far.")


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class TaskQueue:
    """Reads and writes the agent task queue under a project's state directory."""

    def __init__(self, layout: StateLayout) -> None:
        self.layout = layout
        self.runtime_dir = layout.state_dir / "runtime"
        self.tasks_dir = self.runtime_dir / "tasks"
        self.baselines_dir = self.runtime_dir / "baselines"
        self.agents_dir = self.runtime_dir / "agents"

    # --- reading -------------------------------------------------------------------

    def _task_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{state_key(task_id)}.json"

    def get(self, task_id: str) -> AgentTaskRecord | None:
        """The record for `task_id`, or `None` if it was never enqueued."""
        path = self._task_path(task_id)
        if not path.is_file():
            return None
        return AgentTaskRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def require(self, task_id: str) -> AgentTaskRecord:
        """The record for `task_id`; raises `QueueError` if there is none."""
        record = self.get(task_id)
        if record is None:
            raise QueueError(f"no agent task {task_id!r}")
        return record

    def all(self) -> list[AgentTaskRecord]:
        """Every record, sorted by task id."""
        if not self.tasks_dir.is_dir():
            return []
        records = [
            AgentTaskRecord.model_validate_json(path.read_text(encoding="utf-8"))
            for path in self.tasks_dir.glob("*.json")
        ]
        return sorted(records, key=lambda r: r.task_id)

    def baseline(self, task_id: str) -> dict[str, str]:
        """The file hashes recorded when `task_id` was last dispatched (empty if none)."""
        path = self.baselines_dir / f"{state_key(task_id)}.json"
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()
        ):
            raise QueueError(f"baseline {path} is not a mapping of path to hash")
        return dict(data)

    def bindings(self, task_id: str | None = None) -> list[AgentBinding]:
        """Agent bindings written by the write guard, optionally only those for `task_id`.

        A binding file that does not validate raises `QueueError`: it is the guard's
        state, and a broken one means the guard is denying every call anyway.
        """
        if not self.agents_dir.is_dir():
            return []
        found: list[AgentBinding] = []
        for path in sorted(self.agents_dir.glob("*.json")):
            try:
                binding = AgentBinding.model_validate_json(path.read_text(encoding="utf-8"))
            except ValidationError as exc:
                raise QueueError(f"broken agent binding {path}: {exc}") from exc
            if task_id is None or binding.task_id == task_id:
                found.append(binding)
        return found

    def tool_calls(self, task_id: str) -> int:
        """Tool calls made so far in the task's current dispatch, over all its agents."""
        record = self.require(task_id)
        return sum(b.tool_calls for b in self.bindings(task_id) if b.dispatch == record.dispatches)

    # --- writing -------------------------------------------------------------------

    def _put(self, record: AgentTaskRecord) -> AgentTaskRecord:
        record = record.model_copy(update={"updated_at": _now()})
        _write_atomic(self._task_path(record.task_id), record.model_dump_json(indent=2))
        return record

    def enqueue(
        self,
        task: AgentTask,
        *,
        inputs_hash: str,
        output_hashes: Mapping[str, str],
        tool_call_cap: int = DEFAULT_TOOL_CALL_CAP,
    ) -> AgentTaskRecord:
        """Add `task` as `ready`, or return its existing record (idempotent).

        An existing record is reopened as a fresh `ready` task (attempts back to 0) when
        the work it did no longer holds: an `accepted` task whose inputs changed or whose
        outputs no longer hash as accepted, or a `budget_exhausted`/`needs_human` task
        whose inputs changed (a person fixed the spec, say). Otherwise it is unchanged.

        The model tiers come from the task's role (`RoleSpec.default_tier`,
        `escalate_to`) unless the rule's budget sets `tier` or `escalate` itself.
        """
        task_id = task.instance.instance_id
        existing = self.get(task_id)
        if existing is not None:
            inputs_changed = existing.inputs_hash != inputs_hash
            if existing.status == "accepted":
                outputs_changed = dict(output_hashes) != existing.accepted_hashes
                if not inputs_changed and not outputs_changed:
                    return existing
            elif existing.status in ("budget_exhausted", "needs_human"):
                if not inputs_changed:
                    return existing
            else:
                return existing
        tier, escalate = model_ladder(find_role(task.role), task.budget)
        record = AgentTaskRecord(
            task_id=task_id,
            rule_id=task.instance.rule_id,
            role=task.role,
            skills=task.skills,
            allowed_writes=task.allowed_writes,
            tries=task.budget.tries,
            tier=tier,
            escalate=escalate,
            tool_call_cap=tool_call_cap,
            inputs_hash=inputs_hash,
            dispatches=existing.dispatches if existing is not None else 0,
        )
        return self._put(record)

    def _transition(
        self, task_id: str, allowed: frozenset[TaskStatus], action: str
    ) -> AgentTaskRecord:
        record = self.require(task_id)
        if record.status not in allowed:
            raise QueueError(
                f"cannot {action} task {task_id!r}: it is {record.status!r} "
                f"(needs one of {sorted(allowed)})"
            )
        return record

    def dispatch(
        self,
        task_id: str,
        *,
        run_id: str,
        baseline: Mapping[str, str],
        denied_reads: Iterable[str] = (),
    ) -> AgentTaskRecord:
        """Hand the task out: `ready`/`rejected` -> `dispatched`, with a new baseline.

        `denied_reads` (the role's read policy resolved against the project as it is
        now) replaces the record's, in the same write as the status change, so the
        guard never sees the task dispatched without it.
        """
        record = self._transition(task_id, OPEN_STATUSES, "dispatch")
        _write_atomic(
            self.baselines_dir / f"{state_key(task_id)}.json",
            json.dumps(dict(sorted(baseline.items())), indent=1),
        )
        return self._put(
            record.model_copy(
                update={
                    "status": "dispatched",
                    "dispatches": record.dispatches + 1,
                    "run_id": run_id,
                    "denied_reads": tuple(sorted(set(denied_reads))),
                }
            )
        )

    def mark_submitted(self, task_id: str) -> AgentTaskRecord:
        """`dispatched` -> `submitted`: the engine is checking the work (no more writes)."""
        record = self._transition(task_id, frozenset({"dispatched"}), "submit")
        return self._put(record.model_copy(update={"status": "submitted"}))

    def unsubmit(self, task_id: str) -> AgentTaskRecord:
        """`submitted` -> `dispatched`, when checking the work failed for reasons of its own."""
        record = self._transition(task_id, frozenset({"submitted"}), "unsubmit")
        return self._put(record.model_copy(update={"status": "dispatched"}))

    def accept(
        self, task_id: str, *, result: AgentResult, output_hashes: Mapping[str, str]
    ) -> AgentTaskRecord:
        """`submitted` -> `accepted`, remembering the output hashes that were accepted."""
        record = self._transition(task_id, frozenset({"submitted"}), "accept")
        return self._put(
            record.model_copy(
                update={
                    "status": "accepted",
                    "accepted_hashes": dict(output_hashes),
                    "reasons": (),
                    "outside": (),
                    "last_result": result,
                }
            )
        )

    def reject(
        self,
        task_id: str,
        *,
        result: AgentResult,
        reasons: tuple[str, ...],
        outside: tuple[str, ...] = (),
    ) -> AgentTaskRecord:
        """`submitted` -> `rejected`, or `budget_exhausted` once every try is used.

        The stored result's status says which: `failed` or `budget_exhausted`.
        """
        record = self._transition(task_id, frozenset({"submitted"}), "reject")
        attempts = record.attempts + 1
        exhausted = attempts >= record.tries
        status: TaskStatus = "budget_exhausted" if exhausted else "rejected"
        final = result.model_copy(update={"status": "budget_exhausted" if exhausted else "failed"})
        return self._put(
            record.model_copy(
                update={
                    "status": status,
                    "attempts": attempts,
                    "reasons": reasons,
                    "outside": outside,
                    "last_result": final,
                }
            )
        )

    def needs_human(self, task_id: str, *, result: AgentResult) -> AgentTaskRecord:
        """`dispatched`/`submitted` -> `needs_human`: a person must answer `open_questions`."""
        if result.status != "needs_human":
            raise QueueError("needs_human needs an AgentResult with status 'needs_human'")
        record = self._transition(task_id, ACTIVE_STATUSES, "hand to a person")
        return self._put(
            record.model_copy(
                update={
                    "status": "needs_human",
                    "reasons": result.open_questions,
                    "last_result": result,
                }
            )
        )


__all__ = [
    "ACTIVE_STATUSES",
    "DEFAULT_TOOL_CALL_CAP",
    "OPEN_STATUSES",
    "AgentBinding",
    "AgentTaskRecord",
    "QueueError",
    "TaskQueue",
    "TaskStatus",
    "state_key",
]
