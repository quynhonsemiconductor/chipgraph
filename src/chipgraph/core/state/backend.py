"""State backends: where `.chipgraph/state/` actually lives (DESIGN.md 6.1).

This task implements only the `local` backend, which keeps state on the local disk under
the project repo, hidden from git via `.git/info/exclude` (never by editing a tracked
`.gitignore`). The `branch:<name>` (and `repo:...`) backends, which push run state to an
orphan branch or a separate state repository so a run can be resumed or shared from
another machine, need git operations and are left to a later task in the adapters layer —
`chipgraph.core` must not shell out to git (see AGENTS.md: "no subprocess, no git commands
in this code").
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from chipgraph.core.state.layout import StateLayout

_EXCLUDE_LINE = "/.chipgraph/state/"


@runtime_checkable
class StateBackend(Protocol):
    """Where a project's run state is stored, and how to make sure it's ready to use."""

    name: str

    def prepare(self) -> None:
        """Make sure the backend is ready to use (e.g. create directories, hide from git)."""
        ...

    @property
    def layout(self) -> StateLayout:
        """The paths this backend stores state under."""
        ...


def _git_common_dir(root: Path) -> Path | None:
    """Resolve `root/.git`'s common git dir, without shelling out to git.

    Handles both a plain repo (`.git` is a directory) and a worktree (`.git` is a file
    containing `gitdir: <path>`, whose target directory has a `commondir` file pointing at
    the real common git dir, relative to that `gitdir` path).
    """
    git_path = root / ".git"
    if git_path.is_dir():
        return git_path
    if not git_path.is_file():
        return None

    content = git_path.read_text(encoding="utf-8").strip()
    prefix = "gitdir:"
    if not content.startswith(prefix):
        return None
    gitdir = Path(content[len(prefix) :].strip())
    if not gitdir.is_absolute():
        gitdir = (root / gitdir).resolve()

    commondir_file = gitdir / "commondir"
    if not commondir_file.exists():
        # Not a linked worktree: gitdir *is* the common dir.
        return gitdir

    common = Path(commondir_file.read_text(encoding="utf-8").strip())
    if not common.is_absolute():
        common = (gitdir / common).resolve()
    return common


class LocalBackend:
    """Keeps run state on local disk under the project repo, hidden from git.

    `prepare()` creates the state directory and, if the project is a git repo, appends
    `/.chipgraph/state/` to that repo's `.git/info/exclude` — never to a tracked
    `.gitignore` — so state never shows up in `git status` for anyone. It is idempotent:
    calling it again does not duplicate the exclude line or otherwise change tracked files.
    """

    name = "local"

    def __init__(self, root: Path) -> None:
        self.root = root
        self._layout = StateLayout(root)

    @property
    def layout(self) -> StateLayout:
        return self._layout

    def prepare(self) -> None:
        self._layout.state_dir.mkdir(parents=True, exist_ok=True)
        common_dir = _git_common_dir(self.root)
        if common_dir is None:
            return
        exclude_path = common_dir / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = ""
        if exclude_path.exists():
            existing = exclude_path.read_text(encoding="utf-8")
        lines = existing.splitlines()
        if _EXCLUDE_LINE in lines:
            return
        with open(exclude_path, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(_EXCLUDE_LINE + "\n")


def backend_from_config(value: str, root: Path) -> StateBackend:
    """Build a `StateBackend` from a `.chipgraph.yml` `state.backend` value."""
    if value == "local":
        return LocalBackend(root)
    if value.startswith("branch:") or value.startswith("repo:"):
        raise NotImplementedError(f"state backend {value!r} lands with the git state backend task")
    raise ValueError(f"unknown state backend: {value!r}")


__all__ = ["LocalBackend", "StateBackend", "backend_from_config"]
