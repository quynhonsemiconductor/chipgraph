"""Checks: deterministic verification of artifacts, and their specs and results."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chipgraph.core.contracts.types import CheckStatus, Severity

_LOG_TAIL_MAX_CHARS = 4000
_TRUNCATION_MARKER = "…"


class CheckSpec(BaseModel):
    """The specification of a check: which adapter runs it and with what arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    id: str = Field(description="The id of this check.")
    capability: str = Field(description="The capability this check exercises, e.g. lint, sim.")
    adapter: str = Field(description="The adapter id that implements this check.")
    args: dict[str, Any] = Field(default={}, description="Arguments passed to the adapter.")


class Issue(BaseModel):
    """A single issue found by a check, such as a lint violation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file: str | None = Field(default=None, description="The file this issue was found in, if any.")
    line: int | None = Field(default=None, ge=1, description="The 1-based line number, if any.")
    rule: str = Field(default="", description="The specific rule within the check that fired.")
    severity: Severity = Field(default="error", description="How serious this issue is.")
    msg: str = Field(description="A human-readable message describing the issue.")


class CheckResult(BaseModel):
    """The outcome of running a check."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    check_id: str = Field(description="The id of the check that was run.")
    status: CheckStatus = Field(description="The overall outcome of the check.")
    issues: tuple[Issue, ...] = Field(
        default=(), description="Issues found while running the check."
    )
    log_tail: str = Field(
        default="", description="The tail of the check's log output, truncated to 4000 characters."
    )
    duration_s: float = Field(ge=0, description="How long the check took to run, in seconds.")
    idempotency_key: str = Field(
        description="A key identifying repeat runs of the same check invocation."
    )

    @property
    def ok(self) -> bool:
        """Whether this result counts as acceptable: pass or skipped."""
        return self.status in ("pass", "skipped")

    @model_validator(mode="after")
    def _truncate_log_tail(self) -> Self:
        if len(self.log_tail) > _LOG_TAIL_MAX_CHARS:
            keep = _LOG_TAIL_MAX_CHARS - len(_TRUNCATION_MARKER)
            object.__setattr__(self, "log_tail", _TRUNCATION_MARKER + self.log_tail[-keep:])
        return self

    @model_validator(mode="after")
    def _check_pass_has_no_errors(self) -> Self:
        if self.status == "pass" and any(issue.severity == "error" for issue in self.issues):
            raise ValueError(
                "CheckResult with status='pass' must not have any issue of severity 'error'"
            )
        return self
