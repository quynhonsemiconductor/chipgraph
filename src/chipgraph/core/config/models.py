"""The `Profile` model: a project's layered configuration, and the personal `UserConfig`.

See DESIGN.md 8.3, 8.4 and 8.6: a project is configured by exactly one `.chipgraph.yml`,
which may `extend` organization rules, presets and shared git rule repos; a personal
`~/.config/chipgraph/user.yml` may only adjust individual experience, never artifact
conventions (layout, naming, templates, checks).
"""

from __future__ import annotations

import re
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from chipgraph.core.contracts.types import DataLabel, ModelTier

Level = Literal["L1", "L2", "L3", "L4", "L5"]
"""An autonomy level from L1 (human does everything) to L5 (never allowed, see DESIGN 8.3)."""


def _reject_level_5(autonomy: dict[str, Level]) -> None:
    for area, level in autonomy.items():
        if level == "L5":
            raise ValueError(f"autonomy[{area!r}] = 'L5' is never allowed")


class SourceCfg(BaseModel):
    """A single spec source: a file path and its format."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(description="Path to the source file, relative to the project root.")
    format: str = Field(description="Format of the source, e.g. 'qsoc-contract'.")


_DEFAULT_REQ_ID_PATTERN = r"REQ-[A-Z][A-Z0-9_]*-\d+"


class RequirementsCfg(BaseModel):
    """How requirement IDs are found in text specs (DECISIONS D37).

    By default REQ-IDs are declared in the spec and matched by `id_pattern`. The temporary
    `infer: verification` mode also treats each numbered item of a spec's "Verification"
    section as a requirement, keyed by block and content hash rather than by its number.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id_pattern: str = Field(
        default=_DEFAULT_REQ_ID_PATTERN,
        description=(
            "Regex a declared REQ-ID must match (whole ID). '{block}' and '{BLOCK}' are "
            "replaced by the block name, lower- and upper-case, before compiling."
        ),
    )
    infer: Literal["off", "verification"] = Field(
        default="off",
        description=(
            "Temporary mode: 'verification' infers one requirement per numbered item of the "
            "spec section titled `infer_heading`, for specs without REQ-IDs."
        ),
    )
    infer_heading: str = Field(
        default="Verification",
        description="Title of the section items are inferred from, without its number.",
    )

    @field_validator("id_pattern")
    @classmethod
    def _pattern_compiles(cls, value: str) -> str:
        probe = value.replace("{block}", "x").replace("{BLOCK}", "X")
        try:
            re.compile(probe)
        except re.error as exc:
            raise ValueError(f"id_pattern is not a valid regex: {exc}") from exc
        return value

    def id_regex(self, block: str | None = None) -> re.Pattern[str]:
        """The compiled `id_pattern`, with `{block}`/`{BLOCK}` filled in for `block`."""
        pattern = self.id_pattern
        if block is not None:
            pattern = pattern.replace("{block}", re.escape(block.lower()))
            pattern = pattern.replace("{BLOCK}", re.escape(block.upper()))
        elif "{block}" in pattern or "{BLOCK}" in pattern:
            raise ValueError("id_pattern uses '{block}'/'{BLOCK}'; a block name is required")
        return re.compile(pattern)


class SpecCfg(BaseModel):
    """Where the chip-level spec and per-IP specs live."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chip: SourceCfg | None = Field(default=None, description="The chip-level spec source.")
    ip_dir: str | None = Field(default=None, description="Directory containing per-IP specs.")
    requirements: RequirementsCfg = Field(
        default_factory=RequirementsCfg,
        description="How requirement IDs are found in text specs (D37).",
    )


class AdapterCfg(BaseModel):
    """An adapter selection for a capability; extra keys are the adapter's own options."""

    model_config = ConfigDict(extra="allow", frozen=True)

    use: str = Field(description="The adapter implementation to use, e.g. 'cmd', 'edalize'.")


class NamingCfg(BaseModel):
    """The project's naming rules and the checker that enforces them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rules: str | None = Field(
        default=None, description="Reference to naming rules, e.g. 'org:qnsc/naming-v1.yml'."
    )
    checker: AdapterCfg | None = Field(default=None, description="Adapter that checks naming.")


class TargetCfg(BaseModel):
    """What kind of thing is being built, and on which process."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["asic", "fpga", "ip"] = Field(
        default="asic", description="The kind of target being built."
    )
    pdk: str | None = Field(default=None, description="The process design kit, if any.")


class ModelsCfg(BaseModel):
    """LLM providers and the model assigned to each tier."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    providers: dict[str, dict[str, str]] = Field(
        default_factory=dict, description="LLM provider configuration by provider name."
    )
    tiers: dict[ModelTier, str] = Field(
        default_factory=dict, description="Model id used for each capability/cost tier."
    )
    alt: dict[ModelTier, str] = Field(
        default_factory=dict, description="Alternate model or provider per tier (see DESIGN D24)."
    )


class DataCfg(BaseModel):
    """Default data sensitivity and how NDA content is isolated."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    default: DataLabel = Field(default="internal", description="Default data sensitivity label.")
    nda_paths: tuple[str, ...] = Field(
        default=(), description="Glob patterns of paths labeled 'nda'."
    )
    nda_model: Literal["block", "local"] = Field(
        default="block", description="How NDA content is kept away from cloud models."
    )


class StateCfg(BaseModel):
    """Where run state (journal, cache, trace) is kept."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: str = Field(
        default="local",
        description="State backend: 'local', 'branch:<name>', or 'repo:<url>'.",
    )


class DecisionsCfg(BaseModel):
    """Where gate approvals and waivers are kept."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    store: Literal["repo", "state"] = Field(
        default="repo", description="Where gate/waiver decisions are stored."
    )


class PathRule(BaseModel):
    """An exception for one path glob: checks toggled off, writes denied, and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checks: dict[str, Literal["on", "off"]] = Field(
        default_factory=dict, description="Per-check on/off override for this path."
    )
    write: Literal["allow", "deny"] = Field(
        default="allow", description="Whether agents may write files under this path."
    )
    reason: str | None = Field(
        default=None,
        description="Why this exception exists; required when a check is off or write is denied.",
    )

    @field_validator("checks", mode="before")
    @classmethod
    def _yaml_booleans_to_on_off(cls, value: object) -> object:
        # YAML 1.1 (PyYAML) reads an unquoted `off` / `on` as a boolean. Accept both
        # spellings, so `checks: { naming: off }` works as the examples in DESIGN.md show.
        if isinstance(value, dict):
            return {
                k: ("on" if v is True else "off" if v is False else v) for k, v in value.items()
            }
        return value

    @model_validator(mode="after")
    def _require_reason_when_relaxed(self) -> Self:
        relaxed = self.write == "deny" or any(v == "off" for v in self.checks.values())
        if relaxed and not self.reason:
            raise ValueError(
                "a PathRule that turns a check off or denies write must give a 'reason'"
            )
        return self


class BlockOverride(BaseModel):
    """Per-block overrides: only adapters, layout, autonomy, paths and instances may be set.

    `instances` names the memory-map instances of this block, treated as an IP (D38):
    e.g. `blocks.timer.instances: [timer_0, timer_1]`. Each name is a single model-key
    part (non-empty, no ':' or '.') and must be unique within the block. With no
    `instances`, the block matches only a same-named instance in the chip contract.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapters: dict[str, AdapterCfg] = Field(
        default_factory=dict, description="Per-block adapter overrides."
    )
    layout: dict[str, str | tuple[str, ...]] = Field(
        default_factory=dict, description="Per-block layout overrides."
    )
    autonomy: dict[str, Level] = Field(
        default_factory=dict, description="Per-block autonomy overrides."
    )
    paths: dict[str, PathRule] = Field(
        default_factory=dict, description="Per-block path rule overrides."
    )
    instances: tuple[str, ...] = Field(
        default=(),
        description=(
            "Memory-map instances of this block-as-IP (D38); each a valid model-key part "
            "(non-empty, no ':' or '.'), unique within the block."
        ),
    )

    @field_validator("instances")
    @classmethod
    def _check_instances(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        seen: set[str] = set()
        for name in value:
            if not name:
                raise ValueError("an instance name must not be empty")
            if ":" in name or "." in name:
                raise ValueError(f"instance name {name!r} must not contain ':' or '.'")
            if name in seen:
                raise ValueError(f"instance {name!r} is listed more than once")
            seen.add(name)
        return value

    @model_validator(mode="after")
    def _check_autonomy(self) -> Self:
        _reject_level_5(dict(self.autonomy))
        return self


class TemplatesCfg(BaseModel):
    """Per-block-name template overrides (DESIGN 8.6 V2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    override: dict[str, str] = Field(
        default_factory=dict, description="Template block name to override file path."
    )


class StyleCfg(BaseModel):
    """Code style exemplars and the formatter run after generation (DESIGN 8.6 V3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    exemplars: tuple[str, ...] = Field(
        default=(), description="Paths to files whose style agents should imitate."
    )
    formatter: AdapterCfg | None = Field(
        default=None, description="Adapter that formats generated code after it is written."
    )


class EnvCfg(BaseModel):
    """Environment modules and variables needed to run project tools (DESIGN 12.6)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    modules: tuple[str, ...] = Field(
        default=(), description="Environment Modules/Lmod modules to load."
    )
    vars: dict[str, str] = Field(
        default_factory=dict, description="Environment variables to set, e.g. license servers."
    )


class PolicyCfg(BaseModel):
    """Organization/project policy switches (DESIGN 8.6 layer authority table).

    `local_plugins` is tighten-only across layers: an outer layer (e.g. the organization)
    may set `deny` and no later layer may re-`allow` it (enforced in the loader).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    local_plugins: Literal["allow", "deny"] = Field(
        default="allow",
        description="Whether the project may load local plugins from `.chipgraph/plugins/`.",
    )


class Profile(BaseModel):
    """A project's full, merged configuration (DESIGN 8.3-8.4, 8.6, 12.6)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    project: str = Field(description="The project's name.")
    extends: tuple[str, ...] = Field(
        default=(),
        description="Profiles this one extends: 'org:', 'preset:', 'path:', or 'git+'.",
    )
    packs: tuple[str, ...] = Field(default=(), description="Domain packs used by this project.")
    spec: SpecCfg = Field(
        default_factory=SpecCfg, description="Where the chip-level and per-IP specs live."
    )
    adapters: dict[str, AdapterCfg] = Field(
        default_factory=dict, description="Capability name (e.g. 'lint', 'sim') to adapter config."
    )
    naming: NamingCfg = Field(default_factory=NamingCfg, description="Naming rules and checker.")
    layout: dict[str, str | tuple[str, ...]] = Field(
        default_factory=dict, description="Artifact kind to output path template(s)."
    )
    target: TargetCfg = Field(default_factory=TargetCfg, description="What is being built.")
    autonomy: dict[str, Level] = Field(
        default_factory=dict, description="Autonomy level per area, e.g. 'spec', 'rtl', 'verify'."
    )
    models: ModelsCfg = Field(
        default_factory=ModelsCfg, description="LLM providers and tier assignments."
    )
    runtime: Literal["claude-code", "claude-agent-sdk", "generic"] = Field(
        default="claude-code", description="Which agent runtime executes agent rules."
    )
    data: DataCfg = Field(
        default_factory=DataCfg, description="Data sensitivity defaults and NDA handling."
    )
    reviewers: tuple[str, ...] = Field(default=(), description="Default reviewers for gates.")
    state: StateCfg = Field(default_factory=StateCfg, description="Where run state is kept.")
    decisions: DecisionsCfg = Field(
        default_factory=DecisionsCfg, description="Where gate/waiver decisions are kept."
    )
    paths: dict[str, PathRule] = Field(
        default_factory=dict, description="Glob pattern to path-specific exception rule."
    )
    blocks: dict[str, BlockOverride] = Field(
        default_factory=dict, description="Per-block overrides of adapters/layout/autonomy/paths."
    )
    templates: TemplatesCfg = Field(
        default_factory=TemplatesCfg, description="Template block overrides."
    )
    style: StyleCfg = Field(
        default_factory=StyleCfg, description="Code style exemplars and formatter."
    )
    plugins: tuple[str, ...] = Field(
        default=(), description="Local plugin script paths (DESIGN 8.6 V5)."
    )
    policy: PolicyCfg = Field(
        default_factory=PolicyCfg, description="Organization/project policy switches (DESIGN 8.6)."
    )
    workspace: str | None = Field(
        default=None, description="Path to a workspace manifest, if part of one (DESIGN 8.7)."
    )
    offline: bool = Field(
        default=False, description="Disable all outbound network use (DESIGN 12.6)."
    )
    env: EnvCfg = Field(
        default_factory=EnvCfg, description="Environment modules and variables needed by tools."
    )
    vcs: AdapterCfg | None = Field(
        default=None, description="Version control adapter, if not plain git."
    )
    review: AdapterCfg | None = Field(
        default=None, description="Review/gate adapter, if not GitHub PRs."
    )
    runner: AdapterCfg | None = Field(
        default=None, description="Job runner adapter, e.g. 'ssh', 'lsf', 'slurm'."
    )
    workflow_overrides: dict[str, dict[str, object]] = Field(
        default_factory=dict, description="Per-workflow field overrides."
    )
    freeze: dict[str, str] = Field(
        default_factory=dict, description="Pinned versions for reproducibility."
    )

    @model_validator(mode="after")
    def _check_autonomy(self) -> Self:
        _reject_level_5(dict(self.autonomy))
        return self


_USER_ALLOWED_KEYS = frozenset(
    {"schema_version", "autonomy", "notify", "answer_language", "prefer_models", "editor"}
)


class UserConfig(BaseModel):
    """A personal layer (`~/.config/chipgraph/user.yml`): experience only, never conventions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    autonomy: dict[str, Level] = Field(
        default_factory=dict,
        description="Personal autonomy override per area; may only lower the project's level.",
    )
    notify: str | None = Field(default=None, description="Notification channel, e.g. 'slack'.")
    answer_language: str | None = Field(
        default=None, description="Preferred language for agent answers."
    )
    prefer_models: dict[ModelTier, str] = Field(
        default_factory=dict,
        description="Preferred model per tier; must be one the project allows.",
    )
    editor: str | None = Field(default=None, description="Preferred editor to open files in.")

    @model_validator(mode="before")
    @classmethod
    def _reject_project_conventions(cls, data: Any) -> Any:
        if isinstance(data, dict):
            unknown = sorted(set(data) - _USER_ALLOWED_KEYS)
            if unknown:
                raise ValueError(
                    f"{unknown[0]!r} is a project artifact convention (naming, layout, "
                    "template, check, ...); the user layer may not change project "
                    "conventions (DESIGN 8.6), only personal experience settings"
                )
        return data

    @model_validator(mode="after")
    def _check_autonomy(self) -> Self:
        _reject_level_5(dict(self.autonomy))
        return self
