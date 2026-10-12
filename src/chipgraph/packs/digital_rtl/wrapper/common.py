"""Shared by the wrapper rules, tool, check and context: paths and the profile entries."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from chipgraph.core.config.models import Profile, WrapperCfg

DEFAULT_SRC = "rtl/emacs/{wrapper}.src.sv"
"""Where a wrapper's source is, unless a rule or check names another template."""
DEFAULT_OUT = "rtl/{wrapper}.sv"
"""Where the generated wrapper is, unless a rule or check names another template."""
RULE = "digital-rtl/wrapper"
"""The author's rule: writes the source."""
GEN_RULE = "digital-rtl/wrapper_gen"
"""The generator's rule: writes the expanded wrapper."""


class WrapperError(Exception):
    """A wrapper that cannot be named: no such entry, a bad path template."""


@dataclass(frozen=True)
class Wrapper:
    """One `blocks.<block>.wrappers` entry, with the block it belongs to."""

    block: str
    cfg: WrapperCfg

    @property
    def name(self) -> str:
        return self.cfg.name

    @property
    def params(self) -> dict[str, str]:
        """The params of its rule instances."""
        return {"block": self.block, "wrapper": self.cfg.name}


def all_wrappers(profile: Profile, block: str | None = None) -> list[Wrapper]:
    """Every wrapper of the profile (of `block` only, when given), blocks sorted."""
    blocks = [block] if block is not None else sorted(profile.blocks)
    out: list[Wrapper] = []
    for name in blocks:
        override = profile.blocks.get(name)
        if override is not None:
            out.extend(Wrapper(block=name, cfg=cfg) for cfg in override.wrappers)
    return out


def find_wrapper(profile: Profile, params: Mapping[str, str]) -> Wrapper:
    """The wrapper a task's params (`block`, `wrapper`) name; `WrapperError` if none."""
    name = params.get("wrapper")
    block = params.get("block")
    if not name:
        raise WrapperError("the task has no `wrapper` param")
    for wrapper in all_wrappers(profile, block):
        if wrapper.name == name:
            return wrapper
    where = f"blocks.{block}.wrappers" if block else "any block's wrappers"
    raise WrapperError(f"no wrapper {name!r} in the profile's {where}")


def fill(template: str, params: Mapping[str, str]) -> str:
    """`template` with `{key}` / `{KEY}` from `params`; `WrapperError` if one is left."""
    out = template
    for key, value in params.items():
        out = out.replace("{" + key + "}", value).replace("{" + key.upper() + "}", value.upper())
    if "{" in out or "}" in out:
        raise WrapperError(f"path template {template!r} has a placeholder no param fills")
    return repo_rel(out)


def repo_rel(path: str) -> str:
    """`path` as a normalised repo-relative POSIX path; `WrapperError` if it is not one."""
    text = path.replace("\\", "/").strip()
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or text.startswith("/") or ".." in parts or (len(text) > 1 and text[1] == ":"):
        raise WrapperError(f"{path!r} is not a repo-relative path")
    return "/".join(parts)


def inside(root: Path, rel: str) -> Path:
    """The file `rel` of the repo at `root`, refused when it resolves outside it."""
    path = root / repo_rel(rel)
    real = Path(os.path.realpath(path))
    if not real.is_relative_to(Path(os.path.realpath(root))):
        raise WrapperError(f"{rel} resolves outside the repository")
    return path


def paths(wrapper: Wrapper, src: str | None = None, out: str | None = None) -> tuple[str, str]:
    """The wrapper's source and generated paths, from templates (default: the rules')."""
    params = wrapper.params
    return fill(src or DEFAULT_SRC, params), fill(out or DEFAULT_OUT, params)


def instance_id(rule: str, wrapper: Wrapper) -> str:
    """The id of `rule`'s instance for `wrapper` (as `RuleInstance.make_id` builds it)."""
    return f"{rule}[block={wrapper.block},wrapper={wrapper.name}]"


__all__ = [
    "DEFAULT_OUT",
    "DEFAULT_SRC",
    "GEN_RULE",
    "RULE",
    "Wrapper",
    "WrapperError",
    "all_wrappers",
    "fill",
    "find_wrapper",
    "inside",
    "instance_id",
    "paths",
    "repo_rel",
]
