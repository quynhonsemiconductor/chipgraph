"""What the MCP tools of runtime `claude-code` do: `next_task`, `get_context`, `submit`.

The loop (DESIGN.md 5.5, spike S7), driven by the plugin command in the user's Claude
Code session::

    next_task(target)   run the build; every agent task it reaches is queued; hand out
                        the ready ones (several at once: they run in parallel)
    get_context(id)     a role subagent asks for its task: inputs as text, outputs,
                        role, skills and their text (refused when an input is labelled
                        `nda`, or is of a kind the task's role must not see)
    submit(id, result)  the engine checks the work at once: every file changed since
                        dispatch is one of the task's outputs, the outputs exist and are
                        not empty, the rule's checks pass; accept, or reject with the
                        failure's label, the failed checks and the redo instruction

`submit` runs the M2-02a agent rule loop one attempt at a time (`loop`): a failure is
labelled, a try is counted (an infra failure is not, within `max_infra_retries`), the
model tier escalates after the first failed try, and two tries in a row with the same
outputs and failures stop early. When the budget is gone the task turns
`budget_exhausted`, its `rule_fail` (with the label and the loop's `agent` summary) goes
into the journal of the run that dispatched it, and that run's HANDOFF.md is written at
once. `next_task` never hands a task out more than `tries + max_infra_retries` times,
never hands out a stopped task, and reports it (and every rule that depends on it)
under `blocked`; with nothing left to run it answers `stopped: true` and the HANDOFF
path.

A role that must not see some artifact kinds gets a guarded context (`guarded`). The
testbench Author (no `rtl`): its model inputs are the block's spec side and the module's
interface (spec ports, else the model's RTL ports, else the RTL declaration only), never
a general model slice. The RTL Author (no `tb`, M2-04): an `rtl` section with the
module's approved plan entry, requirements, interface, dependencies, naming rules and
style exemplars, and nothing from a testbench. For both, what `submit` tells it of
failed checks goes through `FeedbackFilter` (no issue in a denied file, no denied path,
no source excerpt; the redo text says when something was withheld, in words for the kind
withheld). The journal keeps the full results.

A role with no file-reading tool (`tb-author`, `rtl-author`) cannot overwrite a file
in Claude Code, which refuses a `Write` over a file the agent has not read. So at each
dispatch of such a role `next_task` moves every output that exists into the state
backend (`previous`): the agent's earlier attempt, or a file that was there before the
task. `get_context` shows them (`previous_outputs`: the last attempt's text, and a
pre-existing file's under `existing_output`, capped), the agent writes each file anew,
and a pre-existing file is put back, byte for byte with its mode, when the task ends
without being accepted (`budget_exhausted`, `needs_human`, a rewind) or the dispatch
fails in the engine. Roles with a read tool edit in place, as before.

For a rule whose first check is `format`, that check (the engine, not the agent) formats
the outputs in place before the other checks; the hashes `submit` records are taken
after the checks, so they are the formatted files'.

A role that writes no files but has `engine_writes` (the Critic) reviews a change
instead (`review`): its context holds the diff, the spec slice and the model slice; its
reply is a JSON review the main session passes as `result.review`; `submit` validates it
and the engine itself writes it as the task's one output.

A person who rejects a gate with a note (`approve(reject=true, note=...)`, or
`chipgraph approve <gate> --reject --note ...`) asks for a revision (`_revise_rejected`):
when every file the gate covers is an accepted agent task's output, the next `next_task`
reopens that task (a new budget cycle), rewinds it in the build and hands it out again;
its context starts `previous_rejection` with the note until it is accepted again. The
answer lists it under `revised`. The gate then waits for the new output.

The engine decides everything (P3): which task, which role, which files, which checks.
Claude Code only runs the model. Each role's tool table (`chipgraph.core.runtime.roles`)
picks the subagent (`chipgraph:<role>`), its model per attempt (the role's tier, then its
escalation tier after a rejection, unless the rule sets its own), and what it must not
read (`denied_reads`, set on dispatch). The guard hook (`plugin/hooks/guard.py`) blocks
writes outside a task's outputs and reads of its denied paths while the agent works;
`submit` checks the diff again.

Every function takes a freshly loaded `AppContext` and raises `AppError` for anything
the caller should see as a tool error.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from chipgraph.adapters.runtime.claude_code import loop
from chipgraph.adapters.runtime.claude_code import review as review_mod
from chipgraph.adapters.runtime.claude_code.agents import DEFAULT_TIER_MODELS, agent_type
from chipgraph.adapters.runtime.claude_code.guarded import (
    GuardedContext,
    RtlAuthorContext,
    guarded_context,
)
from chipgraph.adapters.runtime.claude_code.isolation import (
    CopyBackResult,
    best_of_n,
    candidate_id,
    check_overlapping_writes,
    copy_outputs_back,
    create_workspace,
    diff_workspace,
    input_paths_for,
    is_candidate_id,
    max_parallel_limit,
    parse_candidate_id,
    remove_workspace,
    should_isolate,
    sweep_workspaces,
)
from chipgraph.adapters.runtime.claude_code.previous import (
    MoveToken,
    PreviousOutputs,
    moves_outputs,
)
from chipgraph.adapters.runtime.claude_code.runtime import output_hashes
from chipgraph.app.build import graph_artifacts, make_scheduler, pack_search_paths
from chipgraph.app.checks import ProfileCheckRunner
from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.core.contracts import (
    AgentResult,
    Approval,
    CheckResult,
    Event,
    RuleInstance,
    RuleSpec,
)
from chipgraph.core.contracts.event import EventType
from chipgraph.core.contracts.types import FailureLabel, ModelTier
from chipgraph.core.engine.agent_rule import OUTPUTS_CHECK, FeedbackFilter, attempt_checks
from chipgraph.core.engine.gate import gate_id_for
from chipgraph.core.engine.graph import BuildGraph, kind_for
from chipgraph.core.engine.scheduler import RunSummary, Scheduler
from chipgraph.core.engine.select import (
    VARIANTS,
    Candidate,
    Selection,
    Spend,
    error_count,
    plan_budget,
    select_best,
    warning_count,
)
from chipgraph.core.model import ModelQuery, ModelStore, QueryError, default_model_db_path
from chipgraph.core.plugin_api.pack import discover_packs
from chipgraph.core.plugin_api.registry import PluginError
from chipgraph.core.runtime import (
    ACTIVE_STATUSES,
    OPEN_STATUSES,
    AgentTaskRecord,
    QueueError,
    TaskQueue,
    instance_inputs_hash,
    state_key,
    task_for,
)
from chipgraph.core.runtime.queue import TaskBudgetState
from chipgraph.core.runtime.roles import (
    ReviewReport,
    RoleSpec,
    SkillError,
    SkillSet,
    SkillSpec,
    denied_kind,
    find_role,
    load_skills,
    parse_review,
    path_denied,
    review_problems,
    task_denied_reads,
)
from chipgraph.core.state import journal as journal_mod
from chipgraph.core.state.artifacts import ArtifactStore, hash_file
from chipgraph.core.state.handoff import build_handoff, write_handoff

_MAX_INPUT_CHARS = 200_000
"""Longest input text `get_context` returns per file; longer files are cut."""

_NOT_AGENT_WORK = (".git/", ".chipgraph/state/", ".chipgraph/decisions/", ".claude/")
"""Paths never counted as an agent's changes: VCS and chipgraph state, gate decisions a
person records during the run, and Claude Code's own project settings. The write guard
still refuses agent writes to all of them."""


class SubmitReport(BaseModel):
    """What the role subagent reported, passed along by the main session on `submit`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["done", "needs_human"] = Field(
        default="done",
        description="'needs_human' when the task cannot be done without a person's answer.",
    )
    files_written: tuple[str, ...] = Field(
        default=(), description="Files the agent says it wrote (the engine checks the diff)."
    )
    assumptions: tuple[str, ...] = Field(default=(), description="Assumptions the agent made.")
    open_questions: tuple[str, ...] = Field(
        default=(), description="Questions for a person; required when status is needs_human."
    )
    review: dict[str, Any] | str | None = Field(
        default=None,
        description=(
            "Review tasks only (agent chipgraph:critic): the subagent's JSON review, as an "
            "object or its text. The engine validates it and writes it as the task's output."
        ),
    )


# --- Best-of-N round tracking -----------------------------------------------------------


class CandidateState(BaseModel):
    """State of one candidate in a best-of-N round."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate: int = Field(ge=1, description="Candidate number (1..N).")
    variant: str = Field(description="The variant hint for this candidate.")
    task_id: str = Field(description="The candidate's task id (base_task#candidate).")
    workspace: str = Field(description="Absolute path to the candidate's workspace.")
    status: Literal["dispatched", "submitted", "done", "failed", "cancelled"] = Field(
        default="dispatched"
    )
    checks: tuple[CheckResult, ...] = Field(default=(), description="Check results.")
    passed: bool = Field(default=False, description="Whether the candidate passed.")
    errors: int = Field(default=0, ge=0)
    warnings: int = Field(default=0, ge=0)
    spend: Spend = Field(default_factory=Spend)


class BestOfNRound(BaseModel):
    """State of one best-of-N round for a rule instance.

    Stored under <state>/runtime/best-of/<sha256(task_id)>.json while in progress.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1)
    base_task_id: str = Field(description="The original task id (without candidate suffix).")
    run_id: str = Field(description="The run that started this round.")
    round_num: int = Field(ge=1, description="Round number (1-based).")
    n: int = Field(ge=2, description="Number of candidates in this round.")
    candidates: tuple[CandidateState, ...] = Field(description="State of each candidate.")
    ledger_tries: int = Field(description="Total tries in the shared ledger.")
    ledger_used: int = Field(default=0, ge=0, description="Tries used so far.")
    winner: int | None = Field(default=None, description="Winning candidate number, if decided.")
    selection: Selection | None = Field(default=None, description="The selection result.")
    status: Literal["running", "selecting", "done", "failed"] = Field(default="running")


def _best_of_path(ctx: AppContext, task_id: str) -> Path:
    """Path to a best-of-N round state file."""
    return ctx.layout.state_dir / "runtime" / "best-of" / f"{state_key(task_id)}.json"


def _load_best_of_round(ctx: AppContext, task_id: str) -> BestOfNRound | None:
    """Load a best-of-N round state, or None if not in a round."""
    path = _best_of_path(ctx, task_id)
    if not path.is_file():
        return None
    try:
        return BestOfNRound.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_best_of_round(ctx: AppContext, round_state: BestOfNRound) -> None:
    """Save a best-of-N round state."""
    path = _best_of_path(ctx, round_state.base_task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(round_state.model_dump_json(indent=2), encoding="utf-8")


def _remove_best_of_round(ctx: AppContext, task_id: str) -> None:
    """Remove a best-of-N round state file."""
    path = _best_of_path(ctx, task_id)
    path.unlink(missing_ok=True)


# --- shared helpers ---------------------------------------------------------------------


def _require_runtime(ctx: AppContext) -> None:
    runtime = ctx.require_profile().profile.runtime
    if runtime != "claude-code":
        raise AppError(
            f"this project's profile sets runtime {runtime!r}; next_task, get_context and "
            "submit serve runtime 'claude-code' only"
        )


def _queue(ctx: AppContext) -> TaskQueue:
    return TaskQueue(ctx.layout)


def _record(queue: TaskQueue, task_id: str) -> AgentTaskRecord:
    try:
        return queue.require(task_id)
    except QueueError as exc:
        raise AppError(f"{exc}; call next_task for the tasks that exist") from exc


def _graph(ctx: AppContext) -> BuildGraph:
    return make_scheduler(ctx, "*").graph


def _instance(graph: BuildGraph, task_id: str) -> tuple[RuleSpec, RuleInstance]:
    instance = graph.instances.get(task_id)
    if instance is None and is_candidate_id(task_id):  # best-of-N: `<instance id>#<n>`
        try:
            instance = graph.instances.get(parse_candidate_id(task_id)[0])
        except ValueError:
            instance = None
    if instance is None:
        raise AppError(f"task {task_id!r} is no longer a rule instance of this project's graph")
    return graph.rules[instance.rule_id], instance


def _agent_for(role: str) -> str:
    """The plugin subagent for a role: `chipgraph:<role>` (namespace dropped)."""
    return agent_type(role)


def _model_for(ctx: AppContext, record: AgentTaskRecord) -> str:
    """The model of the task's next attempt: its current tier, mapped by the profile."""
    return _tier_model(ctx, record.current_tier)


def _tier_model(ctx: AppContext, tier: ModelTier) -> str:
    configured = ctx.require_profile().profile.models.tiers.get(tier)
    return configured or DEFAULT_TIER_MODELS[tier]


def _skill_set(ctx: AppContext) -> SkillSet:
    """Every skill the profile's packs provide."""
    profile = ctx.require_profile().profile
    try:
        available = discover_packs(pack_search_paths(ctx))
        return load_skills(available[name] for name in profile.packs if name in available)
    except (PluginError, SkillError) as exc:
        raise AppError(str(exc)) from exc


def _check_skills(ctx: AppContext, graph: BuildGraph) -> SkillSet:
    """Resolve every agent rule's skills for its role; an unknown or misused one is an error."""
    skills = _skill_set(ctx)
    for rule in sorted(graph.rules.values(), key=lambda r: r.id):
        if rule.kind == "agent" and rule.role is not None:
            try:
                skills.resolve(rule.skills, rule.role)
            except SkillError as exc:
                raise AppError(f"rule {rule.id!r}: {exc}") from exc
    return skills


def _denied_reads(ctx: AppContext, graph: BuildGraph, record: AgentTaskRecord) -> tuple[str, ...]:
    """What the task's agent must not read: its role's read policy over this project."""
    return task_denied_reads(
        record.role,
        artifacts=graph_artifacts(graph),
        profile=ctx.require_profile().profile,
        keep=record.allowed_writes,
    )


def _emit(
    ctx: AppContext,
    run_id: str | None,
    task_id: str,
    type_: EventType,
    payload: Mapping[str, Any],
    *,
    failure_label: FailureLabel | None = None,
) -> None:
    """Append one event about `task_id` to the journal of the run that dispatched it."""
    if run_id is None:
        return
    journal = journal_mod.Journal(ctx.layout.journal(run_id))
    journal.append(
        Event(
            run_id=run_id,
            seq=journal.next_seq(),
            ts=datetime.now(UTC),
            type=type_,
            rule_instance=task_id,
            payload=dict(payload),
            failure_label=failure_label,
        )
    )


def _handoff_path(ctx: AppContext, run_id: str) -> Path:
    """Where a run's HANDOFF.md is (`write_handoff` writes it there)."""
    return ctx.layout.run_dir(run_id) / "HANDOFF.md"


def _write_run_handoff(ctx: AppContext, run_id: str) -> Path | None:
    """Write run `run_id`'s HANDOFF.md again from its journal; `None` if that failed.

    As in the scheduler, a HANDOFF.md that cannot be written (an `OSError`) never fails
    the call itself.
    """
    try:
        events = journal_mod.read(ctx.layout.journal(run_id)).events
        target = journal_mod.read_manifest(ctx.layout, run_id).target
        return write_handoff(ctx.layout, build_handoff(events, target=target))
    except OSError:
        return None


async def snapshot(ctx: AppContext, extra: Iterable[str] = ()) -> dict[str, str]:
    """Content hash of every project file an agent could have changed, keyed by path.

    Files come from `git ls-files --cached --others --exclude-standard` (tracked plus
    untracked, without ignored ones such as build products); without git, from a walk
    of the tree. `extra` paths (a task's outputs) are always included.
    """
    runner = ctx.registry.get("runner", "local")
    result = await runner.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ctx.root
    )
    if result.returncode == 0:
        paths = {p for p in result.stdout.split("\0") if p}
    else:
        paths = set()
        for dirpath, dirnames, filenames in os.walk(ctx.root):
            rel_dir = Path(dirpath).relative_to(ctx.root).as_posix()
            dirnames[:] = [
                d
                for d in dirnames
                if not f"{'' if rel_dir == '.' else rel_dir + '/'}{d}/".startswith(_NOT_AGENT_WORK)
            ]
            for name in filenames:
                paths.add(name if rel_dir == "." else f"{rel_dir}/{name}")
    paths |= set(extra)
    hashes: dict[str, str] = {}
    for rel in sorted(paths):
        if rel.startswith(_NOT_AGENT_WORK):
            continue
        full = ctx.root / rel
        if full.is_file():
            hashes[rel] = hash_file(full)
    return hashes


def _changed(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))


def _engine_outputs(graph: BuildGraph) -> set[str]:
    """Outputs of the graph's non-agent rules: the engine itself writes those."""
    return {
        ref.path
        for instance in graph.instances.values()
        if graph.rules[instance.rule_id].kind != "agent"
        for ref in instance.outputs
        if ref.path is not None
    }


def _task_entry(
    ctx: AppContext,
    queue: TaskQueue,
    record: AgentTaskRecord,
    *,
    candidate: int | None = None,
    variant: str | None = None,
) -> dict[str, Any]:
    """Build a task entry for the next_task response.

    For best-of-N candidates, `candidate` is the candidate number (1..N) and `variant`
    is the variant hint text.
    """
    state = record.state
    if review_mod.is_review_role(find_role(record.role)):
        prompt = (
            f"You are doing chipgraph review task {record.task_id!r}. First call get_context "
            f"with task_id={record.task_id!r}; write no file; finish with the review as one "
            "JSON object."
        )
        extra: dict[str, Any] = {"reply": "review"}
    else:
        prompt = (
            f"You are doing chipgraph task {record.task_id!r}. First call get_context with "
            f"task_id={record.task_id!r}, then write only the files in its outputs."
        )
        extra = {}
    entry = {
        "task_id": record.task_id,
        "rule": record.rule_id,
        "role": record.role,
        "agent": _agent_for(record.role),
        "model": _model_for(ctx, record),
        "tier": record.current_tier,
        "skills": list(record.skills),
        "outputs": list(record.allowed_writes),
        "attempt": record.dispatches,
        "tries": record.tries,
        "try": min(state.tries_used + 1, record.tries),
        "tries_left": max(record.tries - state.tries_used, 0),
        "dispatches": state.dispatches,
        "max_dispatches": loop.dispatch_limit(record),
        "previous_label": state.label,
        "tool_calls": queue.tool_calls(record.task_id),
        "tool_call_cap": record.tool_call_cap,
        "prompt": prompt,
        **extra,
    }
    # Add candidate info if this is a best-of-N candidate
    if candidate is not None:
        entry["candidate"] = candidate
        entry["variant"] = variant or ""
    return entry


def _blocked(
    ctx: AppContext,
    graph: BuildGraph,
    queue: TaskQueue,
    summary: RunSummary,
    skip: set[str],
) -> list[dict[str, Any]]:
    """Why the build stopped, per instance (from the journal of `summary`'s run).

    Gates; failed rules with their label (an agent task also with its queue status, and
    the HANDOFF path when its loop stopped); and every rule that depends on one of
    those, with the ones it waits for (`blocked_by`). `skip` holds instances reported
    elsewhere (handed out, in progress, or blocked for a reason of their own): they and
    rules that wait only for them are left out.
    """
    events = journal_mod.read(ctx.layout.journal(summary.run_id)).events
    gates = {
        e.rule_instance: str(e.payload.get("gate", "")) for e in events if e.type == "gate_wait"
    }
    fails = {e.rule_instance: e for e in events if e.type == "rule_fail"}
    handoff = str(_handoff_path(ctx, summary.run_id))
    blocked: list[dict[str, Any]] = []
    reported: list[str] = []
    for iid in summary.waiting_gate:
        blocked.append(
            {"instance": iid, "reason": f"waiting for gate {gates.get(iid, '?')!r} to be approved"}
        )
        reported.append(iid)
    for iid in summary.failed:
        if iid in skip:
            continue
        event = fails.get(iid)
        entry: dict[str, Any] = {
            "instance": iid,
            "reason": (str(event.payload.get("message", "")) if event else "") or "failed",
            "label": event.failure_label if event else None,
        }
        record = queue.get(iid)
        if record is not None:
            entry["status"] = record.status
            if record.status in ("budget_exhausted", "needs_human"):
                entry["handoff"] = handoff
        blocked.append(entry)
        reported.append(iid)
    waits: dict[str, set[str]] = {}
    waiting = set(summary.blocked)
    for iid in reported:
        for dependent in graph.downstream(iid) & waiting:
            waits.setdefault(dependent, set()).add(iid)
    for dependent in sorted(waits):
        on = sorted(waits[dependent])
        blocked.append(
            {
                "instance": dependent,
                "reason": f"depends on {', '.join(on)}, which did not finish",
                "blocked_by": on,
            }
        )
    return blocked


# --- revise: a person rejected a gate with a note (M2-11b) ------------------------------


def _revision_path(ctx: AppContext, task_id: str) -> Path:
    """Where a task's pending revision note is kept (state backend, engine-owned)."""
    return ctx.layout.state_dir / "runtime" / "revisions" / f"{state_key(task_id)}.json"


def _revision(ctx: AppContext, task_id: str) -> dict[str, Any] | None:
    """The person's note the task's next attempts must answer, if any."""
    path = _revision_path(ctx, task_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _revision_text(revision: Mapping[str, Any]) -> str:
    return (
        f"A person ({revision.get('by', '?')}) rejected your last output at gate "
        f"{revision.get('gate', '?')!r} with this note: {revision.get('note', '')} "
        "Revise your outputs so they answer the note, and keep what the note does not ask "
        "to change."
    )


def _latest_decision(ctx: AppContext, gate_id: str, instance: RuleInstance) -> Approval | None:
    """The latest current decision file for `gate_id` over `instance`'s file inputs (the
    rule `GateEvaluator` applies; waivers aside)."""
    refs = [ref for ref in instance.inputs if ref.path is not None]
    current = ctx.store.current_hashes(refs)
    decisions = sorted(
        (
            a
            for a in ctx.review.approvals(gate_id)
            if a.decision != "waive"
            and a.is_current(current)
            and set(current) <= set(a.artifact_hashes)
        ),
        key=lambda a: a.at,
    )
    return decisions[-1] if decisions else None


def _revise_rejected(ctx: AppContext, scheduler: Scheduler, target: str) -> list[dict[str, Any]]:
    """Hand an accepted agent task out again when a person rejected its output, with a note.

    For each gated instance of `target` whose gate is `rejected` by a decision file (a
    person's `approve(reject=true, note=...)` or `chipgraph approve --reject --note`), when
    every file the gate covers is the output of an accepted agent task that was accepted
    before the rejection: each such task is reopened as a new `ready` task (a new budget
    cycle) and rewound in the build, and the note is kept for its context
    (`previous_rejection`) until it is accepted again. A rejection with no note, of a gate
    over any file no agent writes, or one the task already answered (accepted after it)
    changes nothing: the gate stays rejected for a person to decide.
    """
    graph = scheduler.graph
    gates = scheduler.gates
    if gates is None:
        return []
    queue = _queue(ctx)
    producers: dict[str, str] = {}
    for iid, inst in graph.instances.items():
        if graph.rules[inst.rule_id].kind == "agent":
            for ref in inst.outputs:
                if ref.path is not None:
                    producers[ref.path] = iid
    revised: list[dict[str, Any]] = []
    for iid in sorted(graph.select(target)):
        instance = graph.instances[iid]
        rule = graph.rules[instance.rule_id]
        if not rule.gate:
            continue
        gate_id = gate_id_for(rule.gate, instance.params)
        if gates.status(gate_id, instance) != "rejected":
            continue
        decision = _latest_decision(ctx, gate_id, instance)
        if decision is None or decision.decision != "reject" or not decision.note.strip():
            continue
        covered = [ref.path for ref in instance.inputs if ref.path is not None]
        owners = {producers.get(path) for path in covered}
        if not covered or None in owners:
            continue
        for task_id in sorted(o for o in owners if o is not None):
            record = queue.get(task_id)
            if record is None or record.status != "accepted" or record.updated_at >= decision.at:
                continue
            task_rule, task_instance = _instance(graph, task_id)
            # `enqueue` reopens an accepted task whose outputs no longer hash as accepted:
            # the person's rejection says they no longer hold.
            queue.enqueue(
                task_for(task_rule, task_instance),
                inputs_hash=instance_inputs_hash(ctx.store, task_instance),
                output_hashes={"<rejected>": decision.at.isoformat()},
                tool_call_cap=record.tool_call_cap,
                max_dispatches=record.max_dispatches,
            )
            scheduler.rewind(task_id)
            item = {
                "task_id": task_id,
                "gate": gate_id,
                "by": decision.by,
                "at": decision.at.isoformat(),
                "note": decision.note,
            }
            path = _revision_path(ctx, task_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"schema_version": 1, **item}, indent=2), encoding="utf-8")
            revised.append(item)
    return revised


# --- Best-of-N dispatch helpers ---------------------------------------------------------


def _dispatch_candidates(
    ctx: AppContext,
    queue: TaskQueue,
    graph: BuildGraph,
    base_record: AgentTaskRecord,
    rule: RuleSpec,
    instance: RuleInstance,
    run_id: str,
    baseline: dict[str, str],
    n: int,
) -> tuple[BestOfNRound, list[tuple[AgentTaskRecord, int, str]]]:
    """Dispatch N candidates for a best-of-N round.

    Returns the round state and list of (record, candidate_num, variant) tuples.
    """
    profile = ctx.require_profile().profile
    vcs = ctx.registry.get("vcs", "git")
    inputs = input_paths_for(instance)

    # Plan the budget
    budget_plan = plan_budget(
        rule.budget,
        requested=n,
        cfg=profile.select,
        max_cost=None,  # No cost cap in this runtime
        variants=len(VARIANTS),
    )
    actual_n = budget_plan.n

    # Compute round number: base record's attempts counts failed rounds, +1 for current
    # A failed round increments attempts by 1, so round_num = attempts + 1
    round_num = base_record.attempts + 1

    # Get variant hints
    variant_ids = list(VARIANTS.keys())[:actual_n]
    variant_hints = [VARIANTS[vid] for vid in variant_ids]

    # Create candidates
    candidates: list[CandidateState] = []
    dispatched: list[tuple[AgentTaskRecord, int, str]] = []

    for i, (_vid, variant) in enumerate(zip(variant_ids, variant_hints, strict=False), 1):
        cand_task_id = candidate_id(base_record.task_id, i, round_num)

        # Create workspace for this candidate
        try:
            ws, seed_hashes = create_workspace(
                ctx, vcs, run_id, cand_task_id, inputs, tuple(base_record.allowed_writes)
            )
        except Exception as exc:
            # If workspace creation fails, clean up and raise
            for _, cand_num, _ in dispatched:
                cand_id = candidate_id(base_record.task_id, cand_num, round_num)
                cand_rec = queue.get(cand_id)
                if cand_rec and cand_rec.workspace:
                    with contextlib.suppress(Exception):
                        remove_workspace(ctx, vcs, cand_rec.workspace)
            raise AppError(f"failed to create workspace for candidate {i}: {exc}") from exc

        # Dispatch the candidate
        try:
            cand_record = queue.dispatch(
                cand_task_id,
                run_id=run_id,
                baseline=baseline,
                denied_reads=_denied_reads(ctx, graph, base_record),
                workspace=str(ws),
                seed_hashes=seed_hashes,
            )
        except QueueError:
            # The candidate is a record of the same instance under its own id
            from chipgraph.core.plugin_api.types import AgentTask

            cand_task = AgentTask(
                instance=instance,
                role=base_record.role,
                skills=base_record.skills,
                allowed_writes=base_record.allowed_writes,
                budget=budget_plan.slice,
            )
            queue.enqueue(
                cand_task,
                inputs_hash=base_record.inputs_hash,
                output_hashes={},
                tool_call_cap=base_record.tool_call_cap,
                max_dispatches=loop.max_dispatches(budget_plan.slice.tries),
                task_id=cand_task_id,
            )
            cand_record = queue.dispatch(
                cand_task_id,
                run_id=run_id,
                baseline=baseline,
                denied_reads=_denied_reads(ctx, graph, base_record),
                workspace=str(ws),
                seed_hashes=seed_hashes,
            )

        candidates.append(
            CandidateState(
                candidate=i,
                variant=variant,
                task_id=cand_task_id,
                workspace=str(ws),
                status="dispatched",
            )
        )
        dispatched.append((cand_record, i, variant))

    round_state = BestOfNRound(
        base_task_id=base_record.task_id,
        run_id=run_id,
        round_num=round_num,
        n=actual_n,
        candidates=tuple(candidates),
        ledger_tries=budget_plan.limits.tries,
        ledger_used=0,
        status="running",
    )

    return round_state, dispatched


# --- next_task --------------------------------------------------------------------------


async def next_task(ctx: AppContext, target: str = "*") -> dict[str, Any]:
    """Run the build for `target` and hand out every agent task it reached.

    Returns `tasks` (newly dispatched: each goes to its own subagent, in parallel),
    `in_progress` (dispatched earlier and not submitted yet), `blocked` (gates, people,
    failures, stopped tasks with their label and HANDOFF path, and the rules that
    depend on them), and either `done: true` (the build finished) or, with nothing to
    run, `stopped: true`, `waiting` (why) and `handoff` (this run's HANDOFF.md).

    A task is never handed out more than its `max_dispatches` (tries plus infra
    retries) in one budget cycle, and never once it is `budget_exhausted` or
    `needs_human`.

    At the start, sweeps stale workspaces left by killed runs (a process that started
    workspaces and died without cleaning up).
    """
    ctx.require_profile()
    _require_runtime(ctx)

    # Sweep stale workspaces from killed runs
    try:
        vcs = ctx.registry.get("vcs", "git")
        sweep_workspaces(ctx, vcs)
    except Exception:
        pass  # Best effort; don't block the build

    scheduler = make_scheduler(ctx, target)
    _check_skills(ctx, scheduler.graph)
    revised = _revise_rejected(ctx, scheduler, target)
    summary = await scheduler.run(target)
    graph = scheduler.graph
    queue = _queue(ctx)
    for item in revised:
        _emit(ctx, summary.run_id, item["task_id"], "agent_turn", {"phase": "revise", **item})

    agent_ids = [
        iid
        for iid in sorted(graph.select(target))
        if graph.rules[graph.instances[iid].rule_id].kind == "agent"
    ]
    now: dict[str, str] | None = None
    dispatched: list[AgentTaskRecord] = []
    in_progress: list[AgentTaskRecord] = []
    held: list[dict[str, Any]] = []
    for iid in agent_ids:
        record = queue.get(iid)
        if record is None:
            continue
        if record.status in ACTIVE_STATUSES:
            in_progress.append(record)
            continue
        # Only tasks this build actually reached (their executor ran and said "waiting").
        if record.status not in OPEN_STATUSES or iid not in summary.failed:
            continue
        limit = loop.dispatch_limit(record)
        if record.state.dispatches >= limit:  # the hard bound; a live loop stops before
            held.append(
                {
                    "instance": iid,
                    "reason": (
                        f"handed out {record.state.dispatches} times, its limit (tries plus "
                        "infra retries); a person must look at it"
                    ),
                    "label": record.state.label,
                    "status": record.status,
                }
            )
            continue
        if now is None:
            now = await snapshot(ctx)
        if record.outside:
            before = queue.baseline(iid)
            left = [p for p in record.outside if before.get(p) != now.get(p)]
            if left:
                held.append(
                    {
                        "instance": iid,
                        "reason": (
                            "the last attempt changed files outside the task's outputs; "
                            f"revert them before it runs again: {', '.join(left)}"
                        ),
                    }
                )
                continue
        # A role without a read tool cannot overwrite a file: move its outputs aside
        # first, so each `Write` creates one (`previous`); put them back if the
        # dispatch fails here.
        mover = PreviousOutputs(queue, iid, ctx.root)
        token: MoveToken | None = None
        if moves_outputs(find_role(record.role)):
            token = mover.move_aside(record.allowed_writes, dispatch=record.dispatches + 1)
        moved = [m.path for m in token.moved] if token is not None else []
        for path in moved:
            now.pop(path, None)

        # Isolation: create workspace if fanout.isolate is true
        workspace_path: str | None = None
        seed_hashes: dict[str, str] = {}
        profile = ctx.require_profile().profile
        rule, instance = _instance(graph, iid)
        # A reviewing role (the Critic) writes nothing: the engine records its report in the
        # user's tree. There is nothing to isolate, and it reads the project as it is.
        isolated = should_isolate(profile, rule) and not review_mod.is_review_role(
            find_role(record.role)
        )

        # F2: check overlapping write sets with tasks being dispatched this cycle
        # (only for isolated tasks - non-isolated tasks write to user's tree directly)
        if isolated:
            # Check max_parallel limit (only for isolated tasks with workspaces)
            max_par = max_parallel_limit(profile)
            live_workspaces = len([r for r in (*dispatched, *in_progress) if r.workspace])
            if live_workspaces >= max_par:
                if token is not None:
                    mover.undo(token)
                held.append(
                    {
                        "instance": iid,
                        "reason": (
                            f"max_parallel limit reached ({max_par} workspaces); "
                            "waiting for a workspace to finish"
                        ),
                    }
                )
                continue

            # Check for overlapping write sets with tasks already being dispatched
            dispatching_writes = {r.task_id: r.allowed_writes for r in dispatched if r.workspace}
            dispatching_writes[iid] = record.allowed_writes
            overlaps = check_overlapping_writes(dispatching_writes)
            if overlaps and any(iid in tasks for tasks in overlaps.values()):
                if token is not None:
                    mover.undo(token)
                # Find which paths overlap
                overlap_info = [
                    f"{path}: {', '.join(tasks)}"
                    for path, tasks in overlaps.items()
                    if iid in tasks
                ]
                held.append(
                    {
                        "instance": iid,
                        "reason": (
                            f"write set overlaps with another task being dispatched: "
                            f"{'; '.join(overlap_info)}"
                        ),
                    }
                )
                continue

        if isolated:
            try:
                vcs = ctx.registry.get("vcs", "git")
                inputs = input_paths_for(instance)

                # Check for best-of-N opt-in
                requested_n = best_of_n(profile, rule.id, dict(instance.params))
                if requested_n is not None and requested_n >= 2:
                    # Best-of-N: dispatch multiple candidates
                    try:
                        round_state, cand_dispatched = _dispatch_candidates(
                            ctx,
                            queue,
                            graph,
                            record,
                            rule,
                            instance,
                            summary.run_id,
                            now,
                            requested_n,
                        )
                        _save_best_of_round(ctx, round_state)

                        # Add all candidates to dispatched and emit events
                        for cand_record, cand_num, variant in cand_dispatched:
                            payload = {
                                "phase": "dispatch",
                                "attempt": cand_record.dispatches,
                                "try": cand_record.state.tries_used + 1,
                                "tier": cand_record.current_tier,
                                "outputs": list(cand_record.allowed_writes),
                                "agent": _agent_for(cand_record.role),
                                "model": _model_for(ctx, cand_record),
                                "denied_reads": list(cand_record.denied_reads),
                                "workspace": cand_record.workspace,
                                "best_of": {
                                    "base_task": iid,
                                    "candidate": cand_num,
                                    "n": round_state.n,
                                    "round": round_state.round_num,
                                    "variant": variant,
                                },
                            }
                            _emit(
                                ctx, cand_record.run_id, cand_record.task_id, "agent_turn", payload
                            )
                            dispatched.append(cand_record)
                        continue

                    except AppError as exc:
                        if token is not None:
                            mover.undo(token)
                        held.append(
                            {"instance": iid, "reason": f"best-of-N dispatch failed: {exc}"}
                        )
                        continue

                # Regular isolated dispatch (best-of-1 or not opted in)
                ws, seed_hashes = create_workspace(
                    ctx, vcs, summary.run_id, iid, inputs, tuple(record.allowed_writes)
                )
                workspace_path = str(ws)
            except Exception as exc:
                if token is not None:
                    mover.undo(token)
                held.append(
                    {
                        "instance": iid,
                        "reason": f"failed to create isolated workspace: {exc}",
                    }
                )
                continue

        try:
            record = queue.dispatch(
                iid,
                run_id=summary.run_id,
                baseline=now,
                denied_reads=_denied_reads(ctx, graph, record),
                workspace=workspace_path,
                seed_hashes=seed_hashes,
            )
        except BaseException:
            if token is not None:
                mover.undo(token)
            if workspace_path:
                try:
                    vcs = ctx.registry.get("vcs", "git")
                    remove_workspace(ctx, vcs, workspace_path)
                except Exception:
                    pass
            raise
        payload = {
            "phase": "dispatch",
            "attempt": record.dispatches,
            "try": record.state.tries_used + 1,
            "tier": record.current_tier,
            "outputs": list(record.allowed_writes),
            "agent": _agent_for(record.role),
            "model": _model_for(ctx, record),
            "denied_reads": list(record.denied_reads),
        }
        if moved:
            payload["moved_aside"] = moved
        if workspace_path:
            payload["workspace"] = workspace_path
        _emit(ctx, record.run_id, iid, "agent_turn", payload)
        dispatched.append(record)

    skip = {r.task_id for r in (*dispatched, *in_progress)} | {h["instance"] for h in held}
    blocked = held + _blocked(ctx, graph, queue, summary, skip)

    # Build task entries, including candidate info for best-of-N tasks
    def _build_task_entry(r: AgentTaskRecord) -> dict[str, Any]:
        if is_candidate_id(r.task_id):
            base_task_id, cand_num, _round_num = parse_candidate_id(r.task_id)
            round_state = _load_best_of_round(ctx, base_task_id)
            if round_state:
                cand_state = next(
                    (c for c in round_state.candidates if c.candidate == cand_num), None
                )
                if cand_state:
                    return _task_entry(
                        ctx, queue, r, candidate=cand_num, variant=cand_state.variant
                    )
        return _task_entry(ctx, queue, r)

    answer: dict[str, Any] = {
        "run_id": summary.run_id,
        "tasks": [_build_task_entry(r) for r in dispatched],
        "in_progress": [_build_task_entry(r) for r in in_progress],
        "done": False,
        "stopped": False,
        "waiting": None,
        "blocked": blocked,
        "handoff": None,
    }
    if revised:
        answer["revised"] = revised
    if dispatched or in_progress:
        return answer
    if summary.ok and not blocked:
        answer["done"] = True
        return answer
    answer["stopped"] = True
    answer["handoff"] = str(_handoff_path(ctx, summary.run_id))
    answer["waiting"] = "; ".join(f"{b['instance']}: {b['reason']}" for b in blocked) or (
        f"{len(summary.blocked)} rule instance(s) blocked"
    )
    return answer


# --- get_context ------------------------------------------------------------------------


def _is_nda(ctx: AppContext, path: str, label: str) -> bool:
    return label == "nda" or ctx.store.labels.label_for(path) == "nda"


def _model_input(ctx: AppContext, key: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"model_key": key}
    kind, _, name = key.partition("/")
    db = default_model_db_path(ctx.layout.root)
    if kind != "block" or not name or not db.is_file():
        entry["note"] = "not available from the Design Model (run `chipgraph ingest` first)"
        return entry
    store = ModelStore(db)
    try:
        info = ModelQuery(store.read(), store).block(name)
    except QueryError as exc:
        entry["note"] = str(exc)
        return entry
    entry["content"] = json.dumps(info.model_dump(mode="json"), indent=1, sort_keys=True)
    return entry


def _refuse(
    ctx: AppContext,
    queue: TaskQueue,
    record: AgentTaskRecord,
    question: str,
    payload: Mapping[str, Any],
) -> AppError:
    """Hand the task to a person with `question`; the error for the tool call."""
    stopped = queue.needs_human(
        record.task_id, result=AgentResult(status="needs_human", open_questions=(question,))
    )
    restored = _settle(ctx, queue, stopped)
    extra = {"restored": restored} if restored else {}
    _emit(
        ctx,
        record.run_id,
        record.task_id,
        "agent_turn",
        {"phase": "context_refused", **payload, **extra},
    )
    return AppError(question)


def _settle(ctx: AppContext, queue: TaskQueue, record: AgentTaskRecord) -> list[str]:
    """Close the task's moved-outputs cycle once it ended (`previous`), and clean up workspace.

    Accepted: the new files stay, workspace is removed (outputs were copied back already).
    `budget_exhausted` or `needs_human`: every file that existed before the task is put
    back, workspace is removed (nothing copied back). Returns the paths put back.

    Every ending (accept, reject-and-redispatch, budget_exhausted, needs_human) removes
    the workspace. A rejected task that will be re-dispatched gets a fresh workspace.
    """
    mover = PreviousOutputs(queue, record.task_id, ctx.root)
    restored: list[str] = []
    if record.status == "accepted":
        mover.accept()
    elif record.status in ("budget_exhausted", "needs_human"):
        restored = mover.restore(dispatch=record.dispatches)

    # Clean up workspace for any terminal status (accepted, budget_exhausted, needs_human)
    # For rejected tasks, the workspace is removed too; next_task creates a fresh one
    terminal_statuses = ("accepted", "budget_exhausted", "needs_human", "rejected")
    if record.workspace and record.status in terminal_statuses:
        try:
            vcs = ctx.registry.get("vcs", "git")
            remove_workspace(ctx, vcs, record.workspace)
        except Exception:
            pass  # Best effort cleanup; the sweep will get it later

    return restored


def _unseeable(role: RoleSpec | None, record: AgentTaskRecord, instance: RuleInstance) -> list[str]:
    """Inputs the task's role must not see: of a denied kind (by kind or by name), or on a
    denied path."""
    if role is None or role.read_policy.mode != "deny":
        return []
    denied = record.denied_reads
    return sorted(
        ref.path or str(ref.model_key)
        for ref in instance.inputs
        if denied_kind(role, ref.kind, ref.path) is not None
        or (ref.path is not None and path_denied(ref.path, denied))
    )


def _denies(role: RoleSpec | None, record: AgentTaskRecord) -> Callable[[str], bool]:
    """Whether the task's role must not read a repo-relative path: one of its
    `denied_reads`, or a file its read policy denies by kind or by name."""

    def denied(path: str) -> bool:
        if path_denied(path, record.denied_reads):
            return True
        return role is not None and denied_kind(role, kind_for(path), path) is not None

    return denied


def _instructions(role: RoleSpec | None, rule: RuleSpec, skills: tuple[SkillSpec, ...]) -> str:
    if review_mod.is_review_role(role):
        follow = " Follow the instructions in `skill_texts`." if skills else ""
        return (
            "Review the change in `review.diff` against `review.spec` and `review.model`. "
            "You write no file and have no shell: reply with one JSON object that follows "
            "`review.reply_schema`, with `target`, `base` and `head` as in `review`. Every "
            "comment is on a file:line inside the diff and quotes its evidence as "
            f"'path:line text'; no comments means no findings.{follow} On submit the engine "
            "checks the review against this diff and writes it to `outputs[0]`; an invalid "
            "review is rejected and uses a try."
        )
    reading = (
        "You may read other project files for style."
        if role is None or role.can_read_files
        else "You have no file-reading tools, on purpose: use only this context."
    )
    follow = " Follow the instructions in `skill_texts`." if skills else ""
    return (
        "Write only the files in `outputs` (absolute paths in `outputs_abs`); the "
        f"chipgraph guard refuses any other write, and you have no shell. {reading}{follow} "
        "On submit the engine checks that only the outputs changed, that they exist, and "
        f"runs these checks: {', '.join(rule.checks) or 'none'}. "
        "Finish with a short report: files written, assumptions, open questions."
    )


def _guarded(
    ctx: AppContext,
    role: RoleSpec | None,
    graph: BuildGraph,
    instance: RuleInstance,
    record: AgentTaskRecord,
) -> GuardedContext | RtlAuthorContext | None:
    """The guarded context of a task whose role must not see some kinds, else None: the
    testbench Author's (no RTL), or the RTL Author's (no testbench, M2-04)."""
    return guarded_context(ctx, role, instance, list(graph.rules.values()), _denies(role, record))


def _feedback_filter(ctx: AppContext, record: AgentTaskRecord) -> FeedbackFilter | None:
    """What `submit` may show the task's role of a failed check (None: everything)."""
    return FeedbackFilter.for_role(
        record.role, denied=record.denied_reads, root=str(ctx.root.resolve())
    )


def _previous_rejection(ctx: AppContext, record: AgentTaskRecord) -> list[str]:
    """Why the last attempt was rejected: a person's note from a rejected gate (until the
    task is accepted again), then the redo instruction (it names the label and each
    failure as `file:line`), or the bare reasons on a record from before it."""
    revision = _revision(ctx, record.task_id)
    head = [_revision_text(revision)] if revision is not None else []
    state = record.budget_state
    if state is not None and state.redo:
        return [*head, state.redo]
    return [*head, *record.reasons]


async def get_context(ctx: AppContext, task_id: str) -> dict[str, Any]:
    """The context of one dispatched task: inputs as text, outputs, role, skills.

    Refused, with no content, when any input or output is labelled `nda`: every model
    this runtime uses is a cloud model. Refused too when an input is of a kind the task's
    role must not see (its `read_policy`), or on one of its `denied_reads`. Either way the
    task then waits for a person (`needs_human`).
    """
    ctx.require_profile()
    _require_runtime(ctx)
    queue = _queue(ctx)
    record = _record(queue, task_id)
    if record.status != "dispatched":
        raise AppError(f"task {task_id!r} is {record.status}, not dispatched; call next_task")
    graph = _graph(ctx)
    rule, instance = _instance(graph, task_id)

    nda = sorted(
        ref.path
        for ref in (*instance.inputs, *instance.outputs)
        if ref.path is not None and _is_nda(ctx, ref.path, ref.label)
    )
    if nda:
        question = (
            f"task {task_id!r} reads or writes data labelled 'nda' ({', '.join(nda)}); "
            "runtime claude-code sends context to a cloud model, so it is refused. Run the "
            "task with a local runtime, or have a person do it."
        )
        raise _refuse(ctx, queue, record, question, {"nda": nda})

    role = find_role(record.role)
    unseeable = _unseeable(role, record, instance)
    if role is not None and unseeable:
        kinds = ", ".join(role.read_policy.deny_kinds) or "denied"
        question = (
            f"task {task_id!r} gives role {role.id!r} inputs it must not see "
            f"({kinds} artifacts or paths): {', '.join(unseeable)}; it is refused. Remove "
            "those inputs from the rule, or give the task to another role or a person."
        )
        raise _refuse(ctx, queue, record, question, {"denied_inputs": unseeable})
    try:
        skills = _skill_set(ctx).resolve(record.skills, record.role)
    except SkillError as exc:
        raise AppError(f"task {task_id!r}: {exc}") from exc

    guarded = _guarded(ctx, role, graph, instance, record)
    extra_specs: list[str] = []
    if guarded is not None:
        listed = {ref.path for ref in instance.inputs}
        extra_specs = [
            p
            for p in guarded.spec_documents()
            if p not in listed and not path_denied(p, record.denied_reads)
        ]
        nda_specs = [p for p in extra_specs if _is_nda(ctx, p, "internal")]
        if nda_specs:
            question = (
                f"task {task_id!r} would read the spec labelled 'nda' "
                f"({', '.join(nda_specs)}); runtime claude-code sends context to a cloud "
                "model, so it is refused. Run the task with a local runtime, or have a "
                "person do it."
            )
            raise _refuse(ctx, queue, record, question, {"nda": nda_specs})
    inputs: list[dict[str, Any]] = []
    for ref in instance.inputs:
        if ref.path is None:
            key = str(ref.model_key)
            inputs.append(guarded.model_input(key) if guarded else _model_input(ctx, key))
            continue
        entry: dict[str, Any] = {"path": ref.path, "kind": ref.kind}
        fragment = graph.spec_fragments.get(f"{ref.repo}:{ref.path}")
        if fragment:
            entry["fragment"] = fragment
        section = guarded.covers(ref.path) if guarded is not None else None
        if section is not None:  # shown in a section of the context already
            entry["section"] = section
        elif ctx.store.exists(ref):
            text = (ctx.root / ref.path).read_text(encoding="utf-8", errors="replace")
            if len(text) > _MAX_INPUT_CHARS:
                text = text[:_MAX_INPUT_CHARS]
                entry["truncated"] = True
            entry["content"] = text
        else:
            entry["missing"] = True
        inputs.append(entry)
    for rel in extra_specs:  # a guarded task's spec documents, from the profile layout
        text = (ctx.root / rel).read_text(encoding="utf-8", errors="replace")
        entry = {"path": rel, "kind": "spec", "from": "layout"}
        if len(text) > _MAX_INPUT_CHARS:
            text = text[:_MAX_INPUT_CHARS]
            entry["truncated"] = True
        entry["content"] = text
        inputs.append(entry)

    review: dict[str, Any] | None = None
    if review_mod.is_review_role(role):
        scope, review = await review_mod.build_review(ctx, graph, rule, instance)
        nda_diff = sorted(p for p in scope.files if _is_nda(ctx, p, "public"))
        if nda_diff:
            question = (
                f"task {task_id!r} reviews files labelled 'nda' ({', '.join(nda_diff)}); "
                "runtime claude-code sends context to a cloud model, so it is refused. Run "
                "the review with a local runtime, or have a person do it."
            )
            raise _refuse(ctx, queue, record, question, {"nda": nda_diff})
        review_mod.save_scope(ctx, task_id, record.dispatches, scope)

    _emit(
        ctx,
        record.run_id,
        task_id,
        "agent_turn",
        {"phase": "context", "attempt": record.dispatches},
    )
    root = ctx.root.resolve()
    # Determine the effective root for this task: workspace if isolated, else project root
    workspace = record.workspace
    if workspace:
        effective_root = Path(workspace)
        workspace_note = (
            " This task runs in an isolated workspace. The workspace IS the project for "
            "this task: write your outputs to the absolute paths in `outputs_abs`. The "
            "workspace is a copy of the project at HEAD with the task's inputs seeded in."
        )
    else:
        effective_root = root
        workspace_note = ""

    answer: dict[str, Any] = {
        "task_id": task_id,
        "rule": rule.id,
        "description": rule.description,
        "role": record.role,
        "skills": list(record.skills),
        "skill_texts": {skill.id: skill.text for skill in skills},
        "params": dict(instance.params),
        "project_root": str(effective_root),
        "outputs": list(record.allowed_writes),
        "outputs_abs": [str(effective_root / p) for p in record.allowed_writes],
        "checks": list(rule.checks),
        "attempt": record.dispatches,
        "tries": record.tries,
        "previous_rejection": _previous_rejection(ctx, record),
        "previous_label": record.state.label,
        "inputs": inputs,
        "instructions": _instructions(role, rule, skills) + workspace_note,
    }
    if workspace:
        answer["workspace"] = workspace
        answer["isolated"] = True
    if review is not None:
        answer["review"] = review
    if guarded is not None:
        answer.update(guarded.sections())
        answer["instructions"] += guarded.instructions
    if moves_outputs(role):
        shown, withheld = PreviousOutputs(queue, task_id, ctx.root).context(
            record.allowed_writes, _denies(role, record)
        )
        answer["previous_outputs"] = shown
        answer["instructions"] += _previous_instructions(shown, withheld)
    if record.role == "planner":
        answer["plan"] = _planner_context(ctx, graph, instance)
    return answer


def _previous_instructions(shown: list[dict[str, Any]], withheld: list[str]) -> str:
    """What `get_context` tells an author without a read tool about its outputs."""
    text = (
        " Every output that existed was moved aside when this task was handed out, so "
        "each output is a new file: write it whole with Write (Edit has no file to change)."
    )
    if any("content" in entry for entry in shown):
        text += (
            " Your previous attempt is in `previous_outputs`; write the whole corrected "
            "file with Write."
        )
    if any("existing_output" in entry for entry in shown):
        text += (
            " A file that existed before this task is in `previous_outputs` under "
            "`existing_output`; it is put back if the task is not accepted."
        )
    if withheld:
        text += (
            f" {len(withheld)} moved file(s) are of a kind your role must not see; "
            "`previous_outputs` lists them without their text."
        )
    return text


def _planner_context(ctx: AppContext, graph: BuildGraph, instance: RuleInstance) -> dict[str, Any]:
    """A planner task's extra context (M2-03): the block's model slice, the paths it may
    write, the limits, the naming rule and the plan schema (`digital-rtl` pack)."""
    from chipgraph.packs.digital_rtl.plan.context import planner_context

    block = instance.params.get("block")
    if block is None or not instance.outputs or instance.outputs[0].path is None:
        return {}
    return planner_context(
        ctx.root,
        ctx.require_profile(),
        list(graph.rules.values()),
        block,
        plan_path=instance.outputs[0].path,
    )


# --- submit -----------------------------------------------------------------------------


def _check_reason(result: CheckResult) -> str:
    msgs = "; ".join(i.msg for i in result.issues[:5]) or result.log_tail[-500:]
    return f"check {result.check_id} {result.status}: {msgs}".strip()


async def _take_review(
    ctx: AppContext,
    graph: BuildGraph,
    rule: RuleSpec,
    instance: RuleInstance,
    record: AgentTaskRecord,
    report: SubmitReport,
) -> tuple[ReviewReport | None, list[str]]:
    """Validate a review task's reply and write it as the task's output (the engine does).

    Returns the review, or `None` and the reasons it is refused (nothing is written).
    """
    if report.review is None:
        return None, [
            "review: a review task is submitted with `review`, the subagent's JSON review "
            "(see review.reply_schema in its context)"
        ]
    parsed, problems = parse_review(report.review)
    if parsed is not None:
        scope = review_mod.load_scope(ctx, record.task_id, record.dispatches)
        if scope is None:  # get_context was not called for this dispatch
            scope = await review_mod.review_scope(ctx, graph, rule, instance)
        problems = review_problems(parsed, scope)
    if parsed is None or problems:
        return None, [f"review: {p}" for p in problems]
    out = ctx.root / record.allowed_writes[0]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(parsed.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return parsed, []


async def _handle_candidate_submit(
    ctx: AppContext,
    queue: TaskQueue,
    record: AgentTaskRecord,
    round_state: BestOfNRound,
    cand_num: int,
    checks: list[CheckResult],
    verdict: loop.Verdict,
) -> Selection | None:
    """Handle a best-of-N candidate submission: update state and run selection if all done.

    A candidate's submit records its check results in the round state but does NOT:
    - Remove its workspace (needed for copy-back if it wins)
    - Accept/reject the base record (done after selection)
    - Settle the candidate record (done after selection)

    When all candidates are submitted, this function:
    1. Runs `select_best` (deterministic key; Critic tiebreak not wired yet)
    2. If there's a winner: copies winner's outputs to user's tree, accepts BASE record
    3. If no candidate passed: rejects BASE record as one counted try
    4. Marks all candidate records terminal
    5. Removes all candidate workspaces
    6. Removes the round state file

    Returns the Selection result if all candidates are done and selection was run,
    otherwise None.
    """
    # Check if this candidate was already submitted (double submit protection)
    existing_cand = next((c for c in round_state.candidates if c.candidate == cand_num), None)
    if existing_cand and existing_cand.status != "dispatched":
        raise AppError(
            f"candidate {cand_num} of {round_state.base_task_id!r} was already submitted "
            f"(status: {existing_cand.status})"
        )

    # Update the candidate state in the round
    updated_candidates = []
    for cand in round_state.candidates:
        if cand.candidate == cand_num:
            status: Literal["dispatched", "submitted", "done", "failed", "cancelled"]
            if verdict.accepted:
                status = "done"
            elif verdict.status in ("budget_exhausted", "needs_human"):
                status = "failed"
            else:
                status = "failed"

            updated_cand = CandidateState(
                candidate=cand.candidate,
                variant=cand.variant,
                task_id=cand.task_id,
                workspace=cand.workspace,
                status=status,
                checks=tuple(checks),
                passed=verdict.accepted,
                errors=error_count(checks),
                warnings=warning_count(checks),
                spend=Spend(tries=1),  # Each submit counts as one try
            )
            updated_candidates.append(updated_cand)
        else:
            updated_candidates.append(cand)

    updated_round = BestOfNRound(
        schema_version=round_state.schema_version,
        base_task_id=round_state.base_task_id,
        run_id=round_state.run_id,
        round_num=round_state.round_num,
        n=round_state.n,
        candidates=tuple(updated_candidates),
        ledger_tries=round_state.ledger_tries,
        ledger_used=round_state.ledger_used + 1,
        winner=round_state.winner,
        selection=round_state.selection,
        status=round_state.status,
    )
    _save_best_of_round(ctx, updated_round)

    # Check if all candidates are done
    all_done = all(c.status in ("done", "failed", "cancelled") for c in updated_candidates)
    if not all_done:
        return None

    # All candidates done - run selection
    updated_round = updated_round.model_copy(update={"status": "selecting"})
    _save_best_of_round(ctx, updated_round)

    # Build Candidate objects for select_best
    # Use the round state's candidate info (workspace paths) since those are the actual
    # paths before any cleanup - the queue records may have stale info
    candidates: list[Candidate] = []
    for cand in updated_candidates:
        workspace_path = Path(cand.workspace) if cand.workspace else None
        candidates.append(
            Candidate(
                id=str(cand.candidate),
                passed=cand.passed,
                checks=cand.checks,
                finished=cand.status != "cancelled",
                workspace=workspace_path if workspace_path and workspace_path.exists() else None,
            )
        )

    # Run selection (deterministic key only; Critic tiebreak is NOT wired in this
    # implementation - that requires the review rule infrastructure and will be wired
    # in a follow-up, say so in the report)
    selection = await select_best(candidates, metrics=None, tiebreak=None, ledger=None)

    # Emit select event
    _emit(
        ctx,
        updated_round.run_id,
        updated_round.base_task_id,
        "tool_call",
        {
            "tool": "select",
            "event": "select",
            "best_of": updated_round.round_num,
            "status": (
                "done" if selection.winner is not None and not selection.none_passed else "failed"
            ),
            "winner": selection.winner,
            "decided_by": selection.decided_by,
            "none_passed": selection.none_passed,
            "order": list(selection.order),
        },
    )

    # Get the base record to accept/reject
    base_record = queue.get(updated_round.base_task_id)
    if base_record is None:
        raise AppError(f"base task {updated_round.base_task_id!r} not found")

    # Get the winning candidate's info from the round state (not from queue - workspace
    # path is authoritative in the round state)
    winner_cand_state: CandidateState | None = None
    winner_cand_record: AgentTaskRecord | None = None
    copy_result: CopyBackResult | None = None

    if selection.winner is not None and not selection.none_passed:
        winner_num = int(selection.winner)
        winner_cand_state = next((c for c in updated_candidates if c.candidate == winner_num), None)
        if winner_cand_state:
            winner_cand_record = queue.get(winner_cand_state.task_id)

    # If we have a winner and it passed, copy back its outputs to user's tree
    if winner_cand_state and winner_cand_record and winner_cand_state.workspace:
        workspace_path = Path(winner_cand_state.workspace)
        if workspace_path.exists():
            copy_result = copy_outputs_back(
                workspace_path,
                ctx.root,
                winner_cand_record.allowed_writes,
                winner_cand_record.seed_hashes,
            )
            if not copy_result.ok:
                # Copy-back failed - treat as no winner (infra issue, not counted as try)
                selection = Selection(
                    winner=None,
                    decided_by="none",
                    none_passed=True,
                    order=selection.order,
                    keys=selection.keys,
                    critic_note=f"copy-back failed: {copy_result.reason}",
                )
                winner_cand_state = None
                winner_cand_record = None

    # Accept or reject the BASE record based on selection outcome
    graph = _graph(ctx)
    rule, instance = _instance(graph, updated_round.base_task_id)

    if selection.winner is not None and not selection.none_passed and winner_cand_record:
        # Winner found and copy-back succeeded - accept the BASE record
        # Get output hashes from the user's tree (where outputs were just copied)
        base_result = AgentResult(
            status="done",
            files_written=winner_cand_record.allowed_writes,
        )
        # Re-dispatch the base so we can accept it (it's in ready/rejected state)
        if base_record.status in OPEN_STATUSES:
            base_record = queue.dispatch(
                updated_round.base_task_id,
                run_id=updated_round.run_id,
                baseline={},  # Empty baseline - we're not checking changes
                denied_reads=(),
            )
        if base_record.status == "dispatched":
            base_record = queue.mark_submitted(updated_round.base_task_id)
        base_record = queue.accept(
            updated_round.base_task_id,
            result=base_result,
            output_hashes=output_hashes(ctx.store, task_for(rule, instance)),
        )
    else:
        # No winner or none passed - reject the BASE record as one counted try
        # Use the best candidate's redo text if available
        best_cand = updated_candidates[0] if updated_candidates else None
        redo_text = (
            f"Best-of-{updated_round.n} round {updated_round.round_num}: no candidate passed. "
        )
        if best_cand:
            best_cand_record = queue.get(best_cand.task_id)
            if best_cand_record and best_cand_record.state.redo:
                redo_text += f"Best candidate's issues: {best_cand_record.state.redo}"
            else:
                # Get the first few issues from the best candidate's checks
                issues = [i.msg for c in best_cand.checks if not c.ok for i in c.issues[:2]][:3]
                if issues:
                    redo_text += f"Issues: {'; '.join(issues)}"

        base_result = AgentResult(
            status="failed",
            files_written=(),
        )
        # Re-dispatch the base so we can reject it (it's in ready state)
        if base_record.status in OPEN_STATUSES:
            base_record = queue.dispatch(
                updated_round.base_task_id,
                run_id=updated_round.run_id,
                baseline={},
                denied_reads=(),
            )
        if base_record.status == "dispatched":
            base_record = queue.mark_submitted(updated_round.base_task_id)
        # Reject counts as one try for the base
        base_record = queue.reject(
            updated_round.base_task_id,
            result=base_result,
            reasons=(redo_text,),
            counted=True,
            exhausted=base_record.attempts + 1 >= base_record.tries,
            budget_state=TaskBudgetState(
                dispatches=base_record.state.dispatches,
                tries_used=base_record.state.tries_used + 1,
                failed_tries=base_record.state.failed_tries + 1,
                redo=redo_text,
                label="verification",
            ),
        )

    # Mark all candidate records as terminal (accepted for winner, rejected for losers)
    # so next_task never hands them out again
    for cand in updated_candidates:
        cand_record = queue.get(cand.task_id)
        if cand_record is None:
            continue
        is_winner = (
            selection.winner is not None
            and not selection.none_passed
            and cand.candidate == int(selection.winner)
        )
        if cand_record.status == "dispatched":
            cand_record = queue.mark_submitted(cand.task_id)
        if cand_record.status != "submitted":
            continue  # Already terminal
        if is_winner:
            # The winner is accepted (its outputs are what landed)
            cand_result = AgentResult(status="done", files_written=cand_record.allowed_writes)
            queue.accept(
                cand.task_id,
                result=cand_result,
                output_hashes={},  # Candidate's outputs are in workspace, not user tree
            )
        else:
            # Losers are rejected (budget_exhausted so they're never re-dispatched)
            cand_result = AgentResult(status="failed", files_written=())
            queue.reject(
                cand.task_id,
                result=cand_result,
                reasons=("not selected as winner in best-of-N round",),
                counted=False,  # Don't count against the candidate's own budget
                exhausted=True,  # Mark as terminal
            )

    # Clean up all candidate workspaces
    vcs = ctx.registry.get("vcs", "git")
    for cand in updated_candidates:
        if cand.workspace:
            with contextlib.suppress(Exception):
                remove_workspace(ctx, vcs, cand.workspace)

    # Update round state with selection result and mark as done
    final_round = BestOfNRound(
        schema_version=updated_round.schema_version,
        base_task_id=updated_round.base_task_id,
        run_id=updated_round.run_id,
        round_num=updated_round.round_num,
        n=updated_round.n,
        candidates=tuple(updated_candidates),
        ledger_tries=updated_round.ledger_tries,
        ledger_used=updated_round.ledger_used,
        winner=int(selection.winner) if selection.winner and not selection.none_passed else None,
        selection=selection,
        status="done" if selection.winner and not selection.none_passed else "failed",
    )
    _save_best_of_round(ctx, final_round)

    # If the round failed (no candidate passed), remove the round state so next_task
    # can start a fresh round
    if selection.none_passed or selection.winner is None:
        _remove_best_of_round(ctx, updated_round.base_task_id)

    return selection


async def submit(
    ctx: AppContext, task_id: str, report: SubmitReport | None = None
) -> dict[str, Any]:
    """Check a dispatched task's work at once and accept or reject it (DESIGN.md 5.2).

    The checks of the attempt, each run once: no file outside the task's outputs
    changed since dispatch, every output exists and is not empty, every input exists
    (`attempt_checks`), and, when those hold, the rule's checks. A failure gets its
    `label` (`loop.judge`, the M2-02a pieces) and counts against the budget:

    - `rejected`: tries are left; the answer has the failed checks, the redo text,
      `next_tier`/`next_model` (the tier escalates after the first failed try) and
      `tries_left`; `next_task` hands the task out again with the redo text in its
      context (`previous_rejection`);
    - `infra` (a check errored, a tool could not run): no try is used, but after
      `max_infra_retries` such submits the task stops;
    - `budget_exhausted`: the tries ran out, two tries in a row left the same outputs
      and failures (`stagnation`), or the infra retries ran out. The `rule_fail` event
      and the run's HANDOFF.md are written at once, and the task is never handed out
      again until an input changes or it is rewound;
    - `needs_human`: the checks point at the spec or plan (`planning`).

    A report with `status: needs_human` hands the task to a person without using a try.
    Tokens and cost are not known per task in this runtime: the loop's token and cost
    caps do not apply.

    A review task (its role writes no files and has `engine_writes`) is submitted with
    `report.review`: rejected when it is missing, not JSON, not a `ReviewReport`, or
    invalid for the diff its context showed (`review_problems`); otherwise the engine
    writes it as the task's one output and then runs the usual checks. A valid review
    is accepted whatever its verdict: AI findings never block the build (DESIGN.md 4.8).

    For best-of-N candidates: each candidate is submitted individually; after all are
    submitted, `select_best` is run (with Critic tiebreak for exact ties), and only the
    winner's outputs are copied back to the user's tree.
    """
    ctx.require_profile()
    _require_runtime(ctx)
    report = report or SubmitReport()
    queue = _queue(ctx)
    record = _record(queue, task_id)
    if record.status != "dispatched":
        raise AppError(f"task {task_id!r} is {record.status}, not dispatched; call next_task")
    graph = _graph(ctx)

    # Handle candidate task_id lookup - need to find the real instance
    if is_candidate_id(task_id):
        base_task_id, _cand_num, _round_num = parse_candidate_id(task_id)
        rule, instance = _instance(graph, base_task_id)
    else:
        rule, instance = _instance(graph, task_id)
    reviewing = review_mod.is_review_role(find_role(record.role))
    if report.review is not None and not reviewing:
        raise AppError(
            f"task {task_id!r} is not a review task (role {record.role!r}): submit it "
            "without `review`"
        )

    if report.status == "needs_human":
        if not report.open_questions:
            raise AppError("a submit with status 'needs_human' must list open_questions")
        result = AgentResult(
            status="needs_human",
            assumptions=report.assumptions,
            open_questions=report.open_questions,
        )
        record = queue.needs_human(task_id, result=result)
        restored = _settle(ctx, queue, record)
        _emit(
            ctx,
            record.run_id,
            task_id,
            "agent_turn",
            {
                "phase": "submit",
                "status": "needs_human",
                "try": record.state.tries_used + 1,
                "counted": False,
                "tier": record.current_tier,
                "label": "planning",
                "failed_checks": [],
                "open_questions": list(report.open_questions),
                "restored": restored,
            },
        )
        return _submit_answer(
            ctx, record, verdict=None, reasons=[], outside=[], missing=[], checks=[]
        )

    record = queue.mark_submitted(task_id)
    try:
        review: ReviewReport | None = None
        review_reasons: list[str] = []
        if reviewing:
            review, review_reasons = await _take_review(ctx, graph, rule, instance, record, report)
        allowed = set(record.allowed_writes)

        # For isolated tasks, diff the workspace; otherwise diff the user's tree
        workspace = record.workspace
        copy_back_result: CopyBackResult | None = None
        if workspace:
            workspace_path = Path(workspace)
            changed_in_allowed, changed_outside = diff_workspace(
                workspace_path, record.allowed_writes, record.seed_hashes
            )
            changed = [*changed_in_allowed, *changed_outside]
            outside = list(changed_outside)
            missing = (
                []
                if review_reasons
                else [p for p in record.allowed_writes if not (workspace_path / p).is_file()]
            )
        else:
            now = await snapshot(ctx, extra=record.allowed_writes)
            changed = _changed(queue.baseline(task_id), now)
            # Outputs of the other agent tasks: their own subagents write them (the guard
            # allows nobody else), whatever their status now: tasks that ran in parallel
            # are submitted one by one, and one may be accepted or rejected before this one.
            others = {
                w for other in queue.all() if other.task_id != task_id for w in other.allowed_writes
            }
            engine = _engine_outputs(graph)
            outside = [
                p for p in changed if p not in allowed and p not in others and p not in engine
            ]
            missing = (
                []
                if review_reasons
                else [p for p in record.allowed_writes if not (ctx.root / p).is_file()]
            )
        attempt = AgentResult(
            status="done",
            files_written=tuple(p for p in changed if p in allowed or p in outside),
            assumptions=report.assumptions,
            open_questions=report.open_questions,
        )
        # an isolated task is judged on its workspace: its outputs are there, not in the user's tree
        judged_store = (
            ArtifactStore(Path(workspace), ctx.store.labels, ctx.store.repo)
            if workspace
            else ctx.store
        )
        results = list(
            attempt_checks(instance, attempt, judged_store, allowed_writes=record.allowed_writes)
        )
        if reviewing:
            if review_reasons:  # nothing was written: the reply is what failed
                results = [r for r in results if r.check_id != OUTPUTS_CHECK]
            results.append(loop.review_result(review_reasons))

        checks: list[CheckResult] = []
        if not outside and not missing and not review_reasons:
            # For isolated tasks, run checks in the workspace; otherwise in user's tree
            check_root = Path(workspace) if workspace else None
            runner = ProfileCheckRunner(ctx, root_override=check_root)
            for check_id in rule.checks:
                checks.append(await runner.run(check_id, instance))
        results.extend(checks)

        shown = _feedback_filter(ctx, record)
        verdict = await loop.judge(
            rule,
            record,
            results,
            attempt,
            judged_store.current_hashes(instance.outputs),
            shown=shown,
        )
        # The journal keeps every check's full result; the answer and the redo text
        # show only what the role may see.
        visible = list(shown.apply(checks)[0]) if shown is not None else checks
        reasons: list[str] = list(review_reasons)
        if outside:
            reasons.append(f"changed files outside the task's outputs: {', '.join(outside)}")
        if missing:
            reasons.append(f"missing outputs: {', '.join(missing)}")
        reasons.extend(_check_reason(c) for c in visible if not c.ok)
        if not reasons and not verdict.accepted:
            reasons = list(verdict.state.failures)
        state = verdict.state
        if verdict.stop is not None and record.run_id is not None:
            state = state.model_copy(update={"handoff": str(_handoff_path(ctx, record.run_id))})
        result = AgentResult(
            status="done" if verdict.accepted else "failed",
            files_written=tuple(p for p in changed if p in allowed),
            assumptions=report.assumptions,
            open_questions=report.open_questions,
        )

        # Check if this is a best-of-N candidate
        is_best_of_candidate = is_candidate_id(task_id)
        best_of_round: BestOfNRound | None = None
        if is_best_of_candidate:
            base_task_id, cand_num, _round_num = parse_candidate_id(task_id)
            best_of_round = _load_best_of_round(ctx, base_task_id)
            # Validate: the round must exist and be running
            if best_of_round is None:
                raise AppError(
                    f"cannot submit candidate {task_id!r}: no active best-of-N round "
                    f"for base task {base_task_id!r}"
                )
            if best_of_round.status not in ("running", "selecting"):
                raise AppError(
                    f"cannot submit candidate {task_id!r}: best-of-N round is already "
                    f"{best_of_round.status}"
                )

        # For isolated tasks: on acceptance, copy outputs back to user's tree
        # For best-of-N candidates: DON'T copy back yet - wait for selection
        # Refuse if user's tree changed since the workspace was seeded
        if verdict.accepted and workspace and not is_best_of_candidate:
            workspace_path = Path(workspace)
            copy_back_result = copy_outputs_back(
                workspace_path, ctx.root, record.allowed_writes, record.seed_hashes
            )
            if not copy_back_result.ok:
                # User's tree changed - reject and re-dispatch (not counted as a try)
                reasons.append(f"copy-back refused: {copy_back_result.reason}")
                # Create a rejection verdict without counting the try (infra issue)
                verdict = loop.Verdict(
                    status="rejected",
                    label="infra",  # Concurrent edit is an infra issue
                    counted=False,
                    stop=None,
                    tier=verdict.tier,
                    state=verdict.state,
                    tries=verdict.tries,
                    failed=verdict.failed,
                    questions=(),
                )

        # For best-of-N candidates: DON'T accept/reject the candidate record here.
        # The candidate stays in "submitted" status; _handle_candidate_submit will
        # mark all candidates terminal after selection and accept/reject the BASE record.
        # Also DON'T remove the workspace - it's needed for copy-back if this candidate wins.
        if not is_best_of_candidate:
            if verdict.accepted:
                record = queue.accept(
                    task_id,
                    result=result,
                    output_hashes=output_hashes(ctx.store, task_for(rule, instance)),
                    budget_state=state,
                )
            elif verdict.status == "needs_human":
                questions = tuple(dict.fromkeys((*report.open_questions, *verdict.questions)))
                record = queue.needs_human(
                    task_id,
                    result=result.model_copy(
                        update={"status": "needs_human", "open_questions": questions}
                    ),
                    budget_state=state,
                )
            else:
                record = queue.reject(
                    task_id,
                    result=result,
                    reasons=tuple(reasons),
                    outside=tuple(outside),
                    counted=verdict.counted,
                    exhausted=verdict.status == "budget_exhausted",
                    budget_state=state,
                )
    except BaseException:
        with contextlib.suppress(QueueError):
            queue.unsubmit(task_id)
        raise

    # Best-of-N selection: update round state and run selection when all candidates done
    # For best-of-N candidates, _handle_candidate_submit handles everything:
    # - Recording the candidate's result in round state
    # - Running selection when all candidates are done
    # - Copying the winner's outputs to user's tree
    # - Accepting/rejecting the BASE record
    # - Marking all candidate records terminal
    # - Cleaning up all workspaces
    selection_result: Selection | None = None
    restored = []
    if is_best_of_candidate and best_of_round is not None:
        base_task_id, cand_num, _round_num = parse_candidate_id(task_id)
        selection_result = await _handle_candidate_submit(
            ctx, queue, record, best_of_round, cand_num, checks, verdict
        )
        # After _handle_candidate_submit, the candidate record is now terminal
        record = queue.require(task_id)
    else:
        # Non-candidate tasks: settle (move-aside restore + workspace cleanup)
        restored = _settle(ctx, queue, record)
        if record.status == "accepted":  # the person's note is answered; the gate decides again
            _revision_path(ctx, task_id).unlink(missing_ok=True)

    for check in checks:
        _emit(ctx, record.run_id, task_id, "check_result", check.model_dump(mode="json"))
    payload: dict[str, Any] = {
        "phase": "submit",
        "status": record.status,
        "attempt": record.dispatches,
        "try": verdict.state.tries_used if verdict.counted else verdict.state.tries_used + 1,
        "counted": verdict.counted,
        "tier": verdict.tier,
        "label": verdict.label,
        "failed_checks": [r.check_id for r in verdict.failed],
        "reasons": reasons,
        "files_written": list(result.files_written),
        "assumptions": list(report.assumptions),
        "open_questions": list(report.open_questions),
        "restored": restored,
    }
    if review is not None:
        payload["review"] = {"verdict": review.verdict, "comments": len(review.comments)}
    # Add best-of-N candidate info to payload
    if is_best_of_candidate:
        base_task_id, cand_num, _round_num = parse_candidate_id(task_id)
        payload["best_of"] = {
            "base_task": base_task_id,
            "candidate": cand_num,
        }
        if selection_result is not None:
            payload["best_of"]["selection"] = {
                "winner": selection_result.winner,
                "decided_by": selection_result.decided_by,
            }
    _emit(ctx, record.run_id, task_id, "agent_turn", payload)
    if verdict.stop is not None:
        _stop(ctx, record)

    answer = _submit_answer(
        ctx,
        record,
        verdict=verdict,
        reasons=reasons,
        outside=outside,
        missing=missing,
        checks=visible,
    )
    if is_best_of_candidate:
        # A candidate is not a task: it is not accepted or stopped on its own. Say what the
        # submit meant for the round, not what its private record says.
        answer["stop_reason"] = None
        answer["handoff"] = None
        if selection_result is None:
            answer["status"], answer["accepted"] = "submitted", False
            answer["next"] = (
                "this candidate is in; wait for the other candidate(s) of the round to be "
                "submitted, then call next_task"
            )
        elif selection_result.winner is not None:
            won = str(cand_num) == str(selection_result.winner)
            answer["status"], answer["accepted"] = ("won" if won else "lost"), won
            answer["next"] = "the round is decided and its winner landed; call next_task"
        else:
            answer["status"], answer["accepted"] = "round_failed", False
            answer["next"] = (
                "no candidate of the round passed, nothing landed; call next_task: a new round "
                "starts, or the task stops when its budget is spent"
            )
    # Add selection result to answer if this was the final candidate
    if selection_result is not None:
        answer["selection"] = {
            "winner": selection_result.winner,
            "decided_by": selection_result.decided_by,
            "none_passed": selection_result.none_passed,
            "order": list(selection_result.order),
        }
    return answer


def _stop(ctx: AppContext, record: AgentTaskRecord) -> None:
    """Journal a stopped task's `rule_fail` in its run, and write that run's HANDOFF.md."""
    if record.run_id is None:
        return
    info = loop.stop_info(record)
    payload: dict[str, Any] = {"message": loop.stop_message(info), "agent": info}
    if record.last_result is not None:
        payload["result"] = record.last_result.model_dump(mode="json")
        if record.last_result.open_questions:
            payload["open_questions"] = list(record.last_result.open_questions)
    _emit(
        ctx,
        record.run_id,
        record.task_id,
        "rule_fail",
        payload,
        failure_label=record.state.label,
    )
    _write_run_handoff(ctx, record.run_id)


def _submit_answer(
    ctx: AppContext,
    record: AgentTaskRecord,
    *,
    verdict: loop.Verdict | None,
    reasons: list[str],
    outside: list[str],
    missing: list[str],
    checks: list[CheckResult],
) -> dict[str, Any]:
    state = record.state
    if record.status == "rejected":
        hint = (
            "call next_task: it hands the task out again (model next_model); start its "
            "subagent with the task's prompt only, the redo text is in its context"
        )
    elif record.status == "budget_exhausted":
        hint = (
            "stop working on this task: it used its budget; a person must look at it (see handoff)"
        )
    elif record.status == "needs_human":
        hint = "a person must answer the open questions; then build again"
    else:
        hint = "call next_task for the next tasks"
    rejected = record.status == "rejected"
    # An agent's own needs_human report (no verdict): M2-02a labels it `planning`.
    label: FailureLabel | None = verdict.label if verdict is not None else "planning"
    return {
        "task_id": record.task_id,
        "accepted": record.status == "accepted",
        "status": record.status,
        "attempts": record.attempts,
        "tries": record.tries,
        "label": label,
        "counted": verdict.counted if verdict is not None else False,
        "failed_checks": loop.failed_checks(verdict.failed) if verdict is not None else [],
        "redo": state.redo if verdict is not None and not verdict.accepted else None,
        "next_tier": record.current_tier if rejected else None,
        "next_model": _model_for(ctx, record) if rejected else None,
        "tries_left": max(record.tries - state.tries_used, 0),
        "budget": {
            "used": state.tries_used,
            "allowed": record.tries,
            "infra_retries": state.infra_failures,
            "max_infra_retries": loop.MAX_INFRA_RETRIES,
            "dispatches": state.dispatches,
            "max_dispatches": loop.dispatch_limit(record),
        },
        "stop_reason": state.stop if verdict is not None else None,
        "handoff": state.handoff if verdict is not None and verdict.stop is not None else None,
        "reasons": reasons,
        "outside_outputs": outside,
        "missing": missing,
        "checks": [
            {"check_id": c.check_id, "status": c.status, "issues": [i.msg for i in c.issues[:10]]}
            for c in checks
        ],
        "result": record.last_result.model_dump(mode="json") if record.last_result else None,
        "next": hint,
    }


__all__ = ["SubmitReport", "get_context", "next_task", "snapshot", "submit"]
