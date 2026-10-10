"""Shared helpers for the fan-out tests: real git repos, branch callables, snapshots."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from chipgraph.core.engine.fanout import BranchContext, BranchFn, BranchOutcome


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)
    return result.stdout


def init_repo(root: Path, files: Mapping[str, str] | None = None) -> str:
    """A repo on `main` with `files` committed; returns the HEAD sha."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "config", "commit.gpgsign", "false")
    for rel, text in (files or {"README.md": "hello\n"}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return git(root, "rev-parse", "HEAD").strip()


def writer(
    files: Mapping[str, str],
    *,
    assumptions: tuple[str, ...] = (),
    keys: Mapping[str, str] | None = None,
    ok: bool = True,
    delay: float = 0.0,
    executable: tuple[str, ...] = (),
) -> BranchFn:
    """A branch that writes `files` into its workspace (after `delay` seconds)."""

    async def run(ctx: BranchContext) -> BranchOutcome:
        if delay:
            await asyncio.sleep(delay)
        for rel, text in files.items():
            path = ctx.path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            if rel in executable:
                path.chmod(0o755)
        return BranchOutcome(
            ok=ok,
            message="" if ok else "branch failed",
            assumptions=assumptions,
            assumption_keys=dict(keys or {}),
        )

    return run


def _tree_files(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            data = path.read_bytes()
            out[rel] = f"{oct(path.stat().st_mode)}:{hashlib.sha256(data).hexdigest()}"
    return out


def snapshot(root: Path) -> dict[str, object]:
    """The user's working tree, index, HEAD and stash, read without writing anything.

    The index bytes are read first: `git status` would refresh it, so it is never run.
    """
    git_dir = root / ".git"
    return {
        "index": hashlib.sha256((git_dir / "index").read_bytes()).hexdigest(),
        "HEAD": (git_dir / "HEAD").read_text(),
        "head_sha": git(root, "rev-parse", "HEAD"),
        "stash": git(root, "stash", "list"),
        "files": _tree_files(root),
    }


def tree_listing(root: Path, ref: str) -> dict[str, tuple[str, str]]:
    """path -> (mode, content) of every file in `ref`."""
    out: dict[str, tuple[str, str]] = {}
    for line in git(root, "ls-tree", "-r", ref).splitlines():
        meta, path = line.split("\t", 1)
        mode, _kind, sha = meta.split()
        out[path] = (mode, git(root, "cat-file", "-p", sha))
    return out


def linked_worktrees(root: Path) -> list[str]:
    """The linked worktrees git still knows for `root` (not the main one)."""
    entries = [
        line[len("worktree ") :]
        for line in git(root, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]
    return entries[1:]
