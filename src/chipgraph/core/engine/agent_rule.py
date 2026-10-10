"""The agent rule loop: write, check at once, triage, fix within the budget (DESIGN.md 5.2).

```
run_task(task) -> run the rule's checks -> pass: done
                                        -> fail: classify the failure (a FailureLabel)
                                           infra:    retry, no try used (max_infra_retries)
                                           planning: stop, a person answers (needs_human)
                                           other:    the budget decides: retry with the
                                                     failures as feedback (and the escalated
                                                     tier), or stop and write HANDOFF
```

The pieces are small and pure so an out-of-process runtime (whose `submit` step checks
one attempt at a time) can reuse them without this loop:

- `classify` / `FailureClassifier`: the `FailureLabel` of a failed attempt, from its
  `CheckResult`s and `AgentResult`, through a data-driven `LabelMap`;
- `AttemptBudget`: counts tries (infra retries are counted apart), sums tokens and cost,
  escalates the model tier after the first failed try, and says when and why it is
  exhausted (`tries` | `tokens` | `cost` | `stagnation`);
- `render_feedback`: the bounded redo instruction for the next try;
- `FeedbackFilter`: what a role whose read policy denies some artifacts (the testbench
  Author, `rtl`) is shown of a failed check: no issue in a denied file, no denied path,
  no source excerpt, and a note when something was withheld. The loop below applies it
  to the redo text of such a role (denying by the role's kinds and globs; a runtime that
  knows the task's denied paths passes them too);
- `attempt_checks`: the checks the engine adds itself (missing inputs, missing or empty
  outputs, files written outside the outputs).

`AgentRuleExecutor` runs the loop over an `AgentRuntime`. It is a `CheckingExecutor`:
the scheduler hands it its check runner and journal, so each try's checks run once and
each runtime call is one `agent_turn` event. The number of runtime calls for one
instance is never more than `budget.tries + max_infra_retries`.

A stop that only a person can undo (the budget ran out, or a person must answer
questions) is remembered under the state directory with the instance's inputs hash:
a later run (or `resume`) reports the same stop without calling the runtime again,
until an input changes or the instance is rewound (`Scheduler.rewind`).

Generic: no model, harness, tool or project is named here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from chipgraph.core.config.models import DecideCfg
from chipgraph.core.contracts import AgentResult, Budget, CheckResult, Issue, ModelTier
from chipgraph.core.contracts.rule import RuleInstance, RuleSpec
from chipgraph.core.contracts.types import FailureLabel
from chipgraph.core.engine.decide import (
    DecisionLog,
    Deferred,
    ModelBackend,
    Question,
    decide,
)
from chipgraph.core.engine.graph import kind_for
from chipgraph.core.engine.scheduler import CheckRunner, ExecContext, ExecEventType, ExecOutcome
from chipgraph.core.plugin_api.protocols import AgentRuntime
from chipgraph.core.plugin_api.types import AgentTask
from chipgraph.core.runtime.executor import instance_inputs_hash, task_for
from chipgraph.core.runtime.roles import find_role, model_ladder, path_denied
from chipgraph.core.state.artifacts import ArtifactStore
from chipgraph.core.state.layout import StateLayout

DEFAULT_MAX_INFRA_RETRIES = 2
"""Runtime calls an instance may repeat for infrastructure errors, on top of its tries."""

ExhaustReason = Literal["tries", "tokens", "cost", "stagnation"]
"""Why an `AttemptBudget` is exhausted."""

StopReason = Literal["tries", "tokens", "cost", "stagnation", "infra", "needs_human", "planning"]
"""Why the loop stopped without success: an `ExhaustReason`, or one of
`infra` (infra retries used up), `needs_human` (the agent asked), `planning` (the checks
show a problem in the spec or plan, which a person must fix)."""

STICKY_STOPS: frozenset[StopReason] = frozenset(
    {"tries", "tokens", "cost", "stagnation", "needs_human", "planning"}
)
"""Stops that hold until an input changes or the instance is rewound. An `infra` stop
does not: once the tool is fixed, `resume` tries again."""

# Synthetic check ids for the checks the engine adds to every attempt.
INPUTS_CHECK = "agent.inputs"
OUTPUTS_CHECK = "agent.outputs"
WRITES_CHECK = "agent.writes"
RUNTIME_CHECK = "agent.runtime"

_LABEL_PRECEDENCE: tuple[FailureLabel, ...] = ("planning", "context", "constraint", "verification")
"""When failures carry different labels, the first of these wins: a spec problem is not
fixed by editing the outputs, a missing input not by following a naming rule, and so on."""


# --- the label map ----------------------------------------------------------------------


class LabelMap(BaseModel):
    """Which `FailureLabel` a failing check stands for (data-driven, packs extend it).

    `issues` maps an issue `rule` prefix and `checks` a check id prefix to a label. A
    prefix matches an id equal to it or continuing with one of `. _ - / : [`; the
    longest matching prefix wins, and an issue's rule wins over its check's id. An
    `error` status is always `infra` (the check could not run). A failing check that
    matches nothing counts as `verification`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    checks: dict[str, FailureLabel] = Field(
        default_factory=dict, description="Check id prefix -> label."
    )
    issues: dict[str, FailureLabel] = Field(
        default_factory=dict, description="Issue rule prefix -> label."
    )

    def merged(self, other: LabelMap) -> LabelMap:
        """This map extended by `other`; `other` wins on the same prefix."""
        return LabelMap(
            checks={**self.checks, **other.checks}, issues={**self.issues, **other.issues}
        )

    def for_check(self, check_id: str) -> FailureLabel | None:
        """The label for `check_id`, or `None` if no prefix matches."""
        return _longest_match(self.checks, check_id)

    def for_issue(self, rule: str) -> FailureLabel | None:
        """The label for an issue `rule`, or `None` if no prefix matches."""
        return _longest_match(self.issues, rule) if rule else None


_SEPARATORS = "._-/:["


def _longest_match(table: Mapping[str, FailureLabel], value: str) -> FailureLabel | None:
    best: tuple[int, FailureLabel] | None = None
    for prefix, label in table.items():
        matches = value == prefix or (
            value.startswith(prefix)
            and len(value) > len(prefix)
            and value[len(prefix)] in _SEPARATORS
        )
        if matches and (best is None or len(prefix) > best[0]):
            best = (len(prefix), label)
    return best[1] if best is not None else None


DEFAULT_LABEL_MAP = LabelMap(
    checks={
        # Project rules: where files go, how they are named and listed, what is generated.
        "layout": "constraint",
        "naming": "constraint",
        "filelist": "constraint",
        "generated": "constraint",
        "hardcode": "constraint",
        "duplicate": "constraint",
        # Checks on the content itself.
        "lint": "verification",
        "sim": "verification",
        "test": "verification",
        "formal": "verification",
        "synth": "verification",
        # The engine's own checks (see `attempt_checks`).
        INPUTS_CHECK: "context",
        OUTPUTS_CHECK: "context",
        WRITES_CHECK: "constraint",
    },
    issues={
        "input.missing": "context",
        "output.missing": "context",
        "output.empty": "context",
        "write.outside": "constraint",
        "layout": "constraint",
        "naming": "constraint",
        "filelist": "constraint",
        "generated": "constraint",
        "spec.ambiguous": "planning",
        "spec.conflict": "planning",
        "req.conflict": "planning",
    },
)
"""The default `LabelMap`:

| matches (check id / issue rule prefix)                         | label        |
|----------------------------------------------------------------|--------------|
| any check with status `error`, a runtime that raised           | infra        |
| issue `spec.ambiguous`, `spec.conflict`, `req.conflict`        | planning     |
| issue `input.missing`, `output.missing`, `output.empty`;       | context      |
| checks `agent.inputs`, `agent.outputs`                         |              |
| issue `write.outside`, `layout`, `naming`, `filelist`,         | constraint   |
| `generated`; checks `layout`, `naming`, `filelist`,            |              |
| `generated`, `hardcode`, `duplicate`, `agent.writes`           |              |
| checks `lint`, `sim`, `test`, `formal`, `synth`; anything else | verification |
"""

_LACKS_INPUT_RE = re.compile(
    r"\b(missing|lack(?:s|ed|ing)?|not (?:found|provided|available|given)|unavailable|"
    r"cannot (?:find|read|open)|no access)\b",
    re.IGNORECASE,
)
"""An agent that gave up (`failed`) and says it is missing something: `context`."""


# --- classify ---------------------------------------------------------------------------


def failing(results: Iterable[CheckResult]) -> tuple[CheckResult, ...]:
    """The results that are not ok (`fail` or `error`)."""
    return tuple(r for r in results if not r.ok)


def _result_label(result: CheckResult, label_map: LabelMap) -> FailureLabel | None:
    if result.status == "error":
        return "infra"
    issue_labels = [
        label
        for issue in result.issues
        if issue.severity == "error"
        for label in (label_map.for_issue(issue.rule),)
        if label is not None
    ]
    if issue_labels:
        return _first_by_precedence(issue_labels)
    return label_map.for_check(result.check_id)


def _first_by_precedence(labels: Iterable[FailureLabel]) -> FailureLabel:
    found = set(labels)
    for label in _LABEL_PRECEDENCE:
        if label in found:
            return label
    return "infra" if "infra" in found else "verification"


def deterministic_label(
    results: Sequence[CheckResult],
    agent: AgentResult | None = None,
    *,
    label_map: LabelMap = DEFAULT_LABEL_MAP,
) -> FailureLabel | None:
    """The label the deterministic rules give a failed attempt, or `None` if none applies.

    `agent` is the attempt's `AgentResult` (`None` when the runtime raised: `infra`).
    Rules, in order: no result -> `infra`; `needs_human` -> `planning`; every failing
    check errored -> `infra`; otherwise the failing checks' labels (an errored check
    among failing ones is ignored: its retry would not fix the others), the first by
    precedence (planning, context, constraint, verification); an agent that gave up and
    says it lacks something -> `context`. `None` when nothing matched.
    """
    if agent is None:
        return "infra"
    if agent.status == "needs_human":
        return "planning"
    bad = failing(results)
    labels: list[FailureLabel] = []
    unmatched = False
    if bad:
        per_result = [_result_label(r, label_map) for r in bad]
        if all(label == "infra" for label in per_result):
            return "infra"
        for label in per_result:
            if label is None:
                unmatched = True
            elif label != "infra":
                labels.append(label)
    if agent.status == "failed" and any(
        _LACKS_INPUT_RE.search(text) for text in (*agent.open_questions, *agent.assumptions)
    ):
        labels.append("context")
    if not labels:
        return None
    if unmatched:
        labels.append("verification")
    return _first_by_precedence(labels)


def classify(
    results: Sequence[CheckResult],
    agent: AgentResult | None = None,
    *,
    label_map: LabelMap = DEFAULT_LABEL_MAP,
) -> FailureLabel:
    """The `FailureLabel` of a failed attempt, by the deterministic rules only.

    See `deterministic_label`; when no rule applies the label is `verification`.
    """
    label = deterministic_label(results, agent, label_map=label_map)
    return label if label is not None else "verification"


_MODEL_CHOICES: tuple[FailureLabel, ...] = ("context", "constraint", "verification", "planning")


class FailureClassifier:
    """Classifies a failed attempt: deterministic rules first, then `decide()`.

    With no `backend`, this is `classify`. With one, a failure no rule explains is put
    to `decide()` as a multiple-choice question (`triage.agent.<hash>`) whose context
    is the redo feedback; an answer that cannot be had (deferred, undecided, refused)
    counts as `verification`. `infra` is never a model's answer: it is decided by the
    check and runtime statuses alone.
    """

    def __init__(
        self,
        *,
        label_map: LabelMap = DEFAULT_LABEL_MAP,
        backend: ModelBackend | None = None,
        log: DecisionLog | None = None,
        cfg: DecideCfg | None = None,
    ) -> None:
        if backend is not None and log is None:
            raise ValueError("a FailureClassifier with a model backend needs a DecisionLog")
        self.label_map = label_map
        self.backend = backend
        self.log = log
        self.cfg = cfg

    async def classify(
        self, results: Sequence[CheckResult], agent: AgentResult | None = None
    ) -> FailureLabel:
        """The `FailureLabel` of a failed attempt."""
        label = deterministic_label(results, agent, label_map=self.label_map)
        if label is not None:
            return label
        if self.backend is None or self.log is None:
            return "verification"
        context = render_feedback(results, "verification", max_chars=2000)
        digest = hashlib.sha256(context.encode("utf-8")).hexdigest()[:16]
        question = Question(
            id=f"triage.agent.{digest}",
            prompt=(
                "An agent's attempt failed these checks. Is the cause missing context "
                "(an input or output is missing), a broken project rule (constraint), "
                "wrong content (verification), or a spec or plan a person must fix "
                "(planning)?"
            ),
            choices=_MODEL_CHOICES,
            context=context,
        )

        def no_rule(_: Question) -> None:
            return None

        try:
            answer = await decide(
                question, rules=(no_rule,), backend=self.backend, cfg=self.cfg, log=self.log
            )
        except Exception:  # undecided, a refusing or failing backend: never stop the loop
            return "verification"
        if isinstance(answer, Deferred):
            return "verification"
        value = question.match(answer.value)
        for choice in _MODEL_CHOICES:
            if value == choice:
                return choice
        return "verification"


# --- the engine's own checks of an attempt ----------------------------------------------


def _synthetic(check_id: str, issues: Sequence[Issue]) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        status="fail" if issues else "pass",
        issues=tuple(issues),
        duration_s=0.0,
        idempotency_key=f"{check_id}:"
        + hashlib.sha256(
            json.dumps([i.model_dump(mode="json") for i in issues], sort_keys=True).encode()
        ).hexdigest(),
    )


def attempt_checks(
    instance: RuleInstance,
    agent: AgentResult,
    store: ArtifactStore,
    *,
    allowed_writes: Sequence[str],
) -> tuple[CheckResult, ...]:
    """The checks the engine runs on every attempt, before the rule's own.

    - `agent.inputs`: an input file of the instance is missing (`input.missing`);
    - `agent.outputs`: an output is missing (`output.missing`) or empty (`output.empty`);
    - `agent.writes`: the agent reports writing a file outside `allowed_writes`
      (`write.outside`).

    Each returns `pass` when it found nothing, so the feedback can say what was fine.
    """
    input_issues = [
        Issue(file=ref.path, rule="input.missing", msg=f"input {ref.path} does not exist")
        for ref in instance.inputs
        if ref.path is not None and not store.exists(ref)
    ]
    output_issues: list[Issue] = []
    for ref in instance.outputs:
        if ref.path is None:
            continue
        if not store.exists(ref):
            output_issues.append(
                Issue(
                    file=ref.path, rule="output.missing", msg=f"output {ref.path} was not written"
                )
            )
        elif (store.root / ref.path).is_file() and (store.root / ref.path).stat().st_size == 0:
            output_issues.append(
                Issue(file=ref.path, rule="output.empty", msg=f"output {ref.path} is empty")
            )
    allowed = set(allowed_writes)
    write_issues = [
        Issue(
            file=path,
            rule="write.outside",
            msg=f"{path} is not one of the outputs; write only: {', '.join(sorted(allowed))}",
        )
        for path in sorted(set(agent.files_written) - allowed)
    ]
    return (
        _synthetic(INPUTS_CHECK, input_issues),
        _synthetic(OUTPUTS_CHECK, output_issues),
        _synthetic(WRITES_CHECK, write_issues),
    )


def runtime_error_result(message: str) -> CheckResult:
    """A `CheckResult` (status `error`) standing for a runtime call that raised."""
    return CheckResult(
        check_id=RUNTIME_CHECK,
        status="error",
        issues=(Issue(rule="runtime.error", msg=message),),
        log_tail=message,
        duration_s=0.0,
        idempotency_key=f"{RUNTIME_CHECK}:error",
    )


# --- feedback ---------------------------------------------------------------------------

_FIX_HINT: dict[FailureLabel, str] = {
    "verification": "Fix the content so these checks pass; keep what already passes.",
    "constraint": (
        "Follow the project's rules (layout, naming, filelists, generated files) and "
        "write only the allowed outputs."
    ),
    "context": (
        "Write every output, non-empty. If an input you need is missing, do not guess: "
        "say so in open_questions."
    ),
    "planning": (
        "The checks point at the spec or plan, not at your work. Do not work around it: "
        "list the questions a person must answer in open_questions."
    ),
    "infra": (
        "The last attempt hit a tool or infrastructure error, not a problem in your work. "
        "Do the same task again."
    ),
}


def _issue_line(issue: Issue) -> str:
    where = issue.file or ""
    if where and issue.line is not None:
        where = f"{where}:{issue.line}"
    rule = f"[{issue.rule}] " if issue.rule else ""
    prefix = f"{where}: " if where else ""
    return f"{prefix}{rule}{issue.msg}".strip()


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def render_feedback(
    results: Sequence[CheckResult],
    label: FailureLabel,
    *,
    attempt: int | None = None,
    max_issues: int = 20,
    max_issues_per_check: int = 8,
    max_log_chars: int = 600,
    max_chars: int = 6000,
    withheld: int = 0,
) -> str:
    """The redo instruction for the next try: what failed, where, and what to fix.

    Lists each failing check with up to `max_issues_per_check` `file:line` issues (at
    most `max_issues` in all), the tail of its log when it has no error issue, the label and
    a hint for it. Bounded: long logs and messages are cut, and the whole text is at
    most `max_chars` characters. `withheld` (from `FeedbackFilter.apply`, over the
    same `results`) adds `WITHHELD_NOTE`, so the agent knows some output was hidden.
    """
    bad = failing(results)
    head = f"Attempt {attempt} failed" if attempt is not None else "The last attempt failed"
    lines = [
        f"{head} ({label}): {len(bad)} check(s) did not pass." if bad else f"{head} ({label}).",
        "",
    ]
    shown = 0
    for result in bad:
        lines.append(f"- check `{result.check_id}`: {result.status}")
        errors = [i for i in result.issues if i.severity == "error"] or list(result.issues)
        take = max(0, min(max_issues_per_check, max_issues - shown))
        for issue in errors[:take]:
            lines.append(f"  - {_cut(_issue_line(issue), 300)}")
        shown += min(take, len(errors))
        if len(errors) > take:
            lines.append(f"  - … and {len(errors) - take} more issue(s)")
        # No error issue explains the failure (none, or only warnings; or a filter
        # withheld them): the log's tail is the explanation.
        if not any(i.severity == "error" for i in errors) and result.log_tail.strip():
            tail = result.log_tail.strip()[-max_log_chars:]
            lines.append("  log tail:")
            lines.extend(f"    {line}" for line in tail.splitlines())
    lines.extend(["", f"What to fix: {_FIX_HINT[label]}"])
    note = f"\n\n{WITHHELD_NOTE}" if withheld else ""
    text = "\n".join(lines)
    if len(text) + len(note) > max_chars:
        text = text[: max(max_chars - len(note) - 2, 0)] + "\n…"
    return text + note


# --- feedback for a role that must not see some artifacts --------------------------------

WITHHELD_NOTE = (
    "Some check output was withheld: it points into files your role must not see (the "
    "design). A failure in the design was hidden; fix the test only if the spec says so, "
    "otherwise report needs_human with the question."
)
"""Added to the redo instruction when `FeedbackFilter.apply` withheld anything."""

_EXCERPT_RE = re.compile(r"^\s*(?:\d+\s*)?\|")
"""A source excerpt line of a compiler message: a line-number gutter, `   12 |   ...`, or
the gutter of its caret line, `      |    ^~~`."""
_CARET_RE = re.compile(r"^\s*[\^~]+[\s\^~]*$")
"""A caret line with no gutter: only `^` and `~` under a quoted source line."""
_PATH_TOKEN_RE = re.compile(r"[^\s'\"`()\[\]{}<>,;:|=]+")
"""A run of characters that may be a file path (paths are matched, then checked)."""


def _looks_like_path(token: str) -> bool:
    return "/" in token or "." in token.strip(".")


class FeedbackFilter(BaseModel):
    """What a role must not be shown of a failed check (DESIGN.md 5.1: the testbench
    Author never sees the design).

    A path is denied when it matches one of `denied` (repo-relative paths and globs, as
    `path_denied` reads them; an absolute path under `root` is made relative first), or
    when its file kind (`kind_for`, by suffix) is one of `kinds`. `apply` then

    - drops every issue whose `file` is denied;
    - replaces each denied path in an issue's message with `<kind>` (the first of
      `kinds`, else `<withheld>`), and cuts message lines that are source excerpts
      (a `NN |` gutter, a caret line);
    - drops every line of the log tail that names a denied path or is a source excerpt;

    and keeps the rest: issues in other files (the role's own outputs) and plain
    behavioural text (`expected 0x2, got 0x0`).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    denied: tuple[str, ...] = Field(
        default=(), description="Repo-relative paths and globs the role must not see."
    )
    kinds: tuple[str, ...] = Field(
        default=(), description="Artifact kinds the role must not see (matched by suffix)."
    )
    root: str | None = Field(
        default=None, description="Absolute project root, to relativise absolute paths."
    )

    @classmethod
    def for_role(
        cls, role_id: str | None, *, denied: Iterable[str] = (), root: str | None = None
    ) -> FeedbackFilter | None:
        """The filter for a task of `role_id`, or `None` when its role may see everything.

        `denied` is the task's own list (its `denied_reads`, when the runtime computed
        one); the role's `deny_globs` are always added.
        """
        role = find_role(role_id) if role_id is not None else None
        if role is None or role.read_policy.mode != "deny":
            return None
        policy = role.read_policy
        return cls(
            denied=tuple(dict.fromkeys((*denied, *policy.deny_globs))),
            kinds=tuple(policy.deny_kinds),
            root=root,
        )

    @property
    def placeholder(self) -> str:
        """What a denied path is replaced with in a message."""
        return f"<{self.kinds[0]}>" if self.kinds else "<withheld>"

    def denies(self, path: str | None) -> bool:
        """Whether `path` (repo-relative or absolute, maybe with `:line`) is denied."""
        if not path:
            return False
        text = path.strip().replace("\\", "/")
        if self.root is not None:
            root = self.root.rstrip("/") + "/"
            if text.startswith(root):
                text = text[len(root) :]
        while text.startswith("./"):
            text = text[2:]
        if self.kinds and kind_for(text) in self.kinds:
            return True
        return not text.startswith("/") and path_denied(text, self.denied)

    def _scrub(self, text: str, *, drop_named: bool) -> tuple[str, int]:
        """`text` without excerpt lines and denied paths; and how much was withheld."""
        kept: list[str] = []
        withheld = 0
        for line in text.splitlines():
            if _EXCERPT_RE.match(line) or _CARET_RE.match(line):
                withheld += 1
                continue
            named = [
                m.group(0)
                for m in _PATH_TOKEN_RE.finditer(line)
                if _looks_like_path(m.group(0)) and self.denies(m.group(0))
            ]
            if named and drop_named:
                withheld += 1
                continue
            for token in sorted(set(named), key=len, reverse=True):
                line = line.replace(token, self.placeholder)
            withheld += bool(named)
            kept.append(line)
        return "\n".join(kept), withheld

    def apply(self, results: Iterable[CheckResult]) -> tuple[tuple[CheckResult, ...], int]:
        """`results` as the role may see them, and the number of items withheld."""
        shown: list[CheckResult] = []
        withheld = 0
        for result in results:
            issues: list[Issue] = []
            for issue in result.issues:
                if self.denies(issue.file):
                    withheld += 1
                    continue
                msg, cut = self._scrub(issue.msg, drop_named=False)
                withheld += cut
                if cut and not msg.strip():  # the message was all source excerpt
                    continue
                issues.append(issue if msg == issue.msg else issue.model_copy(update={"msg": msg}))
            tail, cut = self._scrub(result.log_tail, drop_named=True)
            withheld += cut
            shown.append(result.model_copy(update={"issues": tuple(issues), "log_tail": tail}))
        return tuple(shown), withheld


def failure_signature(results: Iterable[CheckResult]) -> str:
    """A hash of the failing checks and their issues: the same failures, the same hash."""
    entries = sorted(
        json.dumps(
            {
                "check": r.check_id,
                "status": r.status,
                "issues": sorted(
                    json.dumps([i.file, i.line, i.rule, i.msg], sort_keys=True) for i in r.issues
                ),
            },
            sort_keys=True,
        )
        for r in failing(results)
    )
    return hashlib.sha256(json.dumps(entries).encode("utf-8")).hexdigest()


# --- the budget -------------------------------------------------------------------------


class BudgetState(BaseModel):
    """What an `AttemptBudget` has counted so far (persistable, e.g. by a task queue)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    tries_used: int = Field(default=0, ge=0, description="Tries used (failed or passed).")
    failed_tries: int = Field(default=0, ge=0, description="Tries that failed.")
    infra_failures: int = Field(
        default=0, ge=0, description="Calls that failed on infra (not tries)."
    )
    tokens: int = Field(default=0, ge=0, description="Tokens reported over every call.")
    cost: float = Field(default=0.0, ge=0, description="Cost reported over every call, USD.")
    tokens_reported: bool = Field(default=False, description="Whether any call reported tokens.")
    cost_reported: bool = Field(default=False, description="Whether any call reported a cost.")
    last_signature: str | None = Field(
        default=None, description="Output hashes and failures of the last failed try."
    )
    stagnant: bool = Field(
        default=False, description="The last two failed tries had the same signature."
    )


class AttemptBudget:
    """Counts the tries of one agent rule instance against its `Budget`.

    - A try is one runtime call whose result was checked (passed or failed); an infra
      retry is counted apart (`record_infra`) and is not a try.
    - `spend` adds a call's reported `tokens`/`cost`; `Budget.tokens` and `max_cost`
      cap them.
    - `tier` is the first tier until a try failed, then `escalate` if it is set.
    - Stagnation: two failed tries in a row with the same output hashes and the same
      failures exhaust the budget early (when `stagnation` is on).
    """

    def __init__(
        self,
        budget: Budget,
        *,
        tier: ModelTier | None = None,
        escalate: ModelTier | None = None,
        max_cost: float | None = None,
        stagnation: bool = True,
        state: BudgetState | None = None,
    ) -> None:
        self.budget = budget
        self.first_tier: ModelTier = tier if tier is not None else budget.tier
        self.escalate: ModelTier | None = escalate if tier is not None else budget.escalate
        self.max_cost = max_cost
        self.stagnation = stagnation
        self.state = state if state is not None else BudgetState()

    @classmethod
    def for_rule(
        cls, rule: RuleSpec, *, max_cost: float | None = None, stagnation: bool = True
    ) -> AttemptBudget:
        """The budget of an agent rule, with the model tiers of its role (`model_ladder`)."""
        role = find_role(rule.role) if rule.role is not None else None
        tier, escalate = model_ladder(role, rule.budget)
        return cls(
            rule.budget, tier=tier, escalate=escalate, max_cost=max_cost, stagnation=stagnation
        )

    @property
    def tries(self) -> int:
        """The tries the budget allows."""
        return self.budget.tries

    @property
    def remaining(self) -> int:
        """Tries left."""
        return max(self.budget.tries - self.state.tries_used, 0)

    @property
    def tier(self) -> ModelTier:
        """The model tier for the next try."""
        if self.state.failed_tries >= 1 and self.escalate is not None:
            return self.escalate
        return self.first_tier

    def task_budget(self) -> Budget:
        """The `Budget` to hand the runtime for the next try, at the current tier."""
        tier = self.tier
        escalate = self.escalate if self.escalate is not None and tier != self.escalate else None
        return Budget(
            tries=self.budget.tries, tier=tier, escalate=escalate, tokens=self.budget.tokens
        )

    def spend(self, result: AgentResult | None) -> None:
        """Add the tokens and cost one runtime call reported (any call, retry or not)."""
        if result is None:
            return
        update: dict[str, Any] = {}
        if result.tokens is not None:
            update["tokens"] = self.state.tokens + result.tokens
            update["tokens_reported"] = True
        if result.cost is not None:
            update["cost"] = self.state.cost + result.cost
            update["cost_reported"] = True
        if update:
            self.state = self.state.model_copy(update=update)

    def record_pass(self) -> None:
        """Count a try that passed."""
        self.state = self.state.model_copy(update={"tries_used": self.state.tries_used + 1})

    def record_failure(
        self, results: Iterable[CheckResult], output_hashes: Mapping[str, str]
    ) -> None:
        """Count a failed try, with the outputs it left and the checks it failed."""
        signature = hashlib.sha256(
            json.dumps(
                {
                    "outputs": dict(sorted(output_hashes.items())),
                    "failures": failure_signature(results),
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        stagnant = self.state.last_signature == signature
        self.state = self.state.model_copy(
            update={
                "tries_used": self.state.tries_used + 1,
                "failed_tries": self.state.failed_tries + 1,
                "last_signature": signature,
                "stagnant": stagnant,
            }
        )

    def record_infra(self) -> None:
        """Count a call that failed on infra (not a try)."""
        self.state = self.state.model_copy(update={"infra_failures": self.state.infra_failures + 1})

    @property
    def over_spend(self) -> ExhaustReason | None:
        """`tokens` or `cost` when a cap is reached, else `None`."""
        if self.budget.tokens is not None and self.state.tokens >= self.budget.tokens:
            return "tokens"
        if self.max_cost is not None and self.state.cost >= self.max_cost:
            return "cost"
        return None

    @property
    def exhausted(self) -> ExhaustReason | None:
        """Why no further try may run, or `None` while one may."""
        spent = self.over_spend
        if spent is not None:
            return spent
        if self.stagnation and self.state.stagnant:
            return "stagnation"
        if self.state.tries_used >= self.budget.tries:
            return "tries"
        return None


# --- remembered stops -------------------------------------------------------------------


class AgentStop(BaseModel):
    """An agent rule instance that stopped and must not run again by itself."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    instance_id: str = Field(description="The rule instance that stopped.")
    inputs_hash: str = Field(description="Hash of the instance's inputs when it stopped.")
    reason: StopReason = Field(description="Why it stopped.")
    label: FailureLabel = Field(description="The label of the last failure.")
    message: str = Field(description="The rule_fail message.")
    summary: dict[str, Any] = Field(description="The `agent` payload of the stop.")
    result: AgentResult = Field(description="The final AgentResult.")


class StopStore:
    """Remembered stops, one JSON file per instance under `<state>/agent_rules/`."""

    def __init__(self, layout: StateLayout) -> None:
        self._dir = layout.state_dir / "agent_rules"

    def _path(self, instance_id: str) -> Path:
        return self._dir / f"{hashlib.sha256(instance_id.encode('utf-8')).hexdigest()}.json"

    def get(self, instance_id: str) -> AgentStop | None:
        """The remembered stop of `instance_id`, if any."""
        path = self._path(instance_id)
        if not path.is_file():
            return None
        return AgentStop.model_validate_json(path.read_text(encoding="utf-8"))

    def put(self, stop: AgentStop) -> None:
        """Remember `stop` (atomic write)."""
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(stop.instance_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(stop.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def delete(self, instance_id: str) -> None:
        """Forget the stop of `instance_id` (a no-op if none)."""
        self._path(instance_id).unlink(missing_ok=True)


# --- the executor -----------------------------------------------------------------------


def _noop_emit(_type: ExecEventType, _payload: dict[str, Any]) -> None:
    return None


AgentStatus = Literal["done", "failed", "needs_human", "budget_exhausted"]


class _InstanceRun:
    """The state of the loop for one instance: budget, merged results, last failures."""

    def __init__(
        self,
        executor: AgentRuleExecutor,
        rule: RuleSpec,
        instance: RuleInstance,
        base: AgentTask,
        ctx: ExecContext,
        inputs_hash: str,
    ) -> None:
        self.executor = executor
        self.rule = rule
        self.instance = instance
        self.iid = instance.instance_id
        self.base = base
        self.ctx = ctx
        self.inputs_hash = inputs_hash
        self.budget = AttemptBudget.for_rule(
            rule, max_cost=executor.max_cost, stagnation=executor.stagnation
        )
        self.calls = 0
        self.assumptions: list[str] = []
        self.questions: list[str] = []
        self.files: tuple[str, ...] = ()
        self.feedback: str | None = None
        self.previous: list[str] = []
        self.infra_note: str | None = None
        self.last_label: FailureLabel = "infra"
        self.last_results: tuple[CheckResult, ...] = ()
        self.shown = FeedbackFilter.for_role(rule.role, root=str(executor.store.root.resolve()))

    def redo(self, results: Sequence[CheckResult], label: FailureLabel, **kw: Any) -> str:
        """`render_feedback` over what the role may see (`FeedbackFilter`)."""
        if self.shown is None:
            return render_feedback(results, label, **kw)
        visible, withheld = self.shown.apply(results)
        return render_feedback(visible, label, withheld=withheld, **kw)

    # --- one call ----------------------------------------------------------------

    def task(self) -> AgentTask:
        """The task for the next runtime call: feedback, try number and current tier."""
        context = dict(self.base.context)
        context["try"] = str(self.budget.state.tries_used + 1)
        context["tier"] = self.budget.tier
        if self.feedback is not None:
            context["feedback"] = self.feedback
            context["previous_failures"] = json.dumps(self.previous)
        if self.infra_note is not None:
            context["infra_retry"] = self.infra_note
        return self.base.model_copy(
            update={"context": context, "budget": self.budget.task_budget()}
        )

    def merge(self, result: AgentResult) -> None:
        for text in result.assumptions:
            if text not in self.assumptions:
                self.assumptions.append(text)
        for text in result.open_questions:
            if text not in self.questions:
                self.questions.append(text)
        self.files = result.files_written

    async def check(self, agent: AgentResult) -> tuple[CheckResult, ...]:
        """The engine's own checks of the attempt, then the rule's checks (each once)."""
        results = attempt_checks(
            self.instance, agent, self.executor.store, allowed_writes=self.base.allowed_writes
        )
        if self.ctx.run_check is not None:
            for check_id in self.rule.checks:
                results += (await self.ctx.run_check(check_id),)
        return results

    def turn(
        self,
        *,
        try_no: int,
        tier: ModelTier,
        agent: AgentResult | None,
        label: FailureLabel | None,
        counted: bool,
        results: Sequence[CheckResult],
    ) -> None:
        """Journal one runtime call as an `agent_turn` event."""
        payload: dict[str, Any] = {
            "phase": "try",
            "call": self.calls,
            "try": try_no,
            "counted": counted,
            "tier": tier,
            "status": agent.status if agent is not None else "error",
            "label": label,
            "failed_checks": [r.check_id for r in failing(results)],
            "tokens": agent.tokens if agent is not None else None,
            "cost": agent.cost if agent is not None else None,
        }
        if agent is not None and agent.open_questions:
            payload["open_questions"] = list(agent.open_questions)
        self.ctx.emit("agent_turn", payload)

    # --- results -------------------------------------------------------------------

    def summary(
        self, status: AgentStatus, reason: StopReason | None, label: FailureLabel | None
    ) -> dict[str, Any]:
        """The `agent` payload of the instance's final `rule_done`/`rule_fail` event."""
        state = self.budget.state
        bad = failing(self.last_results) if status != "done" else ()
        return {
            "instance": self.iid,
            "status": status,
            "tries": state.tries_used,
            "max_tries": self.budget.tries,
            "infra_failures": state.infra_failures,
            "calls": self.calls,
            "reason": reason,
            "label": label,
            "tier": self.budget.tier,
            "failed_checks": [r.check_id for r in bad],
            "failures": [_issue_summary(r) for r in bad][:10],
            "tokens": state.tokens if state.tokens_reported else None,
            "cost": state.cost if state.cost_reported else None,
        }

    def final(self, status: AgentStatus, questions: Sequence[str] = ()) -> AgentResult:
        """The instance's `AgentResult`: merged over every call, tokens and cost summed."""
        state = self.budget.state
        open_questions = list(self.questions)
        open_questions.extend(q for q in questions if q not in open_questions)
        return AgentResult(
            status=status,
            files_written=self.files,
            assumptions=tuple(self.assumptions),
            open_questions=tuple(open_questions),
            tokens=state.tokens if state.tokens_reported else None,
            cost=state.cost if state.cost_reported else None,
        )

    def done(self) -> ExecOutcome:
        result = self.final("done")
        self.executor.results[self.iid] = result
        if self.executor.stops is not None:
            self.executor.stops.delete(self.iid)
        return ExecOutcome(
            ok=True,
            payload={
                "agent": self.summary("done", None, None),
                "result": result.model_dump(mode="json"),
            },
        )

    def stop(
        self, reason: StopReason, label: FailureLabel, *, questions: Sequence[str] = ()
    ) -> ExecOutcome:
        status: AgentStatus
        if reason in ("needs_human", "planning"):
            status = "needs_human"
        elif reason == "infra":
            status = "failed"
        else:
            status = "budget_exhausted"
        result = self.final(status, questions)
        info = self.summary(status, reason, label)
        message = _stop_message(info)
        self.executor.results[self.iid] = result
        if self.executor.stops is not None and reason in STICKY_STOPS:
            self.executor.stops.put(
                AgentStop(
                    instance_id=self.iid,
                    inputs_hash=self.inputs_hash,
                    reason=reason,
                    label=label,
                    message=message,
                    summary=info,
                    result=result,
                )
            )
        return _stop_outcome(label, message, info, result, again=False)

    # --- the loop --------------------------------------------------------------------

    async def run(self) -> ExecOutcome:
        max_calls = self.executor.max_calls(self.rule)
        while self.calls < max_calls:  # the hard bound: tries + max_infra_retries
            outcome = await self.step()
            if outcome is not None:
                return outcome
        # Unreachable while the counters are consistent (every call records a try or
        # an infra retry, and either one stops the loop at its limit); kept so the
        # loop can never run past its bound.
        return self.stop("tries", self.last_label)

    async def step(self) -> ExecOutcome | None:
        """One runtime call and its checks; an outcome when the loop ends, else `None`."""
        self.calls += 1
        try_no = self.budget.state.tries_used + 1
        tier = self.budget.tier
        task = self.task()
        agent: AgentResult | None = None
        error = ""
        try:
            agent = await self.executor.runtime.run_task(task)
        except Exception as exc:  # a runtime failure not caused by the work: infra
            error = f"{type(exc).__name__}: {exc}"
        self.budget.spend(agent)
        if agent is not None:
            self.merge(agent)

        if agent is not None and agent.status == "needs_human":
            self.turn(
                try_no=try_no, tier=tier, agent=agent, label="planning", counted=False, results=()
            )
            self.last_label = "planning"
            return self.stop("needs_human", "planning")

        results = (runtime_error_result(error),) if agent is None else await self.check(agent)
        if agent is not None and agent.status == "done" and not failing(results):
            self.budget.record_pass()
            self.turn(
                try_no=try_no, tier=tier, agent=agent, label=None, counted=True, results=results
            )
            return self.done()

        label = await self.executor.classifier.classify(results, agent)
        self.last_label = label
        self.last_results = results

        if label == "infra":
            self.budget.record_infra()
            self.turn(
                try_no=try_no, tier=tier, agent=agent, label=label, counted=False, results=results
            )
            spent = self.budget.over_spend
            if spent is not None:
                return self.stop(spent, label)
            if self.budget.state.infra_failures > self.executor.max_infra_retries:
                return self.stop("infra", label)
            self.infra_note = self.redo(results, "infra", max_chars=1500)
            return None

        self.infra_note = None
        self.budget.record_failure(
            results, self.executor.store.current_hashes(self.instance.outputs)
        )
        self.turn(try_no=try_no, tier=tier, agent=agent, label=label, counted=True, results=results)
        if label == "planning":
            questions = [
                _issue_line(issue)
                for r in failing(results)
                for issue in r.issues
                if issue.severity == "error"
            ][:10] or ["the checks point at the spec or plan; a person must decide"]
            return self.stop("planning", label, questions=questions)
        reason = self.budget.exhausted
        if reason is not None:
            return self.stop(reason, label)
        self.feedback = self.redo(results, label, attempt=self.budget.state.tries_used)
        self.previous = [r.check_id for r in failing(results)]
        return None


class AgentRuleExecutor:
    """Runs `kind: agent` rules through an `AgentRuntime`, in the bounded loop above.

    `checks` is only used by `execute` (outside a scheduler); under a scheduler the
    `ExecContext`'s check runner is used, so each check runs once per try. `layout`
    enables remembered stops (see the module docstring); without it, every run starts
    with a fresh budget. `max_cost` caps the summed `AgentResult.cost` (USD). `results`
    holds the last `AgentResult` of each instance.
    """

    def __init__(
        self,
        runtime: AgentRuntime,
        *,
        store: ArtifactStore,
        layout: StateLayout | None = None,
        classifier: FailureClassifier | None = None,
        max_infra_retries: int = DEFAULT_MAX_INFRA_RETRIES,
        max_cost: float | None = None,
        stagnation: bool = True,
        checks: CheckRunner | None = None,
    ) -> None:
        if max_infra_retries < 0:
            raise ValueError("max_infra_retries must be >= 0")
        self.runtime = runtime
        self.store = store
        self.stops = StopStore(layout) if layout is not None else None
        self.classifier = classifier if classifier is not None else FailureClassifier()
        self.max_infra_retries = max_infra_retries
        self.max_cost = max_cost
        self.stagnation = stagnation
        self.checks = checks
        self.results: dict[str, AgentResult] = {}

    def max_calls(self, rule: RuleSpec) -> int:
        """The most runtime calls one instance of `rule` can make: tries + infra retries."""
        return rule.budget.tries + self.max_infra_retries

    def forget(self, instance_id: str) -> None:
        """Forget a remembered stop (`Scheduler.rewind` calls this)."""
        if self.stops is not None:
            self.stops.delete(instance_id)

    async def execute(self, rule: RuleSpec, instance: RuleInstance) -> ExecOutcome:
        """Run the loop outside a scheduler, with `self.checks` (if any) and no journal."""
        runner = self.checks

        async def run_check(check_id: str) -> CheckResult:
            assert runner is not None
            return await runner.run(check_id, instance)

        ctx = ExecContext(
            run_id="", run_check=run_check if runner is not None else None, emit=_noop_emit
        )
        return await self.execute_checked(rule, instance, ctx)

    async def execute_checked(
        self, rule: RuleSpec, instance: RuleInstance, ctx: ExecContext
    ) -> ExecOutcome:
        """Run the loop for one instance, checking each try through `ctx`."""
        iid = instance.instance_id
        try:
            base = task_for(rule, instance)
        except ValueError as exc:
            return ExecOutcome(ok=False, failure_label="planning", message=str(exc))

        inputs_hash = instance_inputs_hash(self.store, instance)
        if self.stops is not None:
            stop = self.stops.get(iid)
            if stop is not None and stop.inputs_hash == inputs_hash:
                self.results[iid] = stop.result
                return _stop_outcome(
                    stop.label, stop.message, stop.summary, stop.result, again=True
                )
            if stop is not None:  # an input changed: a fresh budget
                self.stops.delete(iid)

        return await _InstanceRun(self, rule, instance, base, ctx, inputs_hash).run()


def _stop_outcome(
    label: FailureLabel,
    message: str,
    info: Mapping[str, Any],
    result: AgentResult,
    *,
    again: bool,
) -> ExecOutcome:
    payload: dict[str, Any] = {
        "agent": {**info, "stopped_earlier": again},
        "result": result.model_dump(mode="json"),
    }
    if result.open_questions:
        payload["open_questions"] = list(result.open_questions)
    if again:
        message = (
            f"{message} (stopped in an earlier run; change an input or run "
            f"`chipgraph rewind {info.get('instance', '')}` to try again)"
        )
    return ExecOutcome(ok=False, failure_label=label, message=message, payload=payload)


def _issue_summary(result: CheckResult) -> str:
    issues = [i for i in result.issues if i.severity == "error"] or list(result.issues)
    first = _cut(_issue_line(issues[0]), 200) if issues else _cut(result.log_tail.strip(), 200)
    more = f" (+{len(issues) - 1} more)" if len(issues) > 1 else ""
    return f"{result.check_id}: {result.status}" + (f": {first}{more}" if first else "")


def _stop_message(info: Mapping[str, Any]) -> str:
    reason = info["reason"]
    tries = f"{info['tries']} of {info['max_tries']} tries"
    infra = info["infra_failures"]
    if infra:
        tries += f", {infra} infra failure{'' if infra == 1 else 's'}"
    failed = ", ".join(info["failed_checks"]) or "none"
    head = {
        "tries": "budget exhausted (tries)",
        "tokens": "budget exhausted (token cap)",
        "cost": "budget exhausted (cost cap)",
        "stagnation": "stopped early: two tries in a row left the same outputs and failures",
        "infra": "stopped: infrastructure errors persisted",
        "needs_human": "stopped: the agent needs a person to answer open questions",
        "planning": "stopped: the checks point at the spec or plan",
    }[reason]
    return f"agent {head} after {tries}; last failures [{info['label']}]: {failed}"


__all__ = [
    "DEFAULT_LABEL_MAP",
    "DEFAULT_MAX_INFRA_RETRIES",
    "INPUTS_CHECK",
    "OUTPUTS_CHECK",
    "RUNTIME_CHECK",
    "STICKY_STOPS",
    "WITHHELD_NOTE",
    "WRITES_CHECK",
    "AgentRuleExecutor",
    "AgentStop",
    "AttemptBudget",
    "BudgetState",
    "ExhaustReason",
    "FailureClassifier",
    "FeedbackFilter",
    "LabelMap",
    "StopReason",
    "StopStore",
    "attempt_checks",
    "classify",
    "deterministic_label",
    "failing",
    "failure_signature",
    "render_feedback",
    "runtime_error_result",
]
