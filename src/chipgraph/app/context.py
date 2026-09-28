"""`AppContext`: everything a CLI command needs, wired up from a project's `.chipgraph.yml`.

Read-only commands (`status`, `doctor`, `config show`, ...) must work even when no profile
exists yet (DESIGN.md 8.4/6.1): `AppContext.load` always succeeds, and only writes to disk
(state directory, git exclude) when a profile was actually found. Commands that need a
profile call `require_profile()`, which raises a clear `AppError`.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from chipgraph.adapters.review.file import FileReview
from chipgraph.app.errors import AppError
from chipgraph.core.config.errors import ConfigError
from chipgraph.core.config.loader import ResolvedProfile
from chipgraph.core.config.loader import load as load_profile
from chipgraph.core.config.models import DataCfg, Profile
from chipgraph.core.engine.gate import GateEvaluator
from chipgraph.core.plugin_api.registry import PluginError, Registry
from chipgraph.core.state.artifacts import ArtifactStore, LabelRules
from chipgraph.core.state.backend import LocalBackend
from chipgraph.core.state.layout import StateLayout


def find_repo_root(start: Path) -> Path:
    """The nearest ancestor of `start` containing `.git`, or `start` itself if none.

    Mirrors `chipgraph.core.config.loader.find_profile`'s own boundary, but always
    returns a directory (never `None`): a project root is needed even when no profile
    exists yet, so read-only commands (and `init`) have somewhere to work from.
    """
    current = start.resolve()
    if current.is_file():
        current = current.parent
    walker = current
    while True:
        if (walker / ".git").exists():
            return walker
        parent = walker.parent
        if parent == walker:
            return current
        walker = parent


@dataclass
class AppContext:
    """Everything wired up for one CLI invocation: profile, state, store, registry, ..."""

    root: Path
    resolved: ResolvedProfile | None
    layout: StateLayout
    store: ArtifactStore
    registry: Registry
    review: FileReview
    gates: GateEvaluator
    backend: LocalBackend | None = None

    @property
    def profile(self) -> Profile | None:
        """The resolved project profile, or `None` if no `.chipgraph.yml` was found."""
        return self.resolved.profile if self.resolved is not None else None

    def require_profile(self) -> ResolvedProfile:
        """Return the resolved profile, or raise `AppError` if there is none."""
        if self.resolved is None:
            raise AppError("no .chipgraph.yml: only read-only commands work; run `chipgraph init`")
        return self.resolved

    @classmethod
    def load(cls, start: Path, *, profile_path: Path | None = None) -> AppContext:
        """Build an `AppContext` for a command starting at `start`.

        `start` is searched upward for `.chipgraph.yml` (or `profile_path` is used
        directly); the project root is the nearest git root above `start` (or `start`
        itself, if it is not inside a git repo). State is only written to disk (state
        directory created, `.git/info/exclude` updated) when a profile was found.
        """
        root = find_repo_root(start)
        try:
            resolved = load_profile(start, profile_path=profile_path)
        except ConfigError as exc:
            raise AppError(str(exc)) from exc

        layout = StateLayout(root)
        backend: LocalBackend | None = None
        if resolved is not None:
            backend = LocalBackend(root)
            backend.prepare()

        data_cfg = resolved.profile.data if resolved is not None else DataCfg()
        labels = LabelRules(dict.fromkeys(data_cfg.nda_paths, "nda"), default=data_cfg.default)
        store = ArtifactStore(root, labels)

        registry = Registry()
        try:
            registry.discover()
        except PluginError as exc:
            raise AppError(str(exc)) from exc

        review = FileReview(layout.decisions_dir)
        gates = GateEvaluator(review, store)

        return cls(
            root=root,
            resolved=resolved,
            layout=layout,
            store=store,
            registry=registry,
            review=review,
            gates=gates,
            backend=backend,
        )


def default_identity() -> str:
    """The identity to attribute a decision to when `--by` is not given.

    `git config user.name`, falling back to the `$USER` environment variable, then
    `"unknown"`. Never raises: a missing/broken `git` just falls through.
    """
    try:
        result = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return os.environ.get("USER", "unknown")


__all__ = ["AppContext", "default_identity", "find_repo_root"]
