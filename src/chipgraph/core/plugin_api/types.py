"""Shared plugin types: in-process data passed to and from plugin protocols.

These models are not persisted (no `schema_version`) and are not part of the artifact
store; they only carry data between core and adapters/packs during a single run.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from chipgraph.core.contracts import Budget, DataLabel, RuleInstance

if TYPE_CHECKING:
    from chipgraph.core.plugin_api.protocols import Runner


class RunResult(BaseModel):
    """The outcome of running a subprocess command via a `Runner`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    returncode: int = Field(description="The process's exit code.")
    stdout: str = Field(description="Captured standard output.")
    stderr: str = Field(description="Captured standard error.")
    duration_s: float = Field(ge=0, description="How long the command took, in seconds.")
    timed_out: bool = Field(default=False, description="Whether the command was killed on timeout.")


class ToolContext(BaseModel):
    """Context a `ToolAdapter` or `Check` runs with: repo, runner, env, and free params."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    repo_root: Path = Field(description="Root of the repo (or workspace member) being worked on.")
    runner: Runner = Field(description="The subprocess runner to use for any command.")
    env: dict[str, str] = Field(default={}, description="Extra environment variables.")
    params: dict[str, str] = Field(
        default={}, description="Free-form parameters, e.g. block, module."
    )


class LlmMessage(BaseModel):
    """A single message in an LLM conversation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["system", "user", "assistant"] = Field(description="Who sent this message.")
    content: str = Field(description="The message text.")


class LlmRequest(BaseModel):
    """A request to complete a conversation with an LLM."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(description="The model id to use.")
    messages: tuple[LlmMessage, ...] = Field(min_length=1, description="The conversation so far.")
    max_tokens: int = Field(ge=1, description="Maximum number of tokens to generate.")
    labels: tuple[DataLabel, ...] = Field(
        default=(),
        description=(
            "Data labels of everything in the context. A provider must refuse a request "
            "carrying 'nda' unless it is local (see LlmProvider.local)."
        ),
    )


class LlmResponse(BaseModel):
    """The result of completing an LLM request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(description="The generated text.")
    input_tokens: int = Field(ge=0, description="Tokens consumed by the input.")
    output_tokens: int = Field(ge=0, description="Tokens consumed by the output.")
    model: str = Field(description="The model id that actually served the request.")


class AgentTask(BaseModel):
    """A single task handed to an `AgentRuntime`: what to do, and under what constraints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instance: RuleInstance = Field(description="The rule instance this task fulfils.")
    role: str = Field(description="The agent role that runs this task.")
    skills: tuple[str, ...] = Field(default=(), description="Skill ids available to the agent.")
    allowed_writes: tuple[str, ...] = Field(
        min_length=1, description="Repo-relative paths the agent is allowed to write."
    )
    context: dict[str, str] = Field(default={}, description="Free-form context for the agent.")
    budget: Budget = Field(description="Resource budget for this task.")
