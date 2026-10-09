"""`RoleSpec`: one agent role as data (DESIGN.md 5.1, D8).

A role is a system prompt plus a **tool table**: the abstract capabilities it is granted,
what it may write, what it may read, and its model tier with the tier it escalates to.
An agent runtime maps the capabilities to its own tools (the runtime `claude-code`
renders them into its subagent definitions); the engine and the runtime's guard enforce
the write scope and the read policy.

A role file is markdown with a YAML frontmatter (the fields below, without `prompt`);
the body is the prompt. The prompt names tools only through placeholders,
`{tool:<capability>}`, which the runtime replaces with its own tool names, so this data
stays free of any harness's tool names.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from chipgraph.core.contracts import ArtifactKind, ModelTier

Capability = Literal[
    "read_files",
    "search_files",
    "write_outputs",
    "engine_context",
    "engine_decisions",
]
"""An abstract tool capability a role may be granted.

- `read_files`: read a project file by path.
- `search_files`: list files by pattern and search their content.
- `write_outputs`: create and edit files (only the task's outputs: the guard enforces it).
- `engine_context`: ask the engine for the task's context (`get_context`).
- `engine_decisions`: read the engine's pending multiple-choice questions.
"""

CAPABILITIES: tuple[Capability, ...] = (
    "read_files",
    "search_files",
    "write_outputs",
    "engine_context",
    "engine_decisions",
)
"""Every capability, in the order runtimes list their tools."""

WriteScope = Literal["outputs", "plan", "proposal", "none"]
"""What a role may write: its rule's outputs, only its plan file, only its proposal file,
or nothing. `plan` and `proposal` rules have exactly one output (the rule validator checks
it); every write is still bounded by the task's outputs."""

RoleId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")]
"""A role id: lower case words joined by `-`, e.g. `author`, `tb-author`."""

_PLACEHOLDER_RE = re.compile(r"\{tool:([a-z_]+)\}")

_TIER_ORDER: dict[ModelTier, int] = {"small": 0, "medium": 1, "large": 2}


class RoleError(Exception):
    """An unknown role, a broken role file, or a rule that breaks its role's tool table."""


class ReadPolicy(BaseModel):
    """What a role may read with its own tools (the engine's context is checked apart)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["any", "deny"] = Field(
        default="any",
        description="'any': no read restriction; 'deny': never read the kinds and globs below.",
    )
    deny_kinds: tuple[ArtifactKind, ...] = Field(
        default=(),
        description=(
            "Artifact kinds the role must not see: every artifact of these kinds in the "
            "build graph, and the profile's layout paths for them."
        ),
    )
    deny_globs: tuple[str, ...] = Field(
        default=(), description="Extra repo-relative path globs the role must not read."
    )

    @model_validator(mode="after")
    def _check_mode(self) -> Self:
        denies = bool(self.deny_kinds or self.deny_globs)
        if self.mode == "any" and denies:
            raise ValueError("read_policy mode 'any' takes no deny_kinds or deny_globs")
        if self.mode == "deny" and not denies:
            raise ValueError("read_policy mode 'deny' needs deny_kinds or deny_globs")
        return self


class RoleSpec(BaseModel):
    """One agent role: its prompt and its tool table (DESIGN.md 5.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    id: RoleId = Field(description="The role id rules name in `role:`, e.g. 'author'.")
    description: str = Field(
        min_length=1,
        description="One line: what the role does and how it is started. No line breaks.",
    )
    prompt: str = Field(
        min_length=1,
        description="The role's system prompt; names tools only as {tool:<capability>}.",
    )
    default_tier: ModelTier = Field(description="Model tier of the first attempt.")
    escalate_to: ModelTier | None = Field(
        default=None, description="Model tier of later attempts, after a rejected one."
    )
    tools: tuple[Capability, ...] = Field(
        description="Abstract capabilities granted; the runtime maps them to its tools."
    )
    write_scope: WriteScope = Field(description="What the role may write.")
    read_policy: ReadPolicy = Field(
        default_factory=ReadPolicy, description="What the role may read with its own tools."
    )
    shell: Literal[False] = Field(
        default=False, description="Roles never get a shell (the guard refuses one)."
    )
    dispatch: bool = Field(
        default=True,
        description=(
            "Whether agent rules may use this role (handed out as tasks); false for a role "
            "served by another path, such as the fast decision layer."
        ),
    )

    @model_validator(mode="after")
    def _check_table(self) -> Self:
        if "\n" in self.description:
            raise ValueError("description must be one line")
        if len(set(self.tools)) != len(self.tools):
            raise ValueError("tools lists a capability twice")
        if self.escalate_to is not None and (
            _TIER_ORDER[self.escalate_to] <= _TIER_ORDER[self.default_tier]
        ):
            raise ValueError(
                f"escalate_to {self.escalate_to!r} must be higher than "
                f"default_tier {self.default_tier!r}"
            )
        writes = "write_outputs" in self.tools
        if writes != (self.write_scope != "none"):
            raise ValueError("write_outputs is granted exactly when write_scope is not 'none'")
        for name in _PLACEHOLDER_RE.findall(self.prompt):
            if name not in self.tools:
                raise ValueError(
                    f"prompt names {{tool:{name}}}, a capability the role is not granted"
                )
        return self

    @property
    def can_read_files(self) -> bool:
        """Whether the role has its own file-reading or file-search tools."""
        return "read_files" in self.tools or "search_files" in self.tools

    def render_prompt(self, tool_names: dict[str, tuple[str, ...]]) -> str:
        """The prompt with each `{tool:<capability>}` replaced by the runtime's tool names."""

        def _replace(match: re.Match[str]) -> str:
            names = tool_names.get(match.group(1))
            if not names:
                raise RoleError(f"role {self.id!r}: no tool for capability {match.group(1)!r}")
            return ", ".join(f"`{name}`" for name in names)

        return _PLACEHOLDER_RE.sub(_replace, self.prompt)


def split_frontmatter(text: str, *, source: str) -> tuple[dict[str, object], str]:
    """Split a markdown file into its YAML frontmatter mapping and its body."""
    if not text.startswith("---\n"):
        raise ValueError(f"{source}: the frontmatter must open with '---' on the first line")
    head, sep, body = text[4:].partition("\n---\n")
    if not sep:
        raise ValueError(f"{source}: the frontmatter is not closed with '---'")
    data = yaml.safe_load(head)
    if not isinstance(data, dict):
        raise ValueError(f"{source}: the frontmatter is not a mapping")
    text = body.strip("\n")
    return {str(k): v for k, v in data.items()}, (text + "\n") if text.strip() else ""


def parse_role_file(path: Path) -> RoleSpec:
    """Load one role file (frontmatter + prompt body). Raises `RoleError`."""
    try:
        data, body = split_frontmatter(path.read_text(encoding="utf-8"), source=str(path))
        if "prompt" in data:
            raise ValueError(f"{path}: the prompt is the file's body, not a frontmatter key")
        role = RoleSpec.model_validate({**data, "prompt": body})
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
        raise RoleError(f"invalid role file {path}: {exc}") from exc
    if role.id != path.stem:
        raise RoleError(f"role file {path} defines role {role.id!r}; name it {role.id}.md")
    return role


__all__ = [
    "CAPABILITIES",
    "Capability",
    "ReadPolicy",
    "RoleError",
    "RoleId",
    "RoleSpec",
    "WriteScope",
    "parse_role_file",
    "split_frontmatter",
]
