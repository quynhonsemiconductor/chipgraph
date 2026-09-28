"""The `git` VCS adapter: head, changed files, and disposable worktrees.

Implements `chipgraph.core.plugin_api.protocols.VcsAdapter`. All git invocations go
through `subprocess.run` with argument lists (never `shell=True`); a failed command
raises `VcsError` naming the command and its stderr.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class VcsError(Exception):
    """Raised when a git command fails, or a workspace operation is misused."""


def main_repo(path: Path) -> Path:
    """Return the main repository directory for the (possibly worktree) checkout at `path`."""
    result = _run(["git", "-C", str(path), "rev-parse", "--git-common-dir"], cwd=path)
    common_dir = Path(result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (path / common_dir).resolve()
    # `--git-common-dir` points at the `.git` directory; the repo root is its parent,
    # unless the repo's `.git` dir does not sit directly under the worktree root (rare
    # for our purposes), in which case the common dir itself is still a safe `-C` target.
    if common_dir.name == ".git":
        return common_dir.parent
    return common_dir


def _run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True, cwd=cwd)
    except subprocess.CalledProcessError as exc:
        raise VcsError(f"command {' '.join(cmd)!r} failed in {cwd}: {exc.stderr.strip()}") from exc
    except FileNotFoundError as exc:
        raise VcsError(f"command {' '.join(cmd)!r} failed in {cwd}: {exc}") from exc


def _parse_porcelain_z(output: str) -> set[str]:
    """Parse `git status --porcelain=v1 -z` output into a set of repo-relative paths."""
    paths: set[str] = set()
    fields = output.split("\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        if entry == "":
            i += 1
            continue
        status = entry[:2]
        path = entry[3:]
        paths.add(path)
        i += 1
        if status[0] in ("R", "C") and i < len(fields) and fields[i] != "":
            # Renamed/copied entries carry the original path as a following NUL field.
            paths.add(fields[i])
            i += 1
    return paths


class GitVcs:
    """The `git` implementation of `VcsAdapter`."""

    name = "git"

    def __init__(self, workspaces_dir: Path | None = None) -> None:
        self._workspaces_dir = workspaces_dir

    def head(self, root: Path) -> str:
        """Return the current HEAD revision (full sha) of the repo at `root`."""
        result = _run(["git", "rev-parse", "HEAD"], cwd=root)
        return result.stdout.strip()

    def changed_files(self, root: Path, since: str | None = None) -> tuple[str, ...]:
        """Return repo-relative paths changed in the working tree/index, and since `since`."""
        paths: set[str] = set()

        status = _run(["git", "status", "--porcelain=v1", "-z"], cwd=root)
        paths |= _parse_porcelain_z(status.stdout)

        if since is not None:
            diff = _run(["git", "diff", "--name-only", "-z", since, "HEAD"], cwd=root)
            paths |= {p for p in diff.stdout.split("\0") if p}

        return tuple(sorted(paths))

    def create_workspace(self, root: Path, name: str) -> Path:
        """Create a detached worktree named `name` under the workspaces directory."""
        if not _NAME_RE.match(name):
            raise VcsError(f"invalid workspace name {name!r}: must match {_NAME_RE.pattern!r}")
        base = self._workspaces_dir or (root / ".chipgraph" / "state" / "worktrees")
        target = base / name
        if target.exists():
            raise VcsError(f"workspace path already exists: {target}")
        base.mkdir(parents=True, exist_ok=True)
        _run(["git", "worktree", "add", "--detach", str(target), "HEAD"], cwd=root)
        return target

    def remove_workspace(self, path: Path) -> None:
        """Remove a worktree previously created by `create_workspace`."""
        repo = main_repo(path)
        _run(["git", "worktree", "remove", "--force", str(path)], cwd=repo)
        _run(["git", "worktree", "prune"], cwd=repo)
