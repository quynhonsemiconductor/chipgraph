"""HANDOFF.md and the status summary (DESIGN.md 6.2).

Every agent session starts by reading ``HANDOFF.md``: what was done, who is waited on,
open questions, and the next step. This module derives that summary — and the compact
``chipgraph status`` table — purely from a run's journal events, using
:func:`chipgraph.core.state.journal.replay` for per-instance status and reading messages,
gate ids and open questions straight out of the event payloads the scheduler writes
(``core/engine/scheduler.py``).

Payload keys relied on (as written by ``Scheduler._process_instance``/``_execute``):

- ``rule_done``: ``payload["skipped"] == "fresh"`` marks a fresh (not rebuilt) instance.
- ``rule_fail``: ``payload["message"]`` is the human-readable failure message;
  ``event.failure_label`` carries the :class:`FailureLabel`.
- ``gate_wait``: ``payload["gate"]`` is the (already-formatted) gate id.
- ``rule_fail``/``agent_turn``: an optional ``payload["open_questions"]`` list of strings.
- ``rule_fail`` of an agent rule (``core/engine/agent_rule.py``): ``payload["agent"]``, the
  loop's summary (status, tries, max_tries, infra_failures, reason, label, failed_checks,
  failures, tokens, cost, stopped_earlier); read into :class:`AgentStopInfo` so HANDOFF.md
  says how many tries were used, why the loop stopped and what failed last.
- ``run_stop``: only carries *counts* today (``done``, ``failed``, ...), not instance ids.
  The scheduler currently emits no per-instance event for a *blocked* instance (one whose
  dependency failed/waited/was itself blocked), so ``build_handoff`` reads blocked instance
  ids from an optional ``payload["blocked"]`` list on the last ``run_stop`` event if present,
  and otherwise reports no blocked instances. This is forward-compatible with the scheduler
  later being extended to record that list; it is not a change to the scheduler itself.

A HANDOFF.md is a snapshot of the run that wrote it. When an agent task stops, the file is
written at once, for the run that dispatched the task's last try; a task accepted after that
still shows as waiting there. The next ``next_task`` or ``build`` writes its own, accurate one.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from chipgraph.core.contracts.event import Event
from chipgraph.core.contracts.types import FailureLabel
from chipgraph.core.state.journal import RunState, replay
from chipgraph.core.state.layout import StateLayout

_FAILURE_NEXT_STEP: dict[FailureLabel, str] = {
    "constraint": "revert the hand edit or approve it, then `chipgraph build {target}`",
    "context": "add the missing input or output, then `chipgraph resume {run_id}`",
    "verification": "fix the failing check, then `chipgraph resume {run_id}`",
    "infra": "check the tool or connection (`chipgraph doctor`), then `chipgraph resume {run_id}`",
    "planning": "answer the open questions, then `chipgraph resume {run_id}`",
}
_DEFAULT_NEXT_STEP = "`chipgraph resume {run_id}`"
_AGENT_STEP_KEY = {"needs_human": "needs_human", "planning": "needs_human", "infra": "infra"}
"""An agent stop reason -> its `_AGENT_NEXT_STEP` key; any other reason is `exhausted`."""
_AGENT_NEXT_STEP = {
    "needs_human": (
        "answer the open questions for `{instance}` (update its inputs), then "
        "`chipgraph build {target}`"
    ),
    "infra": (
        "check the tool or connection (`chipgraph doctor`), then `chipgraph rewind {instance}` "
        "and `chipgraph build {target}`: the stop is remembered until the instance is rewound "
        "or an input changes, so `resume` would not run it again"
    ),
    "exhausted": (
        "read the last failures of `{instance}`; fix the cause (its inputs, the rule or "
        "its budget) or write the output by hand, then `chipgraph rewind {instance}` and "
        "`chipgraph build {target}`"
    ),
}
_NOTHING_TO_DO = "nothing to do: the target is up to date."


class AgentStopInfo(BaseModel):
    """How an agent rule's loop stopped, from its ``rule_fail`` payload's ``agent`` key."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    status: str = ""
    reason: str | None = None
    tries: int = 0
    max_tries: int = 0
    infra_failures: int = 0
    label: FailureLabel | None = None
    failed_checks: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    tokens: int | None = None
    cost: float | None = None
    stopped_earlier: bool = False


class FailedItem(BaseModel):
    """One failed rule instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instance: str
    label: FailureLabel | None = None
    message: str = ""
    agent: AgentStopInfo | None = None


def _agent_info(payload: dict[str, Any]) -> AgentStopInfo | None:
    raw = payload.get("agent")
    if not isinstance(raw, dict):
        return None
    try:
        return AgentStopInfo.model_validate(raw)
    except ValidationError:
        return None


class WaitingItem(BaseModel):
    """One rule instance waiting on a human gate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instance: str
    gate_id: str
    who: tuple[str, ...] = ()


class Handoff(BaseModel):
    """Everything the next agent session needs on resume (DESIGN.md 6.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    target: str
    generated_at: AwareDatetime
    done: tuple[str, ...] = ()
    skipped_fresh: tuple[str, ...] = ()
    failed: tuple[FailedItem, ...] = ()
    waiting_gate: tuple[WaitingItem, ...] = ()
    blocked: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = ()


def _collect_open_questions(events: Sequence[Event]) -> tuple[str, ...]:
    seen: set[str] = set()
    questions: list[str] = []
    for event in events:
        if event.type not in ("rule_fail", "agent_turn"):
            continue
        raw = event.payload.get("open_questions")
        if not isinstance(raw, list):
            continue
        for item in raw:
            text = str(item)
            if text not in seen:
                seen.add(text)
                questions.append(text)
    return tuple(questions)


def _next_steps(
    *,
    target: str,
    run_id: str,
    failed: tuple[FailedItem, ...],
    waiting_gate: tuple[WaitingItem, ...],
    blocked: tuple[str, ...],
) -> tuple[str, ...]:
    steps: list[str] = []
    for wait_item in waiting_gate:
        steps.append(f"`chipgraph approve {wait_item.gate_id} --instance {wait_item.instance}`")
    for fail_item in failed:
        if fail_item.agent is not None and fail_item.agent.reason is not None:
            key = _AGENT_STEP_KEY.get(fail_item.agent.reason, "exhausted")
            template = _AGENT_NEXT_STEP[key]
        elif fail_item.label is not None:
            template = _FAILURE_NEXT_STEP.get(fail_item.label, _DEFAULT_NEXT_STEP)
        else:
            template = _DEFAULT_NEXT_STEP
        steps.append(template.format(target=target, run_id=run_id, instance=fail_item.instance))

    if not steps and not blocked:
        return (_NOTHING_TO_DO,)

    seen: set[str] = set()
    deduped: list[str] = []
    for step in steps:
        if step not in seen:
            seen.add(step)
            deduped.append(step)
    return tuple(deduped)


def build_handoff(
    events: Sequence[Event],
    *,
    target: str,
    approvers: Mapping[str, Sequence[str]] | None = None,
    now: datetime | None = None,
) -> Handoff:
    """Derive a :class:`Handoff` from a run's journal events.

    ``approvers`` maps a gate id to the people known to approve it (informational only;
    the scheduler does not record approvers in the journal). ``now`` overrides the
    generated-at timestamp, mainly for deterministic tests.
    """
    ordered = sorted(events, key=lambda event: event.seq)
    state: RunState = replay(list(ordered))
    approver_map = approvers or {}

    last_rule_done: dict[str, Event] = {}
    last_rule_fail: dict[str, Event] = {}
    last_gate_wait: dict[str, Event] = {}
    last_run_stop: Event | None = None
    for event in ordered:
        if event.rule_instance is not None:
            if event.type == "rule_done":
                last_rule_done[event.rule_instance] = event
            elif event.type == "rule_fail":
                last_rule_fail[event.rule_instance] = event
            elif event.type == "gate_wait":
                last_gate_wait[event.rule_instance] = event
        if event.type == "run_stop":
            last_run_stop = event

    done: list[str] = []
    skipped_fresh: list[str] = []
    failed: list[FailedItem] = []
    waiting_gate: list[WaitingItem] = []

    for iid in sorted(state.rules):
        status = state.rules[iid]
        if status == "done":
            done_event = last_rule_done.get(iid)
            if done_event is not None and done_event.payload.get("skipped") == "fresh":
                skipped_fresh.append(iid)
            else:
                done.append(iid)
        elif status == "failed":
            fail_event = last_rule_fail.get(iid)
            message = str(fail_event.payload.get("message", "")) if fail_event is not None else ""
            agent = _agent_info(fail_event.payload) if fail_event is not None else None
            failed.append(
                FailedItem(
                    instance=iid, label=state.failures.get(iid), message=message, agent=agent
                )
            )
        elif status == "waiting_gate":
            wait_event = last_gate_wait.get(iid)
            gate_id = str(wait_event.payload.get("gate", "")) if wait_event is not None else ""
            waiting_gate.append(
                WaitingItem(instance=iid, gate_id=gate_id, who=tuple(approver_map.get(gate_id, ())))
            )

    blocked_ids: list[str] = []
    if last_run_stop is not None:
        raw_blocked = last_run_stop.payload.get("blocked")
        if isinstance(raw_blocked, list):
            blocked_ids = [str(iid) for iid in raw_blocked]
    blocked = tuple(sorted(set(blocked_ids)))

    failed_t = tuple(failed)
    waiting_gate_t = tuple(waiting_gate)
    generated_at = now if now is not None else datetime.now(UTC)

    return Handoff(
        run_id=state.run_id,
        target=target,
        generated_at=generated_at,
        done=tuple(done),
        skipped_fresh=tuple(skipped_fresh),
        failed=failed_t,
        waiting_gate=waiting_gate_t,
        blocked=blocked,
        open_questions=_collect_open_questions(ordered),
        next_steps=_next_steps(
            target=target,
            run_id=state.run_id,
            failed=failed_t,
            waiting_gate=waiting_gate_t,
            blocked=blocked,
        ),
    )


def render_markdown(h: Handoff) -> str:
    """Render a short, deterministic ``HANDOFF.md`` body for a :class:`Handoff`."""
    lines: list[str] = [
        f"# Handoff — {h.target}",
        f"Run `{h.run_id}` — generated {h.generated_at.isoformat()}",
        "",
    ]

    total_done = len(h.done) + len(h.skipped_fresh)
    if total_done:
        lines.append("## Done")
        lines.append("")
        if h.skipped_fresh:
            lines.append(f"{total_done} done ({len(h.skipped_fresh)} already up to date, skipped).")
        else:
            lines.append(f"{total_done} done.")
        if h.done:
            lines.append("")
            for instance in h.done:
                lines.append(f"- {instance}")
        lines.append("")

    if h.waiting_gate:
        lines.append("## Waiting for people")
        lines.append("")
        for wait_item in h.waiting_gate:
            who = ", ".join(wait_item.who) if wait_item.who else "unknown"
            lines.append(
                f"- gate `{wait_item.gate_id}` for `{wait_item.instance}` — waiting on: {who}"
            )
        lines.append("")

    if h.failed:
        lines.append("## Failed")
        lines.append("")
        for fail_item in h.failed:
            label = fail_item.label if fail_item.label is not None else "unlabeled"
            lines.append(f"- `{fail_item.instance}` [{label}]: {fail_item.message}")
            if fail_item.agent is not None:
                lines.extend(_render_agent(fail_item.agent))
        lines.append("")

    if h.blocked:
        lines.append("## Blocked")
        lines.append("")
        lines.append(f"{len(h.blocked)} blocked:")
        for instance in h.blocked:
            lines.append(f"- {instance}")
        lines.append("")

    if h.open_questions:
        lines.append("## Open questions")
        lines.append("")
        for question in h.open_questions:
            lines.append(f"- {question}")
        lines.append("")

    lines.append("## Next step")
    lines.append("")
    for i, step in enumerate(h.next_steps, start=1):
        lines.append(f"{i}. {step}")

    return "\n".join(lines) + "\n"


def _render_agent(info: AgentStopInfo) -> list[str]:
    """The indented detail lines of an agent rule's stop, under its failed item."""
    used = f"{info.tries} of {info.max_tries} tries"
    if info.infra_failures:
        used += f" (+{info.infra_failures} infra failures, not counted)"
    spent = []
    if info.tokens is not None:
        spent.append(f"{info.tokens} tokens")
    if info.cost is not None:
        spent.append(f"${info.cost:.4f}")
    lines = [f"  - agent: {info.status}, reason `{info.reason}`, {used}"]
    if spent:
        lines[0] += "; spent " + ", ".join(spent)
    if info.failures:
        lines.append(f"  - last failures [{info.label}]:")
        lines.extend(f"    - {failure}" for failure in info.failures)
    elif info.failed_checks:
        lines.append(f"  - last failures [{info.label}]: {', '.join(info.failed_checks)}")
    if info.stopped_earlier:
        lines.append("  - stopped in an earlier run; not run again until an input changes")
    return lines


def render_status(state: RunState, *, target: str) -> str:
    """Render a compact plain-text status table for ``chipgraph status``."""
    lines: list[str] = []
    counts: dict[str, int] = {}
    for iid in sorted(state.rules):
        status = state.rules[iid]
        counts[status] = counts.get(status, 0) + 1
        lines.append(f"{status.upper():<12} {iid}")

    total = len(state.rules)
    parts = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
    summary = f"{total} rule(s) for {target}"
    if parts:
        summary += f": {parts}"
    lines.append(summary)

    return "\n".join(lines) + "\n"


def write_handoff(layout: StateLayout, h: Handoff) -> Path:
    """Atomically write ``HANDOFF.md`` for a run's directory and return its path."""
    path = layout.run_dir(h.run_id) / "HANDOFF.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(render_markdown(h), encoding="utf-8")
    os.replace(tmp_path, path)
    return path


__all__ = [
    "AgentStopInfo",
    "FailedItem",
    "Handoff",
    "WaitingItem",
    "build_handoff",
    "render_markdown",
    "render_status",
    "write_handoff",
]
