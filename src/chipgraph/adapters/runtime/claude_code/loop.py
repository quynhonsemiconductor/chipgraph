"""The M2-02a agent rule loop, one submitted attempt at a time (DESIGN.md 5.2).

In an in-process runtime `AgentRuleExecutor` runs the whole loop. In runtime
`claude-code` the user's Claude Code session does each attempt and `submit` checks it,
so this module applies the same pieces (`chipgraph.core.engine.agent_rule`) to one
attempt and keeps the loop's count on the task record (`TaskBudgetState`):

- checks: `attempt_checks` (inputs, outputs, writes) and the rule's checks, each once;
- label: `FailureClassifier` over the default `LabelMap`, the one the in-process loop
  uses (deterministic rules only: no model is asked here);
- budget: `AttemptBudget.for_rule`, restored from the record. An `infra` failure (a
  check that errored, a tool that cannot run) uses no try, and at most
  `max_infra_retries` (2, as in process) of them are retried; two failed tries in a
  row with the same outputs and failures stop the task early (`stagnation`). Tokens and
  cost are not known per task in this runtime (the session reports them only for the
  whole run), so the token and cost caps of the in-process loop do not apply here;
- redo: `render_feedback`, the bounded redo instruction for the next try.

`max_dispatches` (tries plus infra retries) is the hard bound on how often one task is
handed out in a budget cycle: every submit either uses a try or an infra retry, and
the loop stops when either runs out, so the bound is never reached by a live task.
`TaskQueue.dispatch` and `next_task` both refuse to go past it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from chipgraph.core.contracts import AgentResult, CheckResult, Issue, RuleSpec
from chipgraph.core.contracts.types import FailureLabel, ModelTier
from chipgraph.core.engine.agent_rule import (
    DEFAULT_LABEL_MAP,
    DEFAULT_MAX_INFRA_RETRIES,
    AttemptBudget,
    BudgetState,
    FailureClassifier,
    failing,
    render_feedback,
)
from chipgraph.core.runtime.queue import AgentTaskRecord, BudgetStop, TaskBudgetState, TaskStatus

MAX_INFRA_RETRIES = DEFAULT_MAX_INFRA_RETRIES
"""Submits of one task that may fail on infra without using a try (as in process)."""

REVIEW_CHECK = "agent.review"
"""Synthetic check id of a review task's reply (invalid JSON, schema, evidence)."""

MAX_FAILED_CHECKS = 10
MAX_ISSUES_PER_CHECK = 8
MAX_MSG_CHARS = 300
REDO_MAX_CHARS = 6000

_CLASSIFIER = FailureClassifier(label_map=DEFAULT_LABEL_MAP)


def max_dispatches(tries: int, max_infra_retries: int = MAX_INFRA_RETRIES) -> int:
    """The most dispatches of one task in a budget cycle: its tries plus infra retries."""
    return tries + max_infra_retries


def dispatch_limit(record: AgentTaskRecord) -> int:
    """The record's dispatch bound (one from before it was stored: the default)."""
    if record.max_dispatches is not None:
        return record.max_dispatches
    return max_dispatches(record.tries)


@dataclass(frozen=True)
class Verdict:
    """What one submitted attempt comes to: the record's next status and budget state."""

    status: TaskStatus
    label: FailureLabel | None
    counted: bool
    stop: BudgetStop | None
    tier: ModelTier
    state: TaskBudgetState
    tries: int
    failed: tuple[CheckResult, ...]
    questions: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"

    @property
    def tries_left(self) -> int:
        return max(self.tries - self.state.tries_used, 0)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def issue_line(issue: Issue) -> str:
    """`file:line: [rule] msg`, as the redo instruction writes an issue."""
    where = issue.file or ""
    if where and issue.line is not None:
        where = f"{where}:{issue.line}"
    rule = f"[{issue.rule}] " if issue.rule else ""
    return f"{where + ': ' if where else ''}{rule}{issue.msg}".strip()


def _errors(result: CheckResult) -> list[Issue]:
    return [i for i in result.issues if i.severity == "error"] or list(result.issues)


def failure_line(result: CheckResult) -> str:
    """One line for a failing check: its id, status and first issue (or log tail)."""
    issues = _errors(result)
    first = _cut(issue_line(issues[0]), 200) if issues else _cut(result.log_tail.strip(), 200)
    more = f" (+{len(issues) - 1} more)" if len(issues) > 1 else ""
    return f"{result.check_id}: {result.status}" + (f": {first}{more}" if first else "")


def failed_checks(results: Sequence[CheckResult]) -> list[dict[str, Any]]:
    """The failing checks for `submit`'s answer: id, status, issues (capped), log tail."""
    shown: list[dict[str, Any]] = []
    for result in failing(results)[:MAX_FAILED_CHECKS]:
        issues = _errors(result)
        entry: dict[str, Any] = {
            "check_id": result.check_id,
            "status": result.status,
            "issues": [
                {
                    "at": f"{i.file}:{i.line}" if i.file and i.line else i.file,
                    "rule": i.rule,
                    "msg": _cut(i.msg, MAX_MSG_CHARS),
                }
                for i in issues[:MAX_ISSUES_PER_CHECK]
            ],
        }
        if len(issues) > MAX_ISSUES_PER_CHECK:
            entry["more_issues"] = len(issues) - MAX_ISSUES_PER_CHECK
        if not issues and result.log_tail.strip():
            entry["log_tail"] = result.log_tail.strip()[-600:]
        shown.append(entry)
    return shown


def review_result(problems: Sequence[str]) -> CheckResult:
    """A review task's reply as a check: `fail` with one issue per problem."""
    return CheckResult(
        check_id=REVIEW_CHECK,
        status="fail" if problems else "pass",
        issues=tuple(Issue(rule="review.invalid", msg=p) for p in problems),
        duration_s=0.0,
        idempotency_key=f"{REVIEW_CHECK}:{len(problems)}",
    )


def _budget(rule: RuleSpec, prior: TaskBudgetState) -> AttemptBudget:
    budget = AttemptBudget.for_rule(rule)
    budget.state = BudgetState(
        tries_used=prior.tries_used,
        failed_tries=prior.failed_tries,
        infra_failures=prior.infra_failures,
        last_signature=prior.last_signature,
        stagnant=prior.stagnant,
    )
    return budget


async def judge(
    rule: RuleSpec,
    record: AgentTaskRecord,
    results: Sequence[CheckResult],
    agent: AgentResult,
    output_hashes: Mapping[str, str],
    *,
    max_infra_retries: int = MAX_INFRA_RETRIES,
) -> Verdict:
    """Count one submitted attempt against the task's budget and say what comes next.

    `results` are every check of the attempt (the engine's and the rule's);
    `output_hashes` the task's outputs as the attempt left them (for stagnation).
    """
    prior = record.state
    budget = _budget(rule, prior)
    tier = record.current_tier
    bad = failing(results)
    common: dict[str, Any] = {"dispatches": prior.dispatches, "tier": tier}

    def state(**update: Any) -> TaskBudgetState:
        s = budget.state
        return TaskBudgetState(
            tries_used=s.tries_used,
            failed_tries=s.failed_tries,
            infra_failures=s.infra_failures,
            last_signature=s.last_signature,
            stagnant=s.stagnant,
            output_hashes=dict(output_hashes),
            **common,
            **update,
        )

    if not bad:
        budget.record_pass()
        return Verdict(
            status="accepted",
            label=None,
            counted=True,
            stop=None,
            tier=tier,
            state=state(),
            tries=budget.tries,
            failed=(),
        )

    label = await _CLASSIFIER.classify(results, agent)
    lines = tuple(failure_line(r) for r in bad[:MAX_FAILED_CHECKS])
    ids = tuple(r.check_id for r in bad)
    stop: BudgetStop | None = None
    questions: tuple[str, ...] = ()
    if label == "infra":
        budget.record_infra()
        counted = False
        if budget.state.infra_failures > max_infra_retries:
            stop = "infra"
        redo = render_feedback(results, "infra", max_chars=1500)
    else:
        budget.record_failure(results, output_hashes)
        counted = True
        if label == "planning":
            stop = "planning"
            questions = tuple(
                issue_line(i) for r in bad for i in r.issues if i.severity == "error"
            )[:10] or ("the checks point at the spec or plan; a person must decide",)
        else:
            reason = budget.exhausted  # tries | stagnation (no token or cost caps here)
            if reason == "tries" or reason == "stagnation":
                stop = reason
        redo = render_feedback(
            results, label, attempt=budget.state.tries_used, max_chars=REDO_MAX_CHARS
        )
    status: TaskStatus
    if stop == "planning":
        status = "needs_human"
    elif stop is not None:
        status = "budget_exhausted"
    else:
        status = "rejected"
    return Verdict(
        status=status,
        label=label,
        counted=counted,
        stop=stop,
        tier=tier,
        state=state(label=label, failed_checks=ids, failures=lines, redo=redo, stop=stop),
        tries=budget.tries,
        failed=bad,
        questions=questions,
    )


# --- the stop, for the journal and HANDOFF.md ---------------------------------------------

_STOP_STATUS: dict[str, str] = {"planning": "needs_human"}


def stop_info(record: AgentTaskRecord, *, stopped_earlier: bool = False) -> dict[str, Any]:
    """The `agent` payload of a stopped task's `rule_fail` (what `AgentStopInfo` reads)."""
    state = record.state
    reason = state.stop
    return {
        "instance": record.task_id,
        "status": _STOP_STATUS.get(reason or "", "budget_exhausted"),
        "tries": state.tries_used,
        "max_tries": record.tries,
        "infra_failures": state.infra_failures,
        "calls": state.dispatches,
        "reason": reason,
        "label": state.label,
        "tier": state.tier,
        "failed_checks": list(state.failed_checks),
        "failures": list(state.failures),
        "tokens": None,
        "cost": None,
        "stopped_earlier": stopped_earlier,
    }


_STOP_HEAD = {
    "tries": "budget exhausted (tries)",
    "stagnation": "stopped early: two tries in a row left the same outputs and failures",
    "infra": "stopped: infrastructure errors persisted",
    "planning": "stopped: the checks point at the spec or plan",
}


def stop_message(info: Mapping[str, Any]) -> str:
    """The `rule_fail` message of a stopped task, worded as the in-process loop's."""
    tries = f"{info['tries']} of {info['max_tries']} tries"
    infra = int(info["infra_failures"])
    if infra:
        tries += f", {infra} infra failure{'' if infra == 1 else 's'}"
    failed = ", ".join(info["failed_checks"]) or "none"
    head = _STOP_HEAD.get(str(info["reason"]), "stopped")
    return f"agent {head} after {tries}; last failures [{info['label']}]: {failed}"


__all__ = [
    "MAX_INFRA_RETRIES",
    "REVIEW_CHECK",
    "Verdict",
    "dispatch_limit",
    "failed_checks",
    "failure_line",
    "issue_line",
    "judge",
    "max_dispatches",
    "review_result",
    "stop_info",
    "stop_message",
]
