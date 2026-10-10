"""M2-07: a fan-out never touches the user's tree, and cleans up on any exit."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fanout_helpers import git, init_repo, linked_worktrees, snapshot, writer

from chipgraph.adapters.vcs.git import GitVcs
from chipgraph.core.engine.fanout import (
    REGISTRY_FILE,
    BranchContext,
    BranchOutcome,
    FanoutBranch,
    FanoutRegistry,
    find_stale,
    run_fanout,
    sweep_stale,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_WORKER = Path(__file__).parent / "_fanout_kill_worker.py"
BASE_FILES = {"README.md": "hello\n", "spec/regs.md": "regs v1\n"}


def _dirty_repo(tmp_path: Path) -> Path:
    """A repo with every kind of local state: a stash, staged, modified and untracked."""
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)
    (root / "README.md").write_text("stashed\n")
    git(root, "stash", "-q")
    (root / "spec/regs.md").write_text("regs v2, staged\n")
    git(root, "add", "spec/regs.md")
    (root / "README.md").write_text("modified, not staged\n")
    (root / "spec/new.md").write_text("untracked\n")
    return root


def _assert_clean(root: Path, tmp_root: Path) -> None:
    assert linked_worktrees(root) == []
    assert not tmp_root.exists() or not any(tmp_root.iterdir())
    assert not (root / ".git" / "worktrees").exists() or not any(
        (root / ".git" / "worktrees").iterdir()
    )


def _branches_reading_inputs() -> list[FanoutBranch]:
    async def from_inputs(ctx: BranchContext) -> BranchOutcome:
        text = (ctx.path / "spec/regs.md").read_text() + (ctx.path / "spec/new.md").read_text()
        (ctx.path / "rtl").mkdir(exist_ok=True)
        (ctx.path / "rtl/a.v").write_text(text)
        (ctx.path / "README.md").write_text("the branch may edit its copy\n")
        return BranchOutcome(ok=True)

    return [
        FanoutBranch(
            id="a",
            run=from_inputs,
            write_set=("rtl/a.v", "README.md"),
            inputs=("spec/regs.md", "spec/new.md", "README.md"),
        ),
        FanoutBranch(id="b", run=writer({"rtl/b.v": "b\n"}), write_set=("rtl/b.v",)),
    ]


# --- the user's tree --------------------------------------------------------------------


def test_the_users_tree_index_head_and_stash_are_untouched(tmp_path: Path) -> None:
    root = _dirty_repo(tmp_path)
    refs_before = git(root, "for-each-ref", "--format=%(refname)")
    before = snapshot(root)

    result = asyncio.run(
        run_fanout(
            _branches_reading_inputs(),
            root=root,
            vcs=GitVcs(),
            tmp_root=tmp_path / "t",
            run_id="r1",
        )
    )
    assert result.ok, result.message
    assert snapshot(root) == before
    # The only new ref is the result branch.
    refs_after = git(root, "for-each-ref", "--format=%(refname)")
    assert set(refs_after.split()) - set(refs_before.split()) == {"refs/heads/chipgraph/r1/fanout"}
    # Seeded files were copied (still in the user's tree, unchanged), and the branch's
    # edit of its copy of README.md went to the result branch only.
    assert (root / "spec/new.md").read_text() == "untracked\n"
    assert git(root, "show", f"{result.branch}:rtl/a.v") == "regs v2, staged\nuntracked\n"
    assert git(root, "show", f"{result.branch}:README.md") == "the branch may edit its copy\n"
    _assert_clean(root, tmp_path / "t")


def test_the_users_tree_is_untouched_after_a_failing_branch(tmp_path: Path) -> None:
    root = _dirty_repo(tmp_path)
    before = snapshot(root)

    async def boom(ctx: BranchContext) -> BranchOutcome:
        (ctx.path / "rtl").mkdir(exist_ok=True)
        (ctx.path / "rtl/b.v").write_text("half\n")
        raise RuntimeError("the branch crashed")

    branches = [
        *_branches_reading_inputs()[:1],
        FanoutBranch(id="b", run=boom, write_set=("rtl/b.v",)),
    ]
    result = asyncio.run(run_fanout(branches, root=root, vcs=GitVcs(), tmp_root=tmp_path / "t"))
    assert result.status == "failed"
    b = result.branch_result("b")
    assert b is not None and b.status == "error" and "the branch crashed" in b.message
    assert snapshot(root) == before
    _assert_clean(root, tmp_path / "t")


# --- cleanup on every exit --------------------------------------------------------------


class _BrokenMerge(GitVcs):
    def apply_patch(self, path: Path, patch: bytes) -> str | None:
        raise OSError("disk full")


def test_cleanup_after_an_exception_in_the_merge(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)
    branches = [FanoutBranch(id="a", run=writer({"rtl/a.v": "a\n"}), write_set=("rtl/a.v",))]
    with pytest.raises(OSError, match="disk full"):
        asyncio.run(run_fanout(branches, root=root, vcs=_BrokenMerge(), tmp_root=tmp_path / "t"))
    _assert_clean(root, tmp_path / "t")


def test_cleanup_after_an_in_process_cancel(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)
    started = asyncio.Event()
    workspaces: list[Path] = []

    async def hang(ctx: BranchContext) -> BranchOutcome:
        workspaces.append(ctx.path)
        started.set()
        await asyncio.sleep(600)
        return BranchOutcome(ok=True)

    branches = [
        FanoutBranch(id="a", run=hang, write_set=("rtl/a.v",)),
        FanoutBranch(id="b", run=hang, write_set=("rtl/b.v",)),
    ]

    async def main() -> None:
        task = asyncio.create_task(
            run_fanout(branches, root=root, vcs=GitVcs(), tmp_root=tmp_path / "t")
        )
        await started.wait()
        assert workspaces[0].is_dir()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert workspaces and not any(p.exists() for p in workspaces)
    _assert_clean(root, tmp_path / "t")


def test_cleanup_after_keyboard_interrupt_inside_a_branch(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)
    other_cancelled = []

    async def interrupt(ctx: BranchContext) -> BranchOutcome:
        await asyncio.sleep(0.05)
        raise KeyboardInterrupt

    async def slow(ctx: BranchContext) -> BranchOutcome:
        try:
            await asyncio.sleep(600)
        except asyncio.CancelledError:
            other_cancelled.append(ctx.branch_id)
            raise
        return BranchOutcome(ok=True)

    branches = [
        FanoutBranch(id="a", run=interrupt, write_set=("rtl/a.v",)),
        FanoutBranch(id="b", run=slow, write_set=("rtl/b.v",)),
    ]
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(run_fanout(branches, root=root, vcs=GitVcs(), tmp_root=tmp_path / "t"))
    assert other_cancelled == ["b"]
    _assert_clean(root, tmp_path / "t")


def test_a_sigkilled_fanout_is_swept_by_the_next_one(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)
    tmp_root = tmp_path / "t"
    marker = tmp_path / "marker.txt"
    proc = subprocess.Popen([sys.executable, str(_WORKER), str(root), str(tmp_root), str(marker)])
    try:
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "the worker never started its branch"
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:  # pragma: no cover - safety net
            proc.kill()
            proc.wait(timeout=10)

    workspace = Path(marker.read_text())
    assert workspace.is_dir()  # the kill left it behind
    assert len(linked_worktrees(root)) == 2
    (run_dir,) = list(tmp_root.iterdir())
    registry = FanoutRegistry.model_validate_json((run_dir / REGISTRY_FILE).read_text())
    assert registry.pid == proc.pid
    stale = find_stale(root, tmp_root=tmp_root)
    assert [s.run_id for s in stale] == ["killed"]

    # The next fan-out sweeps it before it starts.
    branches = [FanoutBranch(id="a", run=writer({"rtl/a.v": "a\n"}), write_set=("rtl/a.v",))]
    result = asyncio.run(run_fanout(branches, root=root, vcs=GitVcs(), tmp_root=tmp_root))
    assert result.ok
    assert not workspace.exists()
    assert not run_dir.exists()
    _assert_clean(root, tmp_root)


def test_the_sweeper_leaves_a_live_owner_alone(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    init_repo(root, BASE_FILES)
    tmp_root = tmp_path / "t"
    seen: dict[str, Any] = {}

    async def look(ctx: BranchContext) -> BranchOutcome:
        # While this fan-out runs, its own run directory is not stale.
        seen["stale"] = find_stale(root, tmp_root=tmp_root)
        seen["swept"] = sweep_stale(root, GitVcs(), tmp_root=tmp_root)
        seen["exists"] = ctx.path.is_dir()
        return BranchOutcome(ok=True)

    branches = [FanoutBranch(id="a", run=look, write_set=("rtl/a.v",))]
    result = asyncio.run(run_fanout(branches, root=root, vcs=GitVcs(), tmp_root=tmp_root))
    assert result.ok
    assert seen == {"stale": (), "swept": (), "exists": True}
