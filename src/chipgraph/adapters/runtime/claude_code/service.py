"""What the MCP tools of runtime `claude-code` do: `next_task`, `get_context`, `submit`.

The loop (DESIGN.md 5.5, spike S7), driven by the plugin command in the user's Claude
Code session::

    next_task(target)   run the build; every agent task it reaches is queued; hand out
                        the ready ones (several at once: they run in parallel)
    get_context(id)     a role subagent asks for its task: inputs as text, outputs,
                        role, skills and their text (refused when an input is labelled
                        `nda`, or is of a kind the task's role must not see)
    submit(id, result)  the engine checks the work: every file changed since dispatch
                        is one of the task's outputs, the outputs exist, the rule's
                        checks pass; accept, or reject and count an attempt

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

from chipgraph.adapters.runtime.claude_code.agents import DEFAULT_TIER_MODELS, agent_type
from chipgraph.adapters.runtime.claude_code.runtime import output_hashes
from chipgraph.app.build import make_scheduler, pack_search_paths
from chipgraph.app.checks import ProfileCheckRunner
from chipgraph.app.context import AppContext
from chipgraph.app.errors import AppError
from chipgraph.core.contracts import AgentResult, CheckResult, Event, RuleInstance, RuleSpec
from chipgraph.core.contracts.event import EventType
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
    RoleSpec,
    SkillError,
    SkillSet,
    SkillSpec,
    denied_reads,
    find_role,
    load_skills,
    path_denied,
)
from chipgraph.core.state import journal as journal_mod
from chipgraph.core.state.artifacts import hash_file

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
    tier = record.current_tier
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
        )
    )


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
        "tool_calls": queue.tool_calls(record.task_id),
        "tool_call_cap": record.tool_call_cap,
        "prompt": (
            f"You are doing chipgraph task {record.task_id!r}. First call get_context with "
            f"task_id={record.task_id!r}, then write only the files in its outputs."
        ),
    }


def _blocked(ctx: AppContext, summary: RunSummary, skip: set[str]) -> list[dict[str, str]]:
    """Why the build stopped, per instance: gates, people, failed rules (from the journal)."""
    events = journal_mod.read(ctx.layout.journal(summary.run_id)).events
    gates = {
        e.rule_instance: str(e.payload.get("gate", "")) for e in events if e.type == "gate_wait"
    }
    fails = {
        e.rule_instance: str(e.payload.get("message", "")) for e in events if e.type == "rule_fail"
    }
    blocked: list[dict[str, str]] = []
    for iid in summary.waiting_gate:
        blocked.append(
            {"instance": iid, "reason": f"waiting for gate {gates.get(iid, '?')!r} to be approved"}
        )
    for iid in summary.failed:
        if iid not in skip:
            blocked.append({"instance": iid, "reason": fails.get(iid) or "failed"})
    return blocked


# --- next_task --------------------------------------------------------------------------


async def next_task(ctx: AppContext, target: str = "*") -> dict[str, Any]:
    """Run the build for `target` and hand out every agent task it reached.

    Returns `tasks` (newly dispatched: each goes to its own subagent, in parallel),
    `in_progress` (dispatched earlier and not submitted yet), and either `done: true`
    (the build finished) or `waiting` (it is blocked on a gate, a person, or a failure).
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
    stray: list[dict[str, str]] = []
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
        if now is None:
            now = await snapshot(ctx)
        if record.outside:
            before = queue.baseline(iid)
            left = [p for p in record.outside if before.get(p) != now.get(p)]
            if left:
                stray.append(
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
            "outputs": list(record.allowed_writes),
            "agent": _agent_for(record.role),
            "model": _model_for(ctx, record),
            "denied_reads": list(record.denied_reads),
        }
        _emit(ctx, record.run_id, iid, "agent_turn", payload)
        dispatched.append(record)

    answer: dict[str, Any] = {
        "run_id": summary.run_id,
        "tasks": [_task_entry(ctx, queue, r) for r in dispatched],
        "in_progress": [_task_entry(ctx, queue, r) for r in in_progress],
        "done": False,
        "waiting": None,
        "blocked": [],
    }
    if dispatched or in_progress:
        return answer
    blocked = stray + _blocked(ctx, summary, skip={s["instance"] for s in stray})
    if summary.ok and not blocked:
        answer["done"] = True
        return answer
    answer["blocked"] = blocked
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

    inputs: list[dict[str, Any]] = []
    for ref in instance.inputs:
        if ref.path is None:
            inputs.append(_model_input(ctx, str(ref.model_key)))
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

    _emit(
        ctx,
        record.run_id,
        task_id,
        "agent_turn",
        {"phase": "context", "attempt": record.dispatches},
    )
    root = ctx.root.resolve()
    return {
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
        "previous_rejection": list(record.reasons),
        "inputs": inputs,
        "instructions": _instructions(role, rule, skills),
    }


# --- submit -----------------------------------------------------------------------------


def _check_reason(result: CheckResult) -> str:
    msgs = "; ".join(i.msg for i in result.issues[:5]) or result.log_tail[-500:]
    return f"check {result.check_id} {result.status}: {msgs}".strip()


async def submit(
    ctx: AppContext, task_id: str, report: SubmitReport | None = None
) -> dict[str, Any]:
    """Check a dispatched task's work and accept or reject it (DESIGN.md 5.2).

    Rejected when a file outside the task's outputs changed since dispatch, an output
    is missing, or one of the rule's checks fails. A rejection counts an attempt; the
    last try turns the task `budget_exhausted`. A report with `status: needs_human`
    hands the task to a person without using a try.
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
                "open_questions": list(report.open_questions),
            },
        )
        return _submit_answer(record, accepted=False, reasons=[], outside=[], missing=[], checks=[])

    record = queue.mark_submitted(task_id)
    try:
        allowed = set(record.allowed_writes)
        now = await snapshot(ctx, extra=record.allowed_writes)
        changed = _changed(queue.baseline(task_id), now)
        # Outputs of the other agent tasks: their own subagents write them (the guard
        # allows nobody else), also when one of them was accepted since this dispatch,
        # as happens when tasks that ran in parallel are submitted one by one.
        others = {
            w
            for other in queue.all()
            if other.task_id != task_id
            and (other.status in ACTIVE_STATUSES or other.status == "accepted")
            for w in other.allowed_writes
        }
        engine = _engine_outputs(graph)
        outside = [p for p in changed if p not in allowed and p not in others and p not in engine]
        missing = [p for p in record.allowed_writes if not (ctx.root / p).is_file()]

        checks: list[CheckResult] = []
        if not outside and not missing:
            runner = ProfileCheckRunner(ctx)
            for check_id in rule.checks:
                checks.append(await runner.run(check_id, instance))

        reasons: list[str] = []
        if outside:
            reasons.append(f"changed files outside the task's outputs: {', '.join(outside)}")
        if missing:
            reasons.append(f"missing outputs: {', '.join(missing)}")
        reasons.extend(_check_reason(c) for c in checks if not c.ok)
        accepted = not reasons
        result = AgentResult(
            status="done" if accepted else "failed",
            files_written=tuple(p for p in changed if p in allowed),
            assumptions=report.assumptions,
            open_questions=report.open_questions,
        )
        if accepted:
            record = queue.accept(
                task_id,
                result=result,
                output_hashes=output_hashes(ctx.store, task_for(rule, instance)),
            )
        else:
            record = queue.reject(
                task_id, result=result, reasons=tuple(reasons), outside=tuple(outside)
            )
    except BaseException:
        with contextlib.suppress(QueueError):
            queue.unsubmit(task_id)
        raise

    for check in checks:
        _emit(ctx, record.run_id, task_id, "check_result", check.model_dump(mode="json"))
    _emit(
        ctx,
        record.run_id,
        task_id,
        "agent_turn",
        {
            "phase": "submit",
            "status": record.status,
            "attempt": record.dispatches,
            "reasons": reasons,
            "files_written": list(result.files_written),
            "assumptions": list(report.assumptions),
            "open_questions": list(report.open_questions),
        },
    )
    return _submit_answer(
        record, accepted=accepted, reasons=reasons, outside=outside, missing=missing, checks=checks
    )


def _submit_answer(
    record: AgentTaskRecord,
    *,
    accepted: bool,
    reasons: list[str],
    outside: list[str],
    missing: list[str],
    checks: list[CheckResult],
) -> dict[str, Any]:
    if record.status == "rejected":
        hint = "call next_task: it hands the task out again with the rejection reasons"
    elif record.status == "budget_exhausted":
        hint = "the task used all its tries; a person must look at it (see HANDOFF.md)"
    elif record.status == "needs_human":
        hint = "a person must answer the open questions; then build again"
    else:
        hint = "call next_task for the next tasks"
    return {
        "task_id": record.task_id,
        "accepted": accepted,
        "status": record.status,
        "attempts": record.attempts,
        "tries": record.tries,
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
