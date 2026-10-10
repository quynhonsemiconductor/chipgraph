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

A role that must not see some artifact kinds (the testbench Author and `rtl`) gets a
guarded context (`guarded`): its model inputs are the block's spec side and the module's
interface (spec ports, else the model's RTL ports, else the RTL declaration only), never
a general model slice; and what `submit` tells it of failed checks goes through
`FeedbackFilter` (no issue in a denied file, no denied path, no source excerpt; the
redo text says when something was withheld). The journal keeps the full results.

A role that writes no files but has `engine_writes` (the Critic) reviews a change
instead (`review`): its context holds the diff, the spec slice and the model slice; its
reply is a JSON review the main session passes as `result.review`; `submit` validates it
and the engine itself writes it as the task's one output.

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
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from chipgraph.adapters.runtime.claude_code import loop
from chipgraph.adapters.runtime.claude_code import review as review_mod
from chipgraph.adapters.runtime.claude_code.agents import DEFAULT_TIER_MODELS, agent_type
from chipgraph.adapters.runtime.claude_code.guarded import GuardedContext
from chipgraph.adapters.runtime.claude_code.runtime import output_hashes
from chipgraph.app.build import make_scheduler, pack_search_paths
from chipgraph.app.checks import ProfileCheckRunner
from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.core.contracts import AgentResult, CheckResult, Event, RuleInstance, RuleSpec
from chipgraph.core.contracts.event import EventType
from chipgraph.core.contracts.types import FailureLabel, ModelTier
from chipgraph.core.engine.agent_rule import OUTPUTS_CHECK, FeedbackFilter, attempt_checks
from chipgraph.core.engine.graph import BuildGraph
from chipgraph.core.engine.scheduler import RunSummary
from chipgraph.core.model import ModelQuery, ModelStore, QueryError, default_model_db_path
from chipgraph.core.plugin_api.pack import discover_packs
from chipgraph.core.plugin_api.registry import PluginError
from chipgraph.core.runtime import (
    ACTIVE_STATUSES,
    OPEN_STATUSES,
    AgentTaskRecord,
    QueueError,
    TaskQueue,
    task_for,
)
from chipgraph.core.runtime.roles import (
    ReviewReport,
    RoleSpec,
    SkillError,
    SkillSet,
    SkillSpec,
    denied_reads,
    find_role,
    load_skills,
    parse_review,
    path_denied,
    review_problems,
)
from chipgraph.core.state import journal as journal_mod
from chipgraph.core.state.artifacts import hash_file
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
    role = find_role(record.role)
    if role is None:
        return ()
    profile = ctx.require_profile().profile
    refs = [ref for inst in graph.instances.values() for ref in (*inst.inputs, *inst.outputs)]
    return denied_reads(
        role,
        artifacts=refs,
        layout=profile.layout,
        block_layouts={name: block.layout for name, block in profile.blocks.items()},
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


def _task_entry(ctx: AppContext, queue: TaskQueue, record: AgentTaskRecord) -> dict[str, Any]:
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
    return {
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
    """
    ctx.require_profile()
    _require_runtime(ctx)
    scheduler = make_scheduler(ctx, target)
    _check_skills(ctx, scheduler.graph)
    summary = await scheduler.run(target)
    graph = scheduler.graph
    queue = _queue(ctx)

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
        record = queue.dispatch(
            iid,
            run_id=summary.run_id,
            baseline=now,
            denied_reads=_denied_reads(ctx, graph, record),
        )
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
        _emit(ctx, record.run_id, iid, "agent_turn", payload)
        dispatched.append(record)

    skip = {r.task_id for r in (*dispatched, *in_progress)} | {h["instance"] for h in held}
    blocked = held + _blocked(ctx, graph, queue, summary, skip)
    answer: dict[str, Any] = {
        "run_id": summary.run_id,
        "tasks": [_task_entry(ctx, queue, r) for r in dispatched],
        "in_progress": [_task_entry(ctx, queue, r) for r in in_progress],
        "done": False,
        "stopped": False,
        "waiting": None,
        "blocked": blocked,
        "handoff": None,
    }
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
    queue.needs_human(
        record.task_id, result=AgentResult(status="needs_human", open_questions=(question,))
    )
    _emit(ctx, record.run_id, record.task_id, "agent_turn", {"phase": "context_refused", **payload})
    return AppError(question)


def _unseeable(role: RoleSpec | None, record: AgentTaskRecord, instance: RuleInstance) -> list[str]:
    """Inputs the task's role must not see: of a denied kind, or on a denied path."""
    if role is None or role.read_policy.mode != "deny":
        return []
    kinds = set(role.read_policy.deny_kinds)
    denied = record.denied_reads
    return sorted(
        ref.path or str(ref.model_key)
        for ref in instance.inputs
        if ref.kind in kinds or (ref.path is not None and path_denied(ref.path, denied))
    )


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
    ctx: AppContext, role: RoleSpec | None, instance: RuleInstance
) -> GuardedContext | None:
    """The guarded context of a task whose role must not see some kinds, else None."""
    if role is None or role.read_policy.mode != "deny" or not role.read_policy.deny_kinds:
        return None
    return GuardedContext(ctx, instance)


def _feedback_filter(ctx: AppContext, record: AgentTaskRecord) -> FeedbackFilter | None:
    """What `submit` may show the task's role of a failed check (None: everything)."""
    return FeedbackFilter.for_role(
        record.role, denied=record.denied_reads, root=str(ctx.root.resolve())
    )


def _previous_rejection(record: AgentTaskRecord) -> list[str]:
    """Why the last attempt was rejected: the redo instruction (it names the label and
    each failure as `file:line`), or the bare reasons on a record from before it."""
    state = record.budget_state
    if state is not None and state.redo:
        return [state.redo]
    return list(record.reasons)


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

    guarded = _guarded(ctx, role, instance)
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
        if ctx.store.exists(ref):
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
    answer: dict[str, Any] = {
        "task_id": task_id,
        "rule": rule.id,
        "description": rule.description,
        "role": record.role,
        "skills": list(record.skills),
        "skill_texts": {skill.id: skill.text for skill in skills},
        "params": dict(instance.params),
        "project_root": str(root),
        "outputs": list(record.allowed_writes),
        "outputs_abs": [str(root / p) for p in record.allowed_writes],
        "checks": list(rule.checks),
        "attempt": record.dispatches,
        "tries": record.tries,
        "previous_rejection": _previous_rejection(record),
        "previous_label": record.state.label,
        "inputs": inputs,
        "instructions": _instructions(role, rule, skills),
    }
    if review is not None:
        answer["review"] = review
    if guarded is not None:
        answer.update(guarded.sections())
        answer["instructions"] += (
            " The ports you may use are in `interface` (every port names its `source`), "
            "the block's requirements in `requirements`."
        )
    if record.role == "planner":
        answer["plan"] = _planner_context(ctx, graph, instance)
    return answer


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
    """
    ctx.require_profile()
    _require_runtime(ctx)
    report = report or SubmitReport()
    queue = _queue(ctx)
    record = _record(queue, task_id)
    if record.status != "dispatched":
        raise AppError(f"task {task_id!r} is {record.status}, not dispatched; call next_task")
    graph = _graph(ctx)
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
        now = await snapshot(ctx, extra=record.allowed_writes)
        changed = _changed(queue.baseline(task_id), now)
        # Outputs of the other agent tasks: their own subagents write them (the guard
        # allows nobody else), whatever their status now: tasks that ran in parallel
        # are submitted one by one, and one may be accepted or rejected before this one.
        others = {
            w for other in queue.all() if other.task_id != task_id for w in other.allowed_writes
        }
        engine = _engine_outputs(graph)
        outside = [p for p in changed if p not in allowed and p not in others and p not in engine]
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
        results = list(
            attempt_checks(instance, attempt, ctx.store, allowed_writes=record.allowed_writes)
        )
        if reviewing:
            if review_reasons:  # nothing was written: the reply is what failed
                results = [r for r in results if r.check_id != OUTPUTS_CHECK]
            results.append(loop.review_result(review_reasons))

        checks: list[CheckResult] = []
        if not outside and not missing and not review_reasons:
            runner = ProfileCheckRunner(ctx)
            for check_id in rule.checks:
                checks.append(await runner.run(check_id, instance))
        results.extend(checks)

        shown = _feedback_filter(ctx, record)
        verdict = await loop.judge(
            rule,
            record,
            results,
            attempt,
            ctx.store.current_hashes(instance.outputs),
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
    }
    if review is not None:
        payload["review"] = {"verdict": review.verdict, "comments": len(review.comments)}
    _emit(ctx, record.run_id, task_id, "agent_turn", payload)
    if verdict.stop is not None:
        _stop(ctx, record)
    return _submit_answer(
        ctx,
        record,
        verdict=verdict,
        reasons=reasons,
        outside=outside,
        missing=missing,
        checks=visible,
    )


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
