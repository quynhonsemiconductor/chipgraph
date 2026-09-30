"""`decide()`: the fast decision layer for multiple-choice questions (DESIGN 5.4).

A `Question` has a fixed set of `choices` (classify a failing log, escalate or not, is
a command risky ...). `decide()` answers it in cost order:

1. **rules**: deterministic functions of the question, in order; the first that
   returns a `Decision` wins (`backend="rule"`);
2. the **small** model tier, through a `ModelBackend`;
3. the **large** model tier, when the small answer's confidence is under the
   threshold (or the answer is not one of the choices, which counts as confidence 0).

A large answer under its own threshold is still returned (the best valid answer seen,
in fact), and logged as low confidence: AI never blocks (DESIGN 4.8), the caller
decides what a low-confidence answer means. The thresholds and the tiers that may be
asked come from the profile's `decide` section (`DecideCfg`), per question-id prefix.

A backend may also answer `Deferred`: an out-of-process host answers the question
later (the model runs in the user's own agent session), and a later `decide()` call for
the same question picks the answer up.

Every step is appended to the decision log, `<state>/decisions.jsonl` under the state
backend (machine-local run state, not the human-auditable approvals of
`.chipgraph/decisions/`): question id, backend, tier, value, confidence, threshold,
model id when known, and duration.

Generic: no model, provider or harness is named here; backends live in the adapters.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, Self, runtime_checkable

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from chipgraph.core.config.models import DecideCfg, DecideTier
from chipgraph.core.contracts import DataLabel, Decision
from chipgraph.core.state.layout import StateLayout

DECISION_LOG_NAME = "decisions.jsonl"
"""File name of the decision log, directly under the state directory."""

_MAX_REASON_CHARS = 500
"""Longest model `reason` kept in the decision log; longer ones are cut."""

DecisionValue = str | int | float | bool
"""A decided value, as in the `Decision` contract."""


class DecideError(Exception):
    """`decide()` cannot run as asked: a bad rule result, or nothing that could answer."""


class Undecided(DecideError):
    """No rule answered and no model tier gave an answer that is one of the choices."""

    def __init__(self, message: str, *, question_id: str) -> None:
        super().__init__(message)
        self.question_id = question_id


class Question(BaseModel):
    """A multiple-choice question for `decide()`.

    `choices` are the only acceptable answers. `context` is the text given to a model
    with the question (a log excerpt, say); `labels` are the data labels of that
    context, so a context labeled 'nda' never reaches a cloud model.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    id: str = Field(
        min_length=1,
        description="Stable id of the question, e.g. 'triage.<hash>'; prefixes pick overrides.",
    )
    prompt: str = Field(min_length=1, description="The question itself.")
    kind: Literal["choice"] = Field(
        default="choice", description="Answer kind: 'choice', exactly one of `choices`."
    )
    choices: tuple[str, ...] = Field(min_length=1, description="The acceptable answers.")
    context: str = Field(default="", description="Text a model reads to answer the question.")
    labels: tuple[DataLabel, ...] = Field(
        default=(), description="Data labels of the context; 'nda' only goes to local models."
    )

    @model_validator(mode="after")
    def _check_choices(self) -> Self:
        if any(not c.strip() for c in self.choices):
            raise ValueError("a choice must not be empty")
        folded = [c.strip().casefold() for c in self.choices]
        if len(set(folded)) != len(folded):
            raise ValueError(f"choices must be distinct (ignoring case): {list(self.choices)}")
        return self

    def match(self, value: object) -> str | None:
        """The choice `value` names, or `None` if it names none.

        An exact choice matches; otherwise a string equal to one choice ignoring case and
        surrounding whitespace does (models write 'RTL' for 'rtl'). Non-strings never do.
        """
        if not isinstance(value, str):
            return None
        if value in self.choices:
            return value
        folded = value.strip().casefold()
        for choice in self.choices:
            if choice.strip().casefold() == folded:
                return choice
        return None

    def fingerprint(self) -> str:
        """A sha256 of the whole question: a recorded answer is only reused for the same one."""
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()


class ModelAnswer(BaseModel):
    """What a model tier answered: a value (maybe not a valid choice) and its confidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str | None = Field(description="The answer, or None when it could not be read.")
    confidence: float = Field(ge=0, le=1, description="The model's confidence, 0 to 1.")
    model: str | None = Field(default=None, description="The model id that answered, if known.")
    reason: str = Field(default="", description="The model's short justification.")


class Deferred(BaseModel):
    """The question is queued: an out-of-process host answers it later.

    Call `decide()` again for the same question once the host has answered.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    question_id: str = Field(description="The question waiting for an answer.")
    tier: DecideTier = Field(description="The model tier the question waits for.")
    model: str | None = Field(default=None, description="The model the host should use.")
    message: str = Field(default="", description="What the caller should do next.")


@runtime_checkable
class ModelBackend(Protocol):
    """Asks one model tier a `Question`: answers now, or defers to a host."""

    name: str

    async def ask(self, question: Question, tier: DecideTier) -> ModelAnswer | Deferred:
        """Ask `tier` the question; raise to refuse (e.g. 'nda' data for a cloud model)."""
        ...


Rule = Callable[[Question], Decision | None]
"""A deterministic answer: a `Decision` with `backend="rule"`, or `None` to pass."""


def rule_decision(question: Question, value: str, confidence: float = 1.0) -> Decision:
    """The `Decision` a rule returns for `question`: `value`, from a rule."""
    return Decision(question_id=question.id, value=value, confidence=confidence, backend="rule")


# --- the decision log ------------------------------------------------------------------

DecisionEvent = Literal["decided", "escalated", "rejected", "deferred", "undecided", "error"]
"""One step of `decide()`:

- `decided`: the final answer (from a rule or a model tier);
- `escalated`: a tier's answer was not accepted and the next tier is asked;
- `rejected`: the last tier's answer was not accepted (an earlier one may still win);
- `deferred`: a tier will be answered later by a host;
- `undecided`: no valid answer at all;
- `error`: the backend raised (refused the question, the budget is spent ...).
"""


def _now() -> datetime:
    return datetime.now(UTC)


class DecisionLogEntry(BaseModel):
    """One line of the decision log."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    ts: AwareDatetime = Field(default_factory=_now, description="When the step ended.")
    question_id: str = Field(description="The question.")
    event: DecisionEvent = Field(description="What this step was.")
    backend: Literal["rule", "small", "large"] = Field(description="Who answered.")
    tier: DecideTier | None = Field(default=None, description="The model tier; None for rules.")
    value: DecisionValue | None = Field(
        default=None, description="The answer as one of the choices, if it was one."
    )
    answer: str | None = Field(
        default=None, description="The raw model answer, when it was not one of the choices."
    )
    confidence: float = Field(default=0.0, ge=0, le=1, description="Confidence of the answer.")
    threshold: float | None = Field(
        default=None, ge=0, le=1, description="The confidence the tier needed."
    )
    low_confidence: bool = Field(
        default=False, description="A decision returned although under its threshold."
    )
    model: str | None = Field(default=None, description="The model id, when known.")
    duration_s: float = Field(ge=0, description="Seconds this step took.")
    reason: str = Field(default="", description="The model's justification, cut short.")
    error: str | None = Field(default=None, description="The error, for `event='error'`.")


def decision_log_path(layout: StateLayout) -> Path:
    """Where the decision log lives: `<root>/.chipgraph/state/decisions.jsonl`."""
    return layout.state_dir / DECISION_LOG_NAME


class DecisionLog:
    """The append-only JSONL log of every `decide()` step."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def for_layout(cls, layout: StateLayout) -> DecisionLog:
        """The decision log of the project whose state is under `layout`."""
        return cls(decision_log_path(layout))

    def append(self, entry: DecisionLogEntry) -> None:
        """Append one entry, flushed and fsynced like the run journal."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")
            f.flush()
            os.fsync(f.fileno())

    def read(self) -> list[DecisionLogEntry]:
        """Every complete entry; a partial last line (a crash mid-write) is skipped."""
        if not self.path.is_file():
            return []
        entries: list[DecisionLogEntry] = []
        # The text after the last newline (if any) was never finished: skip it.
        complete = self.path.read_text(encoding="utf-8").split("\n")[:-1]
        for line_no, line in enumerate(complete, start=1):
            if not line.strip():
                continue
            try:
                entries.append(DecisionLogEntry.model_validate(json.loads(line)))
            except ValueError as exc:
                raise DecideError(f"decision log {self.path}: bad line {line_no}") from exc
        return entries


# --- decide ----------------------------------------------------------------------------


def _cut(text: str) -> str:
    return text if len(text) <= _MAX_REASON_CHARS else text[: _MAX_REASON_CHARS - 1] + "…"


def _check_rule_decision(question: Question, rule: Rule, decision: Decision) -> None:
    name = getattr(rule, "__name__", repr(rule))
    if decision.question_id != question.id:
        raise DecideError(
            f"rule {name} answered question {decision.question_id!r}, not {question.id!r}"
        )
    if decision.backend != "rule":
        raise DecideError(f"rule {name} returned a decision with backend {decision.backend!r}")
    if not isinstance(decision.value, str) or decision.value not in question.choices:
        raise DecideError(
            f"rule {name} answered {decision.value!r} to {question.id!r}, which is not one "
            f"of its choices {list(question.choices)}"
        )


def low_confidence(decision: Decision, cfg: DecideCfg) -> bool:
    """True when a model's `decision` is under its tier's threshold (rules never are)."""
    if decision.backend == "rule":
        return False
    tier: DecideTier = "small" if decision.backend == "small" else "large"
    return decision.confidence < cfg.for_question(decision.question_id).threshold(tier)


async def decide(
    question: Question,
    *,
    rules: Iterable[Rule] = (),
    backend: ModelBackend | None = None,
    cfg: DecideCfg | None = None,
    log: DecisionLog,
) -> Decision | Deferred:
    """Answer `question`: rules, then the small model tier, then the large one.

    Returns the `Decision`, or `Deferred` when the backend queued the question for a
    host (call again later). Raises `Undecided` when no rule answered and no tier gave
    one of the choices, `DecideError` when a rule misbehaves or nothing can answer, and
    whatever the backend raises (e.g. a refusal of 'nda' data), after logging it.
    """
    cfg = cfg if cfg is not None else DecideCfg()

    for rule in rules:
        start = time.monotonic()
        decision = rule(question)
        if decision is None:
            continue
        _check_rule_decision(question, rule, decision)
        log.append(
            DecisionLogEntry(
                question_id=question.id,
                event="decided",
                backend="rule",
                value=decision.value,
                confidence=decision.confidence,
                duration_s=time.monotonic() - start,
            )
        )
        return decision

    settings = cfg.for_question(question.id)
    tiers = settings.enabled_tiers
    if not tiers:
        raise DecideError(
            f"no rule answered {question.id!r} and every model tier is disabled "
            "(decide.enabled_tiers)"
        )
    if backend is None:
        raise DecideError(f"no rule answered {question.id!r} and no model backend was given")

    begin = time.monotonic()
    best: tuple[Decision, DecisionLogEntry] | None = None
    for index, tier in enumerate(tiers):
        start = time.monotonic()
        threshold = settings.threshold(tier)
        try:
            answer = await backend.ask(question, tier)
        except Exception as exc:
            log.append(
                DecisionLogEntry(
                    question_id=question.id,
                    event="error",
                    backend=tier,
                    tier=tier,
                    threshold=threshold,
                    duration_s=time.monotonic() - start,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            raise
        duration = time.monotonic() - start

        if isinstance(answer, Deferred):
            log.append(
                DecisionLogEntry(
                    question_id=question.id,
                    event="deferred",
                    backend=tier,
                    tier=tier,
                    threshold=threshold,
                    model=answer.model,
                    duration_s=duration,
                )
            )
            return answer

        value = question.match(answer.value)
        confidence = answer.confidence if value is not None else 0.0
        step = DecisionLogEntry(
            question_id=question.id,
            event="escalated",
            backend=tier,
            tier=tier,
            value=value,
            answer=answer.value if value is None else None,
            confidence=confidence,
            threshold=threshold,
            model=answer.model,
            duration_s=duration,
            reason=_cut(answer.reason),
        )
        if value is not None:
            candidate = Decision(
                question_id=question.id, value=value, confidence=confidence, backend=tier
            )
            if confidence >= threshold:
                log.append(step.model_copy(update={"event": "decided"}))
                return candidate
            if best is None or candidate.confidence >= best[0].confidence:
                best = (candidate, step)

        if index == len(tiers) - 1:
            step = step.model_copy(update={"event": "rejected"})
        log.append(step)

    if best is None:
        log.append(
            DecisionLogEntry(
                question_id=question.id,
                event="undecided",
                backend=tiers[-1],
                tier=tiers[-1],
                duration_s=time.monotonic() - begin,
            )
        )
        raise Undecided(
            f"no model tier ({', '.join(tiers)}) answered {question.id!r} with one of "
            f"its choices {list(question.choices)}",
            question_id=question.id,
        )
    decision, answered = best
    log.append(
        answered.model_copy(
            update={
                "event": "decided",
                "low_confidence": True,
                "ts": _now(),
                "duration_s": time.monotonic() - begin,
            }
        )
    )
    return decision


__all__ = [
    "DECISION_LOG_NAME",
    "DecideError",
    "DecisionEvent",
    "DecisionLog",
    "DecisionLogEntry",
    "DecisionValue",
    "Deferred",
    "ModelAnswer",
    "ModelBackend",
    "Question",
    "Rule",
    "Undecided",
    "decide",
    "decision_log_path",
    "low_confidence",
    "rule_decision",
]
