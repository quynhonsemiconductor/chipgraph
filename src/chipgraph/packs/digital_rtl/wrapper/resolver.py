"""`foreach: wrappers` and `foreach: wrappers.vendor`: one rule instance per wrapper.

Both expand to one entry per `blocks.<block>.wrappers` entry of the profile (blocks
sorted, wrappers in profile order), with params `block` and `wrapper`. They differ in
the extra inputs each entry carries:

- `wrappers` (the author's rule): the wrapper's `exemplar`, when the profile names one.
  Never the IP's files: the author sees the IP's port declaration only, which its
  context reads from them (`wrapper.context`), never their text;
- `wrappers.vendor` (the generator's rule): the IP's `vendor_files`, so a change to the
  IP makes the generated wrapper stale and it is regenerated (and checked) again.

`WrapperForeach` adds these two selectors to a project's resolver and hands every other
selector to it.
"""

from __future__ import annotations

from chipgraph.core.config.models import Profile
from chipgraph.core.engine.graph import EntryForeachResolver, ForeachEntry, ForeachResolver
from chipgraph.packs.digital_rtl.wrapper.common import all_wrappers

WRAPPERS = "wrappers"
"""The selector of the author's rule."""
WRAPPERS_VENDOR = "wrappers.vendor"
"""The selector of the generator's rule."""
SELECTORS = frozenset({WRAPPERS, WRAPPERS_VENDOR})


def wrapper_entries(profile: Profile, expr: str) -> list[ForeachEntry]:
    """The entries of selector `expr` (one of `SELECTORS`) for the profile."""
    entries: list[ForeachEntry] = []
    for wrapper in all_wrappers(profile):
        if expr == WRAPPERS_VENDOR:
            inputs = tuple(wrapper.cfg.vendor_files)
        else:
            inputs = (wrapper.cfg.exemplar,) if wrapper.cfg.exemplar else ()
        entries.append(ForeachEntry(params=wrapper.params, inputs=inputs))
    return entries


class WrapperForeach:
    """A project's resolver with the `wrappers` selectors added (see the module)."""

    def __init__(self, inner: ForeachResolver, profile: Profile) -> None:
        self.inner = inner
        self.profile = profile

    def expand(self, expr: str) -> list[dict[str, str]]:
        return [dict(entry.params) for entry in self.expand_entries(expr)]

    def expand_entries(self, expr: str) -> list[ForeachEntry]:
        if expr in SELECTORS:
            return wrapper_entries(self.profile, expr)
        if isinstance(self.inner, EntryForeachResolver):
            return self.inner.expand_entries(expr)
        return [ForeachEntry(params=p) for p in self.inner.expand(expr)]


__all__ = ["SELECTORS", "WRAPPERS", "WRAPPERS_VENDOR", "WrapperForeach", "wrapper_entries"]
