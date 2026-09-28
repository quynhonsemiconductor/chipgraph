"""The plugin registry: where adapters, checks and other plugins register themselves.

Plugins register either directly (`Registry.register`) or by Python entry points
(`Registry.discover`), under the group `chipgraph.adapters.<kind>` for each kind in `KINDS`.
"""

from __future__ import annotations

import inspect
from importlib.metadata import entry_points
from typing import Any

from chipgraph.core.plugin_api.protocols import (
    AgentRuntime,
    Check,
    Extractor,
    FormatAdapter,
    Generator,
    LlmProvider,
    LogParser,
    ReviewAdapter,
    Runner,
    ToolAdapter,
    VcsAdapter,
)

KINDS: dict[str, type] = {
    "runner": Runner,
    "parser": LogParser,
    "tool": ToolAdapter,
    "check": Check,
    "vcs": VcsAdapter,
    "review": ReviewAdapter,
    "llm": LlmProvider,
    "runtime": AgentRuntime,
    "format": FormatAdapter,
    "extractor": Extractor,
    "generator": Generator,
}
"""Maps a plugin kind name to the protocol it must satisfy."""


class PluginError(Exception):
    """Raised for any registry or pack error: unknown kind, duplicate name, bad plugin, ..."""


class Registry:
    """Holds registered plugin instances, keyed by kind and name."""

    def __init__(self) -> None:
        self._plugins: dict[str, dict[str, object]] = {kind: {} for kind in KINDS}

    def register(self, kind: str, name: str, obj: object) -> None:
        """Register `obj` under `kind`/`name`.

        Raises `PluginError` if `kind` is unknown, `name` is already registered for that
        kind, or `obj` does not satisfy the protocol for `kind`.
        """
        if kind not in KINDS:
            raise PluginError(f"unknown plugin kind {kind!r}; known kinds: {sorted(KINDS)}")
        if name in self._plugins[kind]:
            raise PluginError(f"{kind}/{name} is already registered")
        protocol = KINDS[kind]
        if not isinstance(obj, protocol):
            raise PluginError(
                f"{obj!r} does not satisfy the {protocol.__name__} protocol for kind {kind!r}"
            )
        self._plugins[kind][name] = obj

    def get(self, kind: str, name: str) -> Any:
        """Return the plugin registered under `kind`/`name`.

        Raises `PluginError` if `kind` is unknown, listing the registered names otherwise.
        """
        if kind not in KINDS:
            raise PluginError(f"unknown plugin kind {kind!r}; known kinds: {sorted(KINDS)}")
        try:
            return self._plugins[kind][name]
        except KeyError:
            available = sorted(self._plugins[kind])
            raise PluginError(f"no {kind} plugin named {name!r}; available: {available}") from None

    def names(self, kind: str) -> tuple[str, ...]:
        """Return the sorted names registered under `kind`."""
        if kind not in KINDS:
            raise PluginError(f"unknown plugin kind {kind!r}; known kinds: {sorted(KINDS)}")
        return tuple(sorted(self._plugins[kind]))

    def discover(self) -> int:
        """Load plugins from Python entry points, one group per kind.

        For each kind, entry points under group `chipgraph.adapters.<kind>` are loaded.
        A loaded object that is a class or a no-argument factory is called to produce the
        plugin instance; anything else is registered as-is. Returns the number registered.
        Any error loading or registering one entry point becomes a `PluginError` naming it.
        """
        count = 0
        for kind in KINDS:
            group = f"chipgraph.adapters.{kind}"
            for entry_point in entry_points(group=group):
                try:
                    loaded = entry_point.load()
                    is_factory = inspect.isclass(loaded) or inspect.isfunction(loaded)
                    obj = loaded() if is_factory else loaded
                    self.register(kind, entry_point.name, obj)
                except Exception as exc:
                    raise PluginError(
                        f"failed to load entry point {entry_point.name!r} in group {group!r}: {exc}"
                    ) from exc
                count += 1
        return count
