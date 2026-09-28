"""Packs: domain bundles of rules, roles, skills, checks, templates and commands.

A pack is a directory containing a `pack.yml` manifest. Core loads and validates the
manifest and checks that every path it advertises actually exists; it does not interpret
rules, skills or templates itself (that is packs' own concern, or later tasks').
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from chipgraph.core.plugin_api.registry import PluginError

_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
_VERSION_PATTERN = r"^\d+\.\d+\.\d+$"

PackName = Annotated[str, StringConstraints(pattern=_NAME_PATTERN)]
PackVersion = Annotated[str, StringConstraints(pattern=_VERSION_PATTERN)]


class PackRequires(BaseModel):
    """What a pack needs: a core version range, other packs, and capabilities."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    core: str = Field(default=">=0.0", description="Version range of chipgraph core required.")
    packs: tuple[str, ...] = Field(default=(), description="Names of packs this pack requires.")
    capabilities: tuple[str, ...] = Field(
        default=(), description="Adapter capabilities this pack requires, e.g. 'lint'."
    )


class PackProvides(BaseModel):
    """What a pack provides: paths (relative to the pack directory) to its contents."""

    # `schemas` is written `schema:` in pack.yml; the Python name avoids shadowing
    # pydantic's deprecated BaseModel.schema().
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    rules: tuple[str, ...] = Field(default=(), description="Paths to rule definitions.")
    agents: tuple[str, ...] = Field(default=(), description="Paths to agent role definitions.")
    skills: tuple[str, ...] = Field(default=(), description="Paths to skill definitions.")
    checks: tuple[str, ...] = Field(default=(), description="Paths to check definitions.")
    workflows: tuple[str, ...] = Field(default=(), description="Paths to workflow definitions.")
    templates: tuple[str, ...] = Field(default=(), description="Paths to templates.")
    commands: tuple[str, ...] = Field(
        default=(), description="Command names this pack provides, e.g. '/rtl'."
    )
    schemas: tuple[str, ...] = Field(
        default=(),
        alias="schema",
        description="Paths to schema extensions (`schema:` in pack.yml).",
    )


class PackManifest(BaseModel):
    """The validated contents of a pack's `pack.yml`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    name: PackName = Field(description="The pack's name, e.g. 'digital-rtl'.")
    version: PackVersion = Field(description="The pack's version, MAJOR.MINOR.PATCH.")
    description: str = Field(default="", description="A short, plain-English description.")
    requires: PackRequires = Field(
        default_factory=PackRequires, description="What this pack needs."
    )
    provides: PackProvides = Field(
        default_factory=PackProvides, description="What this pack provides."
    )


class Pack:
    """A loaded pack: its manifest, plus the directory it lives in."""

    def __init__(self, manifest: PackManifest, root: Path) -> None:
        self.manifest = manifest
        self.root = root

    def path(self, rel: str) -> Path:
        """Resolve a path relative to this pack's directory."""
        return self.root / rel

    def __repr__(self) -> str:
        return (
            f"Pack(name={self.manifest.name!r}, "
            f"version={self.manifest.version!r}, root={self.root!r})"
        )


_PROVIDES_PATH_FIELDS = ("rules", "agents", "skills", "checks", "workflows", "templates", "schemas")


def load_pack(directory: Path) -> Pack:
    """Load and validate the pack at `directory`.

    Raises `PluginError` if `pack.yml` is missing or invalid, or if any path listed in
    `provides` (other than `commands`, which are names, not paths) does not exist under
    `directory`.
    """
    manifest_path = directory / "pack.yml"
    if not manifest_path.is_file():
        raise PluginError(f"no pack.yml found in {directory}")
    try:
        raw = yaml.safe_load(manifest_path.read_text()) or {}
        manifest = PackManifest.model_validate(raw)
    except Exception as exc:
        raise PluginError(f"invalid pack manifest at {manifest_path}: {exc}") from exc

    for field in _PROVIDES_PATH_FIELDS:
        for rel in getattr(manifest.provides, field):
            if not (directory / rel).exists():
                raise PluginError(
                    f"pack {manifest.name!r} provides missing path {rel!r} "
                    f"(expected at {directory / rel})"
                )

    return Pack(manifest, directory)


def discover_packs(search_paths: Iterable[Path]) -> dict[str, Pack]:
    """Load every pack found as an immediate subdirectory (containing `pack.yml`) of any path.

    Raises `PluginError` if two discovered packs share a name.
    """
    packs: dict[str, Pack] = {}
    for search_path in search_paths:
        if not search_path.is_dir():
            continue
        for entry in sorted(search_path.iterdir()):
            if not entry.is_dir() or not (entry / "pack.yml").is_file():
                continue
            pack = load_pack(entry)
            name = pack.manifest.name
            if name in packs:
                raise PluginError(
                    f"duplicate pack name {name!r}: found at {packs[name].root} and {entry}"
                )
            packs[name] = pack
    return packs


def resolve_requires(packs: Mapping[str, Pack]) -> None:
    """Check that every pack's `requires.packs` is present in `packs`.

    Raises `PluginError` naming the first missing dependency found.
    """
    for pack in packs.values():
        for required in pack.manifest.requires.packs:
            if required not in packs:
                raise PluginError(
                    f"pack {pack.manifest.name!r} requires pack {required!r}, "
                    "which is not available"
                )
