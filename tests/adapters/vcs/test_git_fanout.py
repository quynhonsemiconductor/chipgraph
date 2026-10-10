"""Tests for the git adapter's fan-out methods (`FanoutVcs`)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from chipgraph.adapters.vcs.git import GitVcs, VcsError
from chipgraph.core.engine.fanout import FanoutVcs

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def _repo(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "a.txt").write_text("a\n")
    (root / "gone.txt").write_text("gone\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return _git(root, "rev-parse", "HEAD").strip()


def test_git_vcs_implements_the_fanout_port() -> None:
    assert isinstance(GitVcs(), FanoutVcs)


def test_workspace_commit_diff_patch_apply_branch(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base = _repo(root)
    vcs = GitVcs()
    runs = tmp_path / "runs"

    ws = vcs.create_branch_workspace(root, base, "b000", runs)
    assert ws == runs / "b000"
    assert [p.resolve() for p in vcs.list_workspaces(root)] == [ws.resolve()]
    assert vcs.commit_all(ws, "nothing") == base  # nothing changed: HEAD

    (ws / "a.txt").write_text("a2\n")
    (ws / "gone.txt").unlink()
    (ws / "bin").mkdir()
    (ws / "bin/run.sh").write_text("#!/bin/sh\n")
    (ws / "bin/run.sh").chmod(0o755)
    (ws / "blob.bin").write_bytes(bytes(range(256)))
    commit = vcs.commit_all(ws, "work")
    changes = {c.path: c for c in vcs.diff(ws, base, commit)}
    assert {p: c.status for p, c in changes.items()} == {
        "a.txt": "modified",
        "bin/run.sh": "added",
        "blob.bin": "added",
        "gone.txt": "deleted",
    }
    assert changes["bin/run.sh"].new_mode == "100755"
    assert changes["gone.txt"].new_mode is None

    patch = vcs.patch(ws, base, commit, sorted(changes))
    join = vcs.create_branch_workspace(root, base, "join", runs)
    assert vcs.apply_patch(join, patch) is None
    assert (join / "blob.bin").read_bytes() == bytes(range(256))
    assert os.access(join / "bin/run.sh", os.X_OK)
    merged = vcs.commit_all(join, "merged", paths=sorted(changes))
    assert vcs.tree_hash(join, merged) == vcs.tree_hash(ws, commit)

    vcs.create_branch(root, "chipgraph/r1/x", merged)
    assert _git(root, "rev-parse", "chipgraph/r1/x").strip() == merged
    assert _git(root, "rev-parse", "HEAD").strip() == base
    with pytest.raises(VcsError):
        vcs.create_branch(root, "chipgraph/r1/x", merged)  # never overwritten

    for path in (ws, join):
        vcs.remove(path)
        assert not path.exists()
    vcs.remove(ws)  # already gone: a no-op
    vcs.prune(root)
    assert vcs.list_workspaces(root) == ()


def test_apply_patch_reports_a_conflict_and_changes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base = _repo(root)
    vcs = GitVcs()
    ws = vcs.create_branch_workspace(root, base, "b000", tmp_path / "runs")
    (ws / "a.txt").write_text("from branch\n")
    commit = vcs.commit_all(ws, "work")
    patch = vcs.patch(ws, base, commit, ["a.txt"])

    join = vcs.create_branch_workspace(root, base, "join", tmp_path / "runs")
    (join / "a.txt").write_text("someone else\n")
    error = vcs.apply_patch(join, patch)
    assert error is not None and "a.txt" in error
    assert (join / "a.txt").read_text() == "someone else\n"
    assert vcs.apply_patch(join, b"") is None


def test_commit_runs_no_hooks_and_ignores_redirecting_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    base = _repo(root)
    hooks = root / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    fired = tmp_path / "fired"
    for name in ("pre-commit", "commit-msg", "post-commit", "post-checkout"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\necho {name} >> '{fired}'\nexit 1\n")
        hook.chmod(0o755)
    index_before = (root / ".git" / "index").read_bytes()
    monkeypatch.setenv("GIT_INDEX_FILE", str(root / ".git" / "index"))
    monkeypatch.setenv("GIT_DIR", str(root / ".git"))
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.delenv(key, raising=False)

    vcs = GitVcs()
    ws = vcs.create_branch_workspace(root, base, "b000", tmp_path / "runs")
    (ws / "new.txt").write_text("x\n")
    commit = vcs.commit_all(ws, "work")
    assert not fired.exists()
    assert (root / ".git" / "index").read_bytes() == index_before
    monkeypatch.delenv("GIT_INDEX_FILE")
    monkeypatch.delenv("GIT_DIR")
    assert _git(root, "log", "-1", "--format=%an <%ae>", commit).strip() == (
        "chipgraph <chipgraph@localhost>"
    )
    vcs.remove(ws)


def test_commit_all_with_paths_and_ignored_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _repo(root)
    (root / ".gitignore").write_text("out/\n")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-q", "-m", "ignore")
    base = _git(root, "rev-parse", "HEAD").strip()
    vcs = GitVcs()
    ws = vcs.create_branch_workspace(root, base, "b000", tmp_path / "runs")
    (ws / "out").mkdir()
    (ws / "out/gen.txt").write_text("gen\n")
    (ws / "a.txt").write_text("changed\n")
    (ws / "gone.txt").unlink()
    commit = vcs.commit_all(ws, "only some", paths=["out/gen.txt", "gone.txt", "missing.txt"])
    assert {c.path for c in vcs.diff(ws, base, commit)} == {"out/gen.txt", "gone.txt"}
    vcs.remove(ws)


def test_bad_workspace_names_and_existing_paths_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base = _repo(root)
    vcs = GitVcs()
    with pytest.raises(VcsError):
        vcs.create_branch_workspace(root, base, "Bad Name", tmp_path / "runs")
    ws = vcs.create_branch_workspace(root, base, "b000", tmp_path / "runs")
    with pytest.raises(VcsError):
        vcs.create_branch_workspace(root, base, "b000", tmp_path / "runs")
    vcs.remove(ws)
