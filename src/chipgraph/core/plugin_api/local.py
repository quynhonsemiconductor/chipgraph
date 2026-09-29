"""Load a project's own plugins from files it lists, through the plugin API.

A project may keep small Python plugins next to its code, under `<root>/.chipgraph/plugins/`,
and list them in the profile so the tool loads them into the registry. They run code: an
organization can forbid them with a `deny` policy, a user can switch them off with an
environment variable, and the caller decides whether loading is enabled.

This module stays generic: it knows nothing about chips, tools or any concrete plugin kind.
A listed file must define ``register(api)`` and call ``api.register(kind, name, obj)`` for
each thing it contributes; a class is instantiated the same way the entry-point `discover`
does. A plugin may not replace an existing registration.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from chipgraph.core.plugin_api.registry import PluginError, Registry

_PLUGINS_SUBDIR = (".chipgraph", "plugins")


class LocalPluginRecord(BaseModel):
    """What loading one listed plugin produced: its file, its hash, what it registered."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entry: str = Field(description="The plugin path exactly as the profile listed it.")
    path: str | None = Field(
        default=None,
        description="Repo-relative path of the resolved file, or None when not loaded.",
    )
    sha256: str | None = Field(
        default=None, description="SHA-256 of the file's bytes, or None when not loaded."
    )
    registered: tuple[tuple[str, str], ...] = Field(
        default=(), description="The (kind, name) pairs this plugin registered, in order."
    )
    loaded: bool = Field(default=True, description="Whether the plugin was actually loaded.")
    disabled_reason: str | None = Field(
        default=None, description="Why loading was skipped, when it was."
    )


class LocalPluginApi:
    """The object handed to a plugin's ``register`` function.

    It forwards to the real `Registry` and records every ``(kind, name)`` the plugin
    registers, so callers can report what a plugin contributed. It mirrors the registry's
    own rules: a class is instantiated, a duplicate name is refused.
    """

    def __init__(self, registry: Registry) -> None:
        self._registry = registry
        self._registered: list[tuple[str, str]] = []

    def register(self, kind: str, name: str, obj: object) -> None:
        """Register `obj` under `kind`/`name`, instantiating it if it is a class/factory."""
        instance = obj() if inspect.isclass(obj) or inspect.isfunction(obj) else obj
        self._registry.register(kind, name, instance)
        self._registered.append((kind, name))

    @property
    def registered(self) -> tuple[tuple[str, str], ...]:
        """The (kind, name) pairs registered so far, in registration order."""
        return tuple(self._registered)


def _resolve_under_plugins_dir(root: Path, entry: str) -> Path:
    """Resolve `entry` (repo-relative) to a real `.py` file under `<root>/.chipgraph/plugins/`.

    Rejects absolute paths, `..` escapes, files outside the plugins directory (after
    following symlinks), missing files, and non-`.py` files, each as a `PluginError`.
    """
    if Path(entry).is_absolute():
        raise PluginError(f"local plugin {entry!r}: must be a repo-relative path, not absolute")

    plugins_dir = root.joinpath(*_PLUGINS_SUBDIR).resolve()
    target = (root / entry).resolve()

    if not target.is_relative_to(plugins_dir):
        raise PluginError(
            f"local plugin {entry!r}: must resolve to a file under "
            f"{Path(*_PLUGINS_SUBDIR)}/, got {target}"
        )
    if not target.is_file():
        raise PluginError(f"local plugin {entry!r}: no file at {target}")
    if target.suffix != ".py":
        raise PluginError(f"local plugin {entry!r}: must be a .py file")
    return target


def _module_name(path: Path, digest: str) -> str:
    """A unique module name that cannot shadow a real package."""
    return f"chipgraph_local_plugin_{path.stem}_{digest[:8]}"


def _load_one(root: Path, entry: str, registry: Registry) -> LocalPluginRecord:
    root = root.resolve()
    target = _resolve_under_plugins_dir(root, entry)
    data = target.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    rel = target.relative_to(root).as_posix()

    module_name = _module_name(target, digest)
    spec = importlib.util.spec_from_file_location(module_name, target)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise PluginError(f"local plugin {entry!r}: cannot create an import spec for {target}")

    module = importlib.util.module_from_spec(spec)
    # The module must be in `sys.modules` while it runs (e.g. `dataclasses` looks its
    # module up there). The name is unique to this file's content, so it cannot shadow a
    # real package; it is removed again if the import fails.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise PluginError(f"local plugin {entry!r}: failed to import: {exc}") from exc

    register = getattr(module, "register", None)
    if not callable(register):
        raise PluginError(f"local plugin {entry!r}: must define a callable register(api)")

    api = LocalPluginApi(registry)
    try:
        register(api)
    except PluginError as exc:
        raise PluginError(f"local plugin {entry!r}: {exc}") from exc
    except Exception as exc:
        raise PluginError(f"local plugin {entry!r}: register() failed: {exc}") from exc

    return LocalPluginRecord(
        entry=entry, path=rel, sha256=digest, registered=api.registered, loaded=True
    )


def load_local_plugins(
    root: Path,
    entries: tuple[str, ...],
    registry: Registry,
    *,
    enabled: bool,
    disabled_reason: str | None = None,
) -> tuple[LocalPluginRecord, ...]:
    """Load each listed plugin into `registry`, in profile order.

    When `enabled` is False nothing is loaded: a record per entry is returned, marked not
    loaded, carrying `disabled_reason`. When enabled, each entry is resolved under
    `<root>/.chipgraph/plugins/`, imported and asked to `register`; any failure raises a
    `PluginError` naming the entry (the cause stays chained).
    """
    if not enabled:
        return tuple(
            LocalPluginRecord(entry=entry, loaded=False, disabled_reason=disabled_reason)
            for entry in entries
        )
    return tuple(_load_one(root, entry, registry) for entry in entries)


__all__ = ["LocalPluginApi", "LocalPluginRecord", "load_local_plugins"]
