"""`decide()` in runtime `claude-code`: questions answered by a small-model subagent.

chipgraph never calls a model in this runtime (D35), so `ClaudeCodeDecideBackend.ask`
does not answer: it records the question under the state backend and returns
`Deferred`. The plugin then runs the question in the user's Claude Code session::

    pending_decisions()         the MCP tool lists the queued questions, each with the
                                tier's model and a prompt, for a `chipgraph:decider`
                                subagent started with that model
    answer_decision(id, ...)    the MCP tool records the subagent's JSON answer, after
                                checking the value is one of the question's choices
    decide(question) again      the recorded answer comes back from `ask`; if it is
                                under the small threshold, `decide()` asks the large
                                tier, which queues the question again with its model

State, one file per question (never in the repo tree)::

    <state>/runtime/decisions/<sha256(question_id)>.json      PendingDecision

A recorded answer is reused only for the very same question (same fingerprint): a
question id asked with a new prompt, context or choices starts afresh. A question
labeled 'nda' is refused (`NdaBlocked`): every model of this runtime is a cloud model.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from chipgraph.adapters.llm._errors import NdaBlocked
from chipgraph.adapters.llm.decide_backend import question_prompt
from chipgraph.adapters.runtime.claude_code.runtime import PLUGIN_NAME
from chipgraph.core.config.models import DecideTier, ModelsCfg
from chipgraph.core.engine.decide import Deferred, ModelAnswer, Question
from chipgraph.core.runtime import state_key
from chipgraph.core.state.layout import StateLayout

DECIDER_AGENT = f"{PLUGIN_NAME}:decider"
"""The plugin subagent that answers one question (`plugin/agents/decider.md`)."""

DEFAULT_TIER_MODELS: dict[DecideTier, str] = {"small": "haiku", "large": "opus"}
"""Claude Code model aliases per decide tier, when the profile's `models.tiers` names none
(the same aliases the task loop uses for these tiers)."""


class DecisionStateError(Exception):
    """An unknown question, one not waiting for an answer, or an answer it cannot take."""


def _now() -> datetime:
    return datetime.now(UTC)


class RecordedAnswer(BaseModel):
    """What the decider subagent answered for one tier."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str = Field(description="The answer: one of the question's choices.")
    confidence: float = Field(ge=0, le=1, description="The subagent's confidence.")
    reason: str = Field(default="", description="Its short justification.")
    model: str = Field(description="The model the subagent was asked to run on.")
    answered_at: AwareDatetime = Field(default_factory=_now, description="When it answered.")


class PendingDecision(BaseModel):
    """One question `decide()` handed to the Claude Code session, and its answers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    question: Question = Field(description="The question.")
    fingerprint: str = Field(description="`question.fingerprint()` when it was queued.")
    tier: DecideTier = Field(description="The tier last asked: the one pending, if any.")
    model: str = Field(description="The model for `tier`.")
    status: Literal["pending", "answered"] = Field(description="Whether `tier` has answered.")
    answers: dict[DecideTier, RecordedAnswer] = Field(
        default_factory=dict, description="Recorded answers, per tier."
    )
    updated_at: AwareDatetime = Field(default_factory=_now, description="Last change.")


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class DecisionQueue:
    """Reads and writes the queued questions under a project's state directory."""

    def __init__(self, layout: StateLayout) -> None:
        self.layout = layout
        self.dir = layout.state_dir / "runtime" / "decisions"

    def _path(self, question_id: str) -> Path:
        return self.dir / f"{state_key(question_id)}.json"

    def get(self, question_id: str) -> PendingDecision | None:
        """The record for `question_id`, or `None` if it was never queued."""
        path = self._path(question_id)
        if not path.is_file():
            return None
        return PendingDecision.model_validate_json(path.read_text(encoding="utf-8"))

    def all(self) -> list[PendingDecision]:
        """Every record, sorted by question id."""
        if not self.dir.is_dir():
            return []
        records = [
            PendingDecision.model_validate_json(p.read_text(encoding="utf-8"))
            for p in self.dir.glob("*.json")
        ]
        return sorted(records, key=lambda r: r.question.id)

    def pending(self) -> list[PendingDecision]:
        """The records waiting for an answer."""
        return [r for r in self.all() if r.status == "pending"]

    def _put(self, record: PendingDecision) -> PendingDecision:
        record = record.model_copy(update={"updated_at": _now()})
        _write_atomic(self._path(record.question.id), record.model_dump_json(indent=2))
        return record

    def request(self, question: Question, tier: DecideTier, model: str) -> PendingDecision:
        """Queue `question` for `tier` (idempotent); a changed question starts afresh."""
        fingerprint = question.fingerprint()
        existing = self.get(question.id)
        answers = existing.answers if existing and existing.fingerprint == fingerprint else {}
        if (
            existing is not None
            and existing.fingerprint == fingerprint
            and existing.status == "pending"
            and existing.tier == tier
            and existing.model == model
        ):
            return existing
        return self._put(
            PendingDecision(
                question=question,
                fingerprint=fingerprint,
                tier=tier,
                model=model,
                status="pending",
                answers={t: a for t, a in answers.items() if t != tier},
            )
        )

    def answer(
        self, question_id: str, value: str, confidence: float, reason: str = ""
    ) -> PendingDecision:
        """Record the answer to the pending tier of `question_id`.

        Raises `DecisionStateError` for an unknown question, one not pending, a value
        that is not one of its choices, or a confidence outside 0..1.
        """
        record = self.get(question_id)
        if record is None:
            raise DecisionStateError(
                f"no queued question {question_id!r}; pending_decisions lists them"
            )
        if record.status != "pending":
            raise DecisionStateError(
                f"question {question_id!r} is not waiting for an answer (its {record.tier} "
                "tier already answered)"
            )
        choice = record.question.match(value)
        if choice is None:
            raise DecisionStateError(
                f"{value!r} is not a choice of question {question_id!r}; answer one of "
                f"{list(record.question.choices)}"
            )
        if not 0 <= confidence <= 1:
            raise DecisionStateError(f"confidence must be between 0 and 1, got {confidence}")
        answers = dict(record.answers)
        answers[record.tier] = RecordedAnswer(
            value=choice, confidence=confidence, reason=reason, model=record.model
        )
        return self._put(record.model_copy(update={"status": "answered", "answers": answers}))


class ClaudeCodeDecideBackend:
    """A `ModelBackend` for `decide()` that defers each question to the Claude Code session.

    `models.tiers` (the profile's) may name the model per tier; otherwise `haiku` for
    `small` and `opus` for `large`.
    """

    name = "claude-code"

    def __init__(self, layout: StateLayout, models: ModelsCfg | None = None) -> None:
        self.queue = DecisionQueue(layout)
        self.models = models if models is not None else ModelsCfg()

    def model_for(self, tier: DecideTier) -> str:
        """The subagent model for `tier`."""
        return self.models.tiers.get(tier) or DEFAULT_TIER_MODELS[tier]

    async def ask(self, question: Question, tier: DecideTier) -> ModelAnswer | Deferred:
        if "nda" in question.labels:
            raise NdaBlocked(
                f"question {question.id!r} is labeled 'nda' and runtime claude-code only "
                "has cloud models; 'nda' data only goes to a self-hosted model"
            )
        record = self.queue.get(question.id)
        if record is not None and record.fingerprint == question.fingerprint():
            answered = record.answers.get(tier)
            if answered is not None:
                return ModelAnswer(
                    value=answered.value,
                    confidence=answered.confidence,
                    model=answered.model,
                    reason=answered.reason,
                )
        model = self.model_for(tier)
        self.queue.request(question, tier, model)
        return Deferred(
            question_id=question.id,
            tier=tier,
            model=model,
            message=(
                f"queued for the {DECIDER_AGENT} subagent (model {model}): the plugin "
                "reads it with pending_decisions and records the answer with "
                "answer_decision; then ask again"
            ),
        )


# --- what the MCP tools do ---------------------------------------------------------------


def _entry(record: PendingDecision) -> dict[str, Any]:
    question = record.question
    return {
        "question_id": question.id,
        "tier": record.tier,
        "model": record.model,
        "agent": DECIDER_AGENT,
        "description": f"chipgraph decide {question.id}",
        "choices": list(question.choices),
        "prompt": question_prompt(question),
    }


def pending_decisions(layout: StateLayout) -> dict[str, Any]:
    """The `pending_decisions` MCP tool: every question waiting for the decider."""
    return {"decisions": [_entry(r) for r in DecisionQueue(layout).pending()]}


def answer_decision(
    layout: StateLayout, question_id: str, value: str, confidence: float, reason: str = ""
) -> dict[str, Any]:
    """The `answer_decision` MCP tool: record the decider's answer for a pending question."""
    record = DecisionQueue(layout).answer(question_id, value, confidence, reason)
    answer = record.answers[record.tier]
    return {
        "question_id": question_id,
        "tier": record.tier,
        "value": answer.value,
        "confidence": answer.confidence,
        "status": record.status,
    }


__all__ = [
    "DECIDER_AGENT",
    "DEFAULT_TIER_MODELS",
    "ClaudeCodeDecideBackend",
    "DecisionQueue",
    "DecisionStateError",
    "PendingDecision",
    "RecordedAnswer",
    "answer_decision",
    "pending_decisions",
]
