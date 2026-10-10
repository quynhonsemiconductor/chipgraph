"""Skills: per-artifact-kind instructions a role loads for a task (DESIGN.md 5.1, D8).

A skill is a markdown file with a YAML frontmatter::

    ---
    id: lang-sv/rtl            # <namespace>/<name>
    description: One line.
    roles: [author]            # the roles that may use it
    version: 0.1.0
    ---
    The instructions (the body).

Packs list skill files, or directories of `*.md` skill files, in `provides.skills`.
`load_skills(packs)` reads every skill of the given packs; `SkillSet.resolve(ids, role)`
returns the skills a task names, and refuses an unknown id or a skill its role may not
use.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from chipgraph.core.plugin_api.pack import Pack
from chipgraph.core.runtime.roles.registry import find_role
from chipgraph.core.runtime.roles.spec import RoleId, split_frontmatter

SkillId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9_-]*$")]
"""A skill id, `<namespace>/<name>`, e.g. `lang-sv/rtl`."""


class SkillError(Exception):
    """A broken skill file, a duplicate or unknown id, or a skill its role may not use."""


class SkillSpec(BaseModel):
    """One skill: its frontmatter plus the instructions (the file's body)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Schema version of this model.")
    id: SkillId = Field(description="The skill id, '<namespace>/<name>'.")
    description: str = Field(min_length=1, description="One line: what the skill is for.")
    roles: tuple[RoleId, ...] = Field(min_length=1, description="The roles that may use it.")
    version: Annotated[str, StringConstraints(pattern=r"^\d+\.\d+\.\d+$")] = Field(
        description="The skill's version, MAJOR.MINOR.PATCH."
    )
    text: str = Field(min_length=1, description="The instructions: the file's body.")


def parse_skill_file(path: Path) -> SkillSpec:
    """Load one skill file. Raises `SkillError` for a bad file or an unknown role."""
    try:
        data, body = split_frontmatter(path.read_text(encoding="utf-8"), source=str(path))
        if "text" in data:
            raise ValueError(f"{path}: the text is the file's body, not a frontmatter key")
        skill = SkillSpec.model_validate({**data, "text": body})
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
        raise SkillError(f"invalid skill file {path}: {exc}") from exc
    unknown = [r for r in skill.roles if find_role(r) is None]
    if unknown:
        raise SkillError(f"skill {skill.id!r} in {path} names unknown roles: {', '.join(unknown)}")
    return skill


def _skill_files(path: Path) -> list[Path]:
    return sorted(path.glob("*.md")) if path.is_dir() else [path]


class SkillSet:
    """The skills of a set of packs, by id."""

    def __init__(self, skills: Mapping[str, SkillSpec], sources: Mapping[str, str]) -> None:
        self.skills = MappingProxyType(dict(skills))
        self._sources = MappingProxyType(dict(sources))

    def source(self, skill_id: str) -> str:
        """Where a skill was loaded from (pack name and file)."""
        return self._sources[skill_id]

    def get(self, skill_id: str) -> SkillSpec:
        """The skill `skill_id`; raises `SkillError` if no loaded pack provides it."""
        skill = self.skills.get(skill_id)
        if skill is None:
            known = ", ".join(sorted(self.skills)) or "none"
            raise SkillError(
                f"unknown skill {skill_id!r}: no pack of this project provides it "
                f"(skills available: {known})"
            )
        return skill

    def resolve(self, skill_ids: Iterable[str], role: str) -> tuple[SkillSpec, ...]:
        """The skills `skill_ids` for a task of `role`, in order.

        Raises `SkillError` for an unknown id, or a skill whose `roles` does not list
        `role` (a `<namespace>/` prefix on the role is ignored).
        """
        role_id = role.rsplit("/", 1)[-1]
        found: list[SkillSpec] = []
        for skill_id in skill_ids:
            skill = self.get(skill_id)
            if role_id not in skill.roles:
                raise SkillError(
                    f"skill {skill_id!r} is not for role {role_id!r} "
                    f"(it may be used by: {', '.join(skill.roles)})"
                )
            found.append(skill)
        return tuple(found)


def load_skills(packs: Iterable[Pack]) -> SkillSet:
    """Every skill the packs provide. Raises `SkillError` for a broken file or a duplicate id."""
    skills: dict[str, SkillSpec] = {}
    sources: dict[str, str] = {}
    for pack in packs:
        for rel in pack.manifest.provides.skills:
            for path in _skill_files(pack.path(rel)):
                skill = parse_skill_file(path)
                where = f"pack {pack.manifest.name!r} ({path})"
                if skill.id in skills:
                    raise SkillError(
                        f"duplicate skill id {skill.id!r}: in {sources[skill.id]} and {where}"
                    )
                skills[skill.id] = skill
                sources[skill.id] = where
    return SkillSet(skills, sources)


__all__ = ["SkillError", "SkillId", "SkillSet", "SkillSpec", "load_skills", "parse_skill_file"]
