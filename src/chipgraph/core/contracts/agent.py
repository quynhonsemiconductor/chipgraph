"""Agent results and decisions: the output of AI agent rules and their sub-decisions."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentResult(BaseModel):
    """The outcome of running an agent rule."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    status: Literal["done", "failed", "needs_human", "budget_exhausted"] = Field(
        description="The final status of the agent run."
    )
    files_written: tuple[str, ...] = Field(
        default=(), description="Paths of files the agent wrote."
    )
    assumptions: tuple[str, ...] = Field(
        default=(), description="Assumptions the agent made while working."
    )
    open_questions: tuple[str, ...] = Field(
        default=(), description="Questions the agent could not resolve on its own."
    )
    tokens: int | None = Field(default=None, ge=0, description="Tokens consumed by the agent run.")
    cost: float | None = Field(default=None, ge=0, description="Cost of the agent run, in USD.")

    @model_validator(mode="after")
    def _check_needs_human_has_question(self) -> Self:
        if self.status == "needs_human" and len(self.open_questions) < 1:
            raise ValueError("status='needs_human' requires at least one open question")
        return self


class Decision(BaseModel):
    """A single resolved decision, made by a rule, a small model, or a large model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    question_id: str = Field(description="The id of the question this decision answers.")
    value: str | int | float | bool = Field(description="The decided value.")
    confidence: float = Field(ge=0, le=1, description="Confidence in this decision, from 0 to 1.")
    backend: Literal["rule", "small", "large"] = Field(
        description="What made this decision: a rule, a small model, or a large model."
    )
