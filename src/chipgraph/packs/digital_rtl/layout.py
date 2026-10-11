"""Where a block's RTL and testbench files go: from the profile's `layout` (M2-11a).

A project says where each kind of file lives with `layout:` templates (and per block,
`blocks.<b>.layout`). The built-in rules write new files there:

- `digital-rtl/rtl_module` writes `{layout.rtl}/{module}.sv`;
- `dv/tb_module` writes `{layout.tb}/test_{block}.py`;
- a plan's write sets must stay inside the block's `rtl` and `tb` layout paths.

`layout_dir(resolved, block, kind)` is the directory part of the block's first `kind`
template: `{block}`/`{BLOCK}` filled in, cut before the file name and before the first
segment with a glob or another placeholder. QSoC's `design/{block}/rtl/*.sv` gives
`design/pwm/rtl` for `pwm`; tinysoc's `rtl/*.sv` gives `rtl`; `dv/{block}/**` gives
`dv/timer`. With a list of templates the first one is where new files go.

`layout_vars(resolved, block)` is what the graph fills `{layout.rtl}` and `{layout.tb}`
with (template values, not params: they never change an instance id). A project with no
`rtl` or `tb` layout key keeps the built-in defaults, `rtl` and `dv/{block}`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from chipgraph.core.config.loader import ResolvedProfile
from chipgraph.core.config.models import Profile

LayoutValue = str | tuple[str, ...]

RTL_VAR = "layout.rtl"
"""The template value of the block's RTL directory."""
TB_VAR = "layout.tb"
"""The template value of the block's testbench directory."""

DEFAULT_DIRS: Mapping[str, str] = {"rtl": "rtl", "tb": "dv/{block}"}
"""The directories the built-in rules use when the profile has no layout key for a kind."""

_KINDS: Mapping[str, str] = {RTL_VAR: "rtl", TB_VAR: "tb"}
_DYNAMIC = re.compile(r"[*?\[\]{}]")


class LayoutError(ValueError):
    """The profile's layout does not say where a kind of file goes."""


def _profile(resolved: ResolvedProfile | Profile, block: str) -> Profile:
    if isinstance(resolved, ResolvedProfile):
        return resolved.for_block(block)
    override = resolved.blocks.get(block)
    if override is not None and override.layout:
        return resolved.model_copy(update={"layout": {**resolved.layout, **override.layout}})
    return resolved


def _fill(template: str, block: str) -> str:
    return template.replace("{block}", block).replace("{BLOCK}", block.upper())


def templates(resolved: ResolvedProfile | Profile, block: str, kind: str) -> tuple[str, ...]:
    """The block's `kind` layout templates, `{block}`/`{BLOCK}` filled in (maybe none)."""
    value: LayoutValue | None = _profile(resolved, block).layout.get(kind)
    if value is None:
        return ()
    raw = (value,) if isinstance(value, str) else tuple(value)
    return tuple(_fill(t, block) for t in raw)


def template_dir(template: str) -> str:
    """The directory part of a filled layout template ('' when it has none)."""
    parts = template.split("/")
    out: list[str] = []
    for part in parts[:-1]:  # the last segment is the file (or `**`)
        if not part or part == "." or _DYNAMIC.search(part):
            break
        out.append(part)
    return "/".join(out)


def layout_dir(resolved: ResolvedProfile | Profile, block: str, kind: str) -> str:
    """The directory new `kind` files of `block` go to (see the module docstring).

    Raises `LayoutError` when the block's layout has no `kind` key, or its first
    template has no directory part.
    """
    found = templates(resolved, block, kind)
    if not found:
        raise LayoutError(
            f"block {block!r} has no layout.{kind}: say where its {kind} files go in "
            f'.chipgraph.yml, e.g. `layout: {{ {kind}: "<dir>/*" }}`'
        )
    directory = template_dir(found[0])
    if not directory or directory == ".." or directory.startswith("../"):
        raise LayoutError(
            f"block {block!r}: layout.{kind} {found[0]!r} has no directory part before its "
            "first glob or placeholder; start it with the directory, e.g. 'rtl/*.sv'"
        )
    return directory


def dir_or_default(resolved: ResolvedProfile | Profile, block: str, kind: str) -> str:
    """`layout_dir`, or the built-in default (`DEFAULT_DIRS`) when the key is missing."""
    if not templates(resolved, block, kind):
        return _fill(DEFAULT_DIRS[kind], block)
    return layout_dir(resolved, block, kind)


def layout_vars(resolved: ResolvedProfile | Profile, block: str) -> dict[str, str]:
    """`{layout.rtl}` and `{layout.tb}` for `block` (raises `LayoutError` on a bad template)."""
    return {var: dir_or_default(resolved, block, kind) for var, kind in _KINDS.items()}


def write_globs(resolved: ResolvedProfile | Profile, block: str) -> tuple[str, ...]:
    """The paths a plan of `block` may write: its `rtl` and `tb` layout templates.

    A kind with no layout key allows everything under its default directory.
    """
    out: list[str] = []
    for kind in ("rtl", "tb"):
        found = templates(resolved, block, kind)
        out.extend(found or (f"{_fill(DEFAULT_DIRS[kind], block)}/**",))
    return tuple(dict.fromkeys(out))


def fill_vars(template: str, values: Mapping[str, str]) -> str:
    """`template` with each `{name}` of `values` replaced; other placeholders stay."""
    for name, value in values.items():
        template = template.replace("{" + name + "}", value)
    return template


__all__ = [
    "DEFAULT_DIRS",
    "RTL_VAR",
    "TB_VAR",
    "LayoutError",
    "dir_or_default",
    "fill_vars",
    "layout_dir",
    "layout_vars",
    "template_dir",
    "templates",
    "write_globs",
]
