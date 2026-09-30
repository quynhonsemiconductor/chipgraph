"""The `/ask` data contracts: retrieved sources, the answer, and the citation check.

An answer is `{"answer": "...", "citations": ["file:line" | "model:<key>", ...],
"unknown": bool}` (`AskAnswer`). `ask_check` verifies it and returns an `AskCheck`: the
verified answer, or the reasons it was rejected. Every model is JSON-dumpable, frozen, and
carries a `schema_version`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

UNKNOWN_ANSWER = "I don't know: no source in this project's Design Model or documents answers it."
"""The answer text used when there is no source, or no answer could be verified."""


class DocLine(BaseModel):
    """One line of an indexed document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    line: int = Field(ge=1, description="1-based line number.")
    text: str = Field(description="The line's text.")


class AskSource(BaseModel):
    """One source `ask_context` returned: a model entity or a document line."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    citation: str = Field(description="What to cite: 'model:<key>' or 'path:line'.")
    kind: Literal["model", "document"] = Field(description="A model entity or a document line.")
    text: str = Field(description="The entity's facts, or the document line.")
    origin: Literal["lookup", "search"] = Field(
        description="'lookup': a typed model query on a name in the question; 'search': FTS5."
    )
    defined_at: str | None = Field(
        default=None,
        description="For a model entity: the indexed 'path:line' it was read from, also citable.",
    )
    context: tuple[DocLine, ...] = Field(
        default=(), description="For a document line: the lines around it."
    )


class AskContext(BaseModel):
    """The result of `ask_context`: the sources an answer may use, and only those."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    question: str = Field(description="The question, as asked.")
    query: str | None = Field(description="The FTS5 query used, or None when no term was left.")
    sources: tuple[AskSource, ...] = Field(default=(), description="Best first.")
    no_sources: bool = Field(description="True when nothing matched: the answer is 'unknown'.")
    note: str = Field(description="How to use the sources.")


class AskAnswer(BaseModel):
    """An answer to one `/ask` question: text, citations, and whether it is 'unknown'."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    answer: str = Field(description="The answer text.")
    citations: tuple[str, ...] = Field(
        default=(), description="'path:line' (or 'path:start-end') or 'model:<key>'."
    )
    unknown: bool = Field(
        default=False, description="True when the sources do not answer the question."
    )


class CitationCheck(BaseModel):
    """The verdict on one citation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    citation: str = Field(description="The citation as given.")
    valid: bool = Field(description="True when it names an indexed line or a model key.")
    kind: Literal["model", "document"] | None = Field(
        default=None, description="What it names, when that could be told."
    )
    normalized: str | None = Field(
        default=None, description="The canonical form: 'path:line[-end]' or 'model:<key>'."
    )
    text: str | None = Field(default=None, description="What it points at, when valid.")
    reason: str | None = Field(default=None, description="Why it is invalid.")


class AskCheck(BaseModel):
    """The result of `ask_check`: the verified answer (when `ok`) or the reasons."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    ok: bool = Field(description="True when the answer is accepted.")
    answer: AskAnswer | None = Field(
        default=None, description="The verified answer (canonical citations), when ok."
    )
    citations: tuple[CitationCheck, ...] = Field(default=(), description="One per citation.")
    reasons: tuple[str, ...] = Field(default=(), description="Why it was rejected.")


__all__ = [
    "UNKNOWN_ANSWER",
    "AskAnswer",
    "AskCheck",
    "AskContext",
    "AskSource",
    "CitationCheck",
    "DocLine",
]
