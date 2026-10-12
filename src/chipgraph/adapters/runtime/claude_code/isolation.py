"""Isolated workspaces for agent tasks in the claude-code runtime (M2-11c).

When `fanout.isolate: true` (profile) or a rule sets `isolate: true`, each agent task
gets its own workspace outside the user's tree: a worktree at HEAD with the task's
declared inputs copied in. The subagent writes there, not the user's files. On
acceptance, only the task's outputs are copied back; on any other ending, the workspace
is removed with nothing touching the user's tree.

This module provides:
- `create_workspace`: create and seed a workspace for a task
- `remove_workspace`: remove a task's workspace
- `sweep_workspaces`: remove stale workspaces from killed runs
- `should_isolate`: whether a task should run isolated (profile or rule)
- `diff_workspace`: get files changed in workspace vs seed
- `copy_outputs_back`: copy outputs from workspace to user's tree (with refusal)
- `check_overlapping_writes`: check if two tasks' write sets overlap
- Best-of-N support: `candidate_id`, `parse_candidate_id`, `best_of_n`
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from chipgraph.core.engine.fanout import (
    fanout_tmp_root,
    seed_workspace,
    sweep_stale,
)
from chipgraph.core.model.store import default_model_db_path
from chipgraph.core.state.artifacts import hash_file

if TYPE_CHECKING:
    from chipgraph.adapters.vcs.git import GitVcs
    from chipgraph.app.context import AppContext
    from chipgraph.core.config.models import Profile
    from chipgraph.core.contracts import RuleInstance, RuleSpec


# --- Copy-back result types --------------------------------------------------------------


class CopyBackResult(BaseModel):
    """Result of copying outputs from workspace back to user's tree."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool = Field(description="Whether all outputs were copied successfully.")
    copied: tuple[str, ...] = Field(default=(), description="Repo-relative paths of files copied.")
    refused: tuple[str, ...] = Field(
        default=(),
        description="Files refused (user's copy changed since seed).",
    )
    symlinks: tuple[str, ...] = Field(
        default=(),
        description="Symlinks on the output paths (not copied).",
    )
    outside: tuple[str, ...] = Field(
        default=(),
        description="Files that exist in workspace but outside allowed_writes.",
    )
    reason: str = Field(default="", description="Why copy-back failed, if not ok.")


def should_isolate(profile: Profile, rule: RuleSpec | None = None) -> bool:
    """Whether a task should run in an isolated workspace.

    True when `fanout.isolate` is true in the profile, or (eventually) when the rule
    sets `isolate: true` as a parameter. Default is false: existing behavior.
    """
    # Future: also check rule.params.get("isolate", False)
    return profile.fanout.isolate


def workspace_tmp_root(ctx: AppContext) -> Path:
    """Where task workspaces go: `<state>/tmp/fanout` if outside the repo, else system temp."""
    return fanout_tmp_root(ctx.root, ctx.layout)


def workspace_dir_for(ctx: AppContext, run_id: str, task_id: str) -> Path:
    """The directory for a task's workspace: `<tmp>/<run_id>/<safe name>/`.

    The name is a lowercase slug of the task id plus a short hash of the exact id, so two
    ids never share a directory and every name passes the VCS adapter's name rule.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", task_id.lower()).strip("-")[:40] or "task"
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:8]
    return workspace_tmp_root(ctx) / run_id / f"{slug}-{digest}"


def create_workspace(
    ctx: AppContext,
    vcs: GitVcs,
    run_id: str,
    task_id: str,
    inputs: tuple[str, ...],
    outputs: tuple[str, ...] = (),
) -> tuple[Path, dict[str, str]]:
    """Create and seed a workspace for a task.

    Returns (workspace_path, seed_hashes): the hash of every file the workspace holds that
    differs from HEAD or that the task reads or writes, as it was when the task started
    (`""` for a file that HEAD has and the user's tree does not).

    The workspace is a git worktree at HEAD, with the task's inputs and outputs copied from
    the user's tree (so it sees what the user sees). The user's working tree, index and HEAD
    are never touched.
    """
    workspace_dir = workspace_dir_for(ctx, run_id, task_id)
    workspace_dir.parent.mkdir(parents=True, exist_ok=True)
    base = vcs.head(ctx.root)
    workspace = vcs.create_branch_workspace(
        ctx.root, base, workspace_dir.name, workspace_dir.parent
    ).resolve()
    try:
        seed_workspace(ctx.root, workspace, (*inputs, *outputs, *_user_delta(ctx.root, vcs)))
        _copy_model(ctx.root, workspace)
        seed_hashes: dict[str, str] = {}
        for rel in vcs.status_all(workspace):
            seed_hashes[rel] = _hash_or_empty(workspace / rel)
        for rel in (*inputs, *outputs):
            top = workspace / rel
            if top.is_dir() and not top.is_symlink():
                for dirpath, dirnames, filenames in os.walk(top):
                    dirnames[:] = [d for d in dirnames if d != ".git"]
                    for name in filenames:
                        full = Path(dirpath) / name
                        seed_hashes[full.relative_to(workspace).as_posix()] = _hash_or_empty(full)
            else:
                seed_hashes[rel] = _hash_or_empty(top)
    except BaseException:
        remove_workspace(ctx, vcs, workspace)
        raise
    return workspace, seed_hashes


MAX_DELTA_FILES = 5000
"""More uncommitted files than this in the user's tree: no workspace (something is wrong)."""


def _user_delta(root: Path, vcs: GitVcs) -> tuple[str, ...]:
    """What the user's tree has that HEAD does not: modified, deleted and untracked files.

    The workspace is a worktree at HEAD, so the accepted outputs of the tasks this one builds
    on (uncommitted in the user's tree) would be missing from it: a lint or a testbench check
    in the workspace could not find them. They are copied in, as the user sees them. Engine
    state (`.chipgraph/`) is not part of the project.
    """
    paths = [
        p for p in vcs.status_all(root) if p != ".chipgraph" and not p.startswith(".chipgraph/")
    ]
    if len(paths) > MAX_DELTA_FILES:
        raise OSError(f"{len(paths)} uncommitted files in the tree: commit or stash them first")
    return tuple(paths)


def _copy_model(root: Path, workspace: Path) -> None:
    """A read-only copy of the Design Model's index, so the checks run in the workspace see it.

    The index is a cache under `.chipgraph/state` (not tracked); the workspace gets its own
    copy, so nothing a check does to it reaches the user's state.
    """
    source = default_model_db_path(root)
    if source.is_file() and not source.is_symlink():
        target = default_model_db_path(workspace)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _hash_or_empty(path: Path) -> str:
    """The content hash of a regular file; `""` for anything else (missing, link, directory)."""
    if path.is_symlink() or not path.is_file():
        return ""
    return hash_file(path)


def remove_workspace(ctx: AppContext, vcs: GitVcs, workspace: str | Path) -> None:
    """Remove a task's workspace. A no-op if it is already gone."""
    _ = ctx  # unused but kept for API consistency
    workspace_path = Path(workspace)
    if not workspace_path.exists() and not workspace_path.is_symlink():
        return
    with contextlib.suppress(Exception):
        vcs.remove(workspace_path)
    # Fallback: remove the directory tree
    if workspace_path.exists() or workspace_path.is_symlink():
        shutil.rmtree(workspace_path, ignore_errors=True)


def sweep_workspaces(ctx: AppContext, vcs: GitVcs) -> int:
    """Remove stale workspaces from killed runs. Returns count of runs swept."""
    stale = sweep_stale(ctx.root, vcs, layout=ctx.layout)
    return len(stale)


def input_paths_for(instance: RuleInstance) -> tuple[str, ...]:
    """The repo-relative paths of a task's inputs (files, not model keys)."""
    return tuple(ref.path for ref in instance.inputs if ref.path is not None)


# --- Step 2: diff workspace, copy-back, write-set checks ---------------------------------


def _has_symlink_in_path(path: Path, base: Path) -> bool:
    """Check if any component of `path` relative to `base` is a symlink."""
    current = base
    for part in path.relative_to(base).parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _is_safe_output(workspace: Path, rel: str) -> tuple[bool, str]:
    """Check if a workspace output path is safe to copy (no symlinks anywhere on path).

    Returns (is_safe, reason) where reason explains why not if is_safe is False.
    """
    path = workspace / rel
    if not path.exists() and not path.is_symlink():
        return True, ""  # Missing files are not our concern here
    # Check for symlinks anywhere in the path
    if _has_symlink_in_path(path, workspace):
        return False, f"symlink in path: {rel}"
    if path.is_symlink():
        return False, f"output is a symlink: {rel}"
    return True, ""


def diff_workspace(
    workspace: Path,
    allowed_writes: tuple[str, ...],
    seed_hashes: dict[str, str],
    vcs: GitVcs | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Get files changed in workspace since seed.

    Returns (changed_in_allowed, changed_outside) where:
    - changed_in_allowed: files in allowed_writes that changed or were added
    - changed_outside: files outside allowed_writes that changed (should be rejected); found
      with `git status` of the worktree, so a file that HEAD has and nobody touched is not one.
      Anything under `.chipgraph/` is the engine's own (check scratch) and ignored.
    """
    changed_in_allowed: list[str] = []
    changed_outside: list[str] = []
    allowed_set = set(allowed_writes)
    for rel in allowed_writes:
        path = workspace / rel
        present = path.is_file() or path.is_symlink()
        # a link is always a change: `copy_outputs_back` refuses it by name
        if present and (
            path.is_symlink() or rel not in seed_hashes or _hash_or_empty(path) != seed_hashes[rel]
        ):
            changed_in_allowed.append(rel)
    if vcs is None:
        from chipgraph.adapters.vcs.git import GitVcs as _GitVcs

        vcs = _GitVcs()
    for rel in vcs.status_all(workspace):
        if rel in allowed_set or rel == ".chipgraph" or rel.startswith(".chipgraph/"):
            continue
        if _hash_or_empty(workspace / rel) != seed_hashes.get(rel):
            changed_outside.append(rel)
    return tuple(sorted(changed_in_allowed)), tuple(sorted(changed_outside))


def copy_outputs_back(
    workspace: Path,
    user_root: Path,
    allowed_writes: tuple[str, ...],
    seed_hashes: dict[str, str],
) -> CopyBackResult:
    """Copy allowed outputs from workspace to user's tree, atomically.

    Refuses to copy if:
    - The user's copy changed since the seed (concurrent edit protection)
    - There's a symlink anywhere on the output path (security)

    Returns CopyBackResult with details of what was copied or refused.
    """
    copied: list[str] = []
    refused: list[str] = []
    symlinks: list[str] = []

    # First pass: check all outputs for safety and conflicts
    outputs_to_copy: list[str] = []
    for rel in allowed_writes:
        ws_path = workspace / rel
        if not ws_path.exists() and not ws_path.is_symlink():
            continue  # Output doesn't exist in workspace - skip

        # Check for symlinks (on the workspace side and on the user's side) and for anything
        # that is not a regular file
        safe, _reason = _is_safe_output(workspace, rel)
        if safe and (user_root / rel).parent.exists():
            safe, _reason = _is_safe_output(user_root, rel)
        if safe and not ws_path.is_file():
            safe = False
        if not safe:
            symlinks.append(rel)
            continue

        # Check if user's tree changed since seed
        user_path = user_root / rel
        if seed_hashes.get(rel):
            # File existed at seed time - check if user changed it
            if user_path.is_file() and not user_path.is_symlink():
                current_user_hash = hash_file(user_path)
                if current_user_hash != seed_hashes[rel]:
                    refused.append(rel)
                    continue
            elif user_path.exists() or user_path.is_symlink():
                # User's file type changed (became dir/symlink)
                refused.append(rel)
                continue
            elif not user_path.exists():
                # User deleted the file - that counts as a change
                refused.append(rel)
                continue
        else:
            # File didn't exist at seed time
            if user_path.exists() or user_path.is_symlink():
                # User created a file there concurrently
                refused.append(rel)
                continue

        outputs_to_copy.append(rel)

    # If anything was refused, don't copy anything (atomic)
    if refused or symlinks:
        reason_parts = []
        if refused:
            reason_parts.append(f"user's tree changed since seed for: {', '.join(refused)}")
        if symlinks:
            reason_parts.append(f"symlinks in output paths: {', '.join(symlinks)}")
        return CopyBackResult(
            ok=False,
            copied=(),
            refused=tuple(refused),
            symlinks=tuple(symlinks),
            reason="; ".join(reason_parts),
        )

    # Second pass: stage every output next to its destination, then rename them into place
    # (a rename is atomic per file; a failure while staging leaves the user's tree untouched)
    staged: list[tuple[Path, Path, str]] = []
    try:
        for rel in outputs_to_copy:
            user_path = user_root / rel
            user_path.parent.mkdir(parents=True, exist_ok=True)
            temp = user_path.with_name(f".{user_path.name}.chipgraph-new")
            temp.unlink(missing_ok=True)
            shutil.copy2(workspace / rel, temp)
            staged.append((temp, user_path, rel))
    except OSError as exc:
        for temp, _dest, _rel in staged:
            temp.unlink(missing_ok=True)
        return CopyBackResult(ok=False, reason=f"could not stage the outputs: {exc}")
    for temp, dest, rel in staged:
        os.replace(temp, dest)
        copied.append(rel)

    return CopyBackResult(ok=True, copied=tuple(copied))


def check_overlapping_writes(
    write_sets: dict[str, tuple[str, ...]],
) -> dict[str, list[str]]:
    """Check if any two tasks' write sets overlap.

    Returns a dict mapping each overlapping path to the list of task IDs that write it.
    Empty dict means no overlaps.
    """
    # Map each path to the tasks that write it
    path_to_tasks: dict[str, list[str]] = {}
    for task_id, writes in write_sets.items():
        for path in writes:
            key = path.casefold()  # Case-insensitive for filesystem safety
            path_to_tasks.setdefault(key, []).append(task_id)

    # Find overlaps
    overlaps: dict[str, list[str]] = {}
    for path, tasks in path_to_tasks.items():
        if len(tasks) > 1:
            overlaps[path] = sorted(tasks)

    return overlaps


def max_parallel_limit(profile: Profile) -> int:
    """Get the max parallel tasks limit from the profile."""
    return profile.fanout.effective_max_parallel()


# --- Step 3: Best-of-N support -----------------------------------------------------------


def candidate_id(task_id: str, candidate: int, round_num: int = 1) -> str:
    """The task id suffix for a best-of-N candidate: `<task_id>#r<round>-<candidate>`.

    For round 1, this produces `<task_id>#r1-1`, `<task_id>#r1-2`, etc.
    """
    return f"{task_id}#r{round_num}-{candidate}"


def parse_candidate_id(candidate_task_id: str) -> tuple[str, int, int]:
    """Parse a candidate task id into (base_task_id, candidate_number, round_number).

    Supports both old format `<task_id>#<n>` (round 1 assumed) and new format
    `<task_id>#r<round>-<candidate>`.

    Raises ValueError if not a candidate id.
    """
    if "#" not in candidate_task_id:
        raise ValueError(f"{candidate_task_id!r} is not a candidate id (no #)")
    base, suffix = candidate_task_id.rsplit("#", 1)

    # Try new format: r<round>-<candidate>
    if suffix.startswith("r") and "-" in suffix:
        try:
            round_part, cand_part = suffix[1:].split("-", 1)
            return base, int(cand_part), int(round_part)
        except ValueError:
            pass

    # Fall back to old format: just <candidate> (round 1 assumed)
    try:
        return base, int(suffix), 1
    except ValueError:
        raise ValueError(f"{candidate_task_id!r} is not a candidate id (invalid format)") from None


def is_candidate_id(task_id: str) -> bool:
    """Whether a task id is a candidate id (contains #)."""
    return "#" in task_id


def best_of_n(profile: Profile, rule_id: str, instance_params: dict[str, object]) -> int | None:
    """Get the best-of-N value for a rule instance, or None if not opted in.

    A rule opts in by:
    1. Instance parameter `best_of: <n>`
    2. Profile's `select.rules[rule_id]`
    """
    # Check instance param first
    raw = instance_params.get("best_of")
    if raw is not None:
        try:
            n = int(str(raw))
        except (ValueError, TypeError):
            return None  # Invalid value, not opted in
        return n if n >= 1 else None

    # Check profile's select.rules
    return profile.select.rules.get(rule_id)


__all__ = [
    "CopyBackResult",
    "best_of_n",
    "candidate_id",
    "check_overlapping_writes",
    "copy_outputs_back",
    "create_workspace",
    "diff_workspace",
    "input_paths_for",
    "is_candidate_id",
    "max_parallel_limit",
    "parse_candidate_id",
    "remove_workspace",
    "should_isolate",
    "sweep_workspaces",
    "workspace_dir_for",
    "workspace_tmp_root",
]
