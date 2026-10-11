"""Named targets: a pack-provided set of graph selectors, e.g. `rtl:<block>` (M2-11a).

A target is what a person asks for in one word, `chipgraph build rtl:timer`, instead of
naming every rule. A pack ships its targets as YAML files in its `targets/` directory,
one per file::

    schema_version: 1
    target: rtl                     # the name: `rtl:<params>`
    description: The RTL of one block, from its plan to a pull request.
    params:
      - { name: block, description: A block of the profile. }
    select:                         # graph selectors, `{param}` filled in
      - select: "pack/plan_expand[block={block}]"
      - select: "pack/pr_gate[block={block}]"
        optional: true              # may match nothing (yet)

`BuildGraph.select("rtl:timer")` (with a `TargetRegistry`) is the union of what each
selector selects, each with its upstream instances, exactly as `select` of a rule id.
A required selector that matches nothing is an error (an unknown param value, a pack
that is off); an optional one is skipped. A selector is a rule selector only
(`pack/name[k=v]`), never another target.

Params are given in order after the colon, `rtl:timer`, or by name, `rtl:block=timer`,
comma-separated. This module knows no pack, rule or project: only names and templates.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from chipgraph.core.plugin_api.pack import Pack

TARGETS_DIR = "targets"
"""The directory of a pack that holds its target files."""

_NAME_RE = r"^[a-z][a-z0-9_-]*$"
_VALUE_RE = re.compile(r"^[^\s\[\],=:]+$")
_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


class TargetError(ValueError):
    """A target file is invalid, or a target is unknown or given the wrong params."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TargetParam(_Model):
    """One param of a target."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", description="The param name.")
    description: str = Field(default="", description="What the param is.")


class TargetSelector(_Model):
    """One graph selector of a target."""

    select: str = Field(description="A rule selector, `pack/name[k={param}]`.")
    optional: bool = Field(
        default=False, description="It may match no instance (it is skipped then)."
    )

    @field_validator("select")
    @classmethod
    def _rule_selector(cls, value: str) -> str:
        if "/" not in value.partition("[")[0]:
            raise ValueError(f"a target selector names a rule ('pack/name[...]'), got {value!r}")
        return value


class TargetSpec(_Model):
    """A named target: its params and selectors."""

    schema_version: Literal[1] = Field(default=1, description="Schema version of this file.")
    name: str = Field(alias="target", pattern=_NAME_RE, description="The target's name.")
    description: str = Field(default="", description="What building the target does.")
    params: tuple[TargetParam, ...] = Field(default=(), description="The params, in order.")
    select: tuple[TargetSelector, ...] = Field(min_length=1, description="The selectors.")
    pack: str | None = Field(default=None, description="The pack that provides it.")

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    def selectors(self, params: Mapping[str, str]) -> tuple[TargetSelector, ...]:
        """The selectors with `params` filled in."""
        return tuple(
            item.model_copy(update={"select": _fill(item.select, params, self.name)})
            for item in self.select
        )


def _fill(template: str, params: Mapping[str, str], name: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in params:
            raise TargetError(f"target {name!r}: selector {template!r} uses unknown {{{key}}}")
        return params[key]

    return _PLACEHOLDER_RE.sub(_replace, template)


class TargetRegistry:
    """The targets of a project's packs, by name."""

    def __init__(self, specs: Iterable[TargetSpec] = ()) -> None:
        self._specs: dict[str, TargetSpec] = {}
        for spec in specs:
            self.add(spec)

    def add(self, spec: TargetSpec) -> None:
        """Register `spec`; a second target of the same name is an error."""
        other = self._specs.get(spec.name)
        if other is not None:
            raise TargetError(
                f"target {spec.name!r} is defined twice (packs {other.pack!r} and {spec.pack!r})"
            )
        for item in spec.select:
            for key in _PLACEHOLDER_RE.findall(item.select):
                if key not in {p.name for p in spec.params}:
                    raise TargetError(
                        f"target {spec.name!r}: selector {item.select!r} uses {{{key}}}, "
                        "which is not one of its params"
                    )
        self._specs[spec.name] = spec

    def names(self) -> tuple[str, ...]:
        """Every target name, sorted."""
        return tuple(sorted(self._specs))

    def get(self, name: str) -> TargetSpec | None:
        """The target called `name`, if any."""
        return self._specs.get(name)

    def is_target(self, text: str) -> bool:
        """Whether `text` names a target: `<name>` or `<name>:<params>` of a known name."""
        name = text.partition(":")[0]
        return "/" not in name and "[" not in name and name in self._specs

    def parse(self, text: str) -> tuple[TargetSpec, dict[str, str]]:
        """The target `text` names and its params. Raises `TargetError`."""
        name, _, rest = text.partition(":")
        spec = self._specs.get(name)
        if spec is None:
            known = ", ".join(self.names()) or "none"
            raise TargetError(f"unknown target {name!r}; known targets: {known}")
        wanted = [p.name for p in spec.params]
        values: dict[str, str] = {}
        parts = [p.strip() for p in rest.split(",")] if rest else []
        for index, part in enumerate(parts):
            key, sep, value = part.partition("=")
            if not sep:
                if index >= len(wanted):
                    raise TargetError(f"target {name!r} takes {len(wanted)} param(s): {text!r}")
                key, value = wanted[index], part
            key, value = key.strip(), value.strip()
            if key not in wanted:
                raise TargetError(f"target {name!r} has no param {key!r} (params: {wanted})")
            if key in values:
                raise TargetError(f"target {name!r}: param {key!r} given twice in {text!r}")
            if not _VALUE_RE.match(value):
                raise TargetError(f"target {name!r}: invalid value {value!r} for {key!r}")
            values[key] = value
        missing = [k for k in wanted if k not in values]
        if missing:
            usage = f"{name}:" + ",".join(f"<{k}>" for k in wanted)
            raise TargetError(f"target {name!r} needs {', '.join(missing)}: write {usage!r}")
        return spec, values

    def resolve(self, text: str) -> tuple[TargetSelector, ...]:
        """The selectors of the target `text`, params filled in. Raises `TargetError`."""
        spec, values = self.parse(text)
        return spec.selectors(values)

    def describe_params(self, text: str) -> str:
        """`k='v', ...` of the target `text`'s params, for an error message."""
        try:
            _, values = self.parse(text)
        except TargetError:
            return text
        return ", ".join(f"{k}={v!r}" for k, v in values.items())


def load_target_file(path: Path, *, pack: str | None = None) -> TargetSpec:
    """Load one target file. Raises `TargetError` naming `path`."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise TargetError(f"cannot read target file {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise TargetError(f"target file {path} must contain a YAML mapping")
    try:
        spec = TargetSpec.model_validate({**raw, "pack": pack})
    except ValidationError as exc:
        raise TargetError(f"invalid target file {path}: {exc}") from exc
    return spec


def load_pack_targets(pack: Pack) -> list[TargetSpec]:
    """Every target in `<pack>/targets/*.yml` (none when the directory is absent)."""
    directory = pack.root / TARGETS_DIR
    if not directory.is_dir():
        return []
    files = sorted({*directory.glob("*.yml"), *directory.glob("*.yaml")})
    return [load_target_file(path, pack=pack.manifest.name) for path in files]


__all__ = [
    "TARGETS_DIR",
    "TargetError",
    "TargetParam",
    "TargetRegistry",
    "TargetSelector",
    "TargetSpec",
    "load_pack_targets",
    "load_target_file",
]
