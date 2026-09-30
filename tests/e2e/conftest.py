"""Shared fixtures for `tests/e2e`: a fresh, git-initialized copy of `examples/tinysoc`.

`examples/tinysoc` lives inside the chipgraph git repository itself, so running
chipgraph directly against it would resolve the *chipgraph* repo as the project root
(`AppContext.find_repo_root` walks up to the nearest `.git`) and would write run state
into a tracked directory. Every e2e test instead copies the example into a fresh
`tmp_path` and `git init`s it there, so chipgraph always sees `tmp_path` as both the
profile root and the repo root, and nothing lands in the chipgraph checkout.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chipgraph.cli import app

_EXAMPLE_ROOT = Path(__file__).resolve().parents[2] / "examples" / "tinysoc"


def _tool_missing(name: str) -> bool:
    return shutil.which(name) is None


requires_eda_tools = pytest.mark.skipif(
    _tool_missing("verilator") or _tool_missing("make"),
    reason="verilator and/or make not found on PATH",
)


def copy_tinysoc(dest: Path) -> Path:
    """Copy `examples/tinysoc` into `dest` and `git init` it there.

    Returns `dest`. The copy is a plain file copy (not a git clone/worktree): the
    example's own git history, if any, is irrelevant here.
    """
    shutil.copytree(_EXAMPLE_ROOT, dest)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=dest, check=True)
    subprocess.run(["git", "config", "user.email", "e2e@example.invalid"], cwd=dest, check=True)
    subprocess.run(["git", "config", "user.name", "e2e"], cwd=dest, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial import of tinysoc"], cwd=dest, check=True)
    # An existing project's first step (DESIGN.md 6.4): baseline what is on main, so the
    # `spec:{block}` gates pass, then commit the decisions like a lead would.
    result = CliRunner().invoke(
        app, ["-C", str(dest), "baseline", "--confirm", "--by", "e2e", "--note", "e2e setup"]
    )
    assert result.exit_code == 0, result.output
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=dest, check=True)
    # Build the Design Model the cross checks (`trace`/`connect`/`hardcode`, M1-07) read;
    # `chipgraph check` runs every configured adapter, and these error without a model
    # (the README documents `ingest` before `check`). The model is state, not committed.
    ingest = CliRunner().invoke(app, ["-C", str(dest), "ingest"])
    assert ingest.exit_code == 0, ingest.output
    return dest


@pytest.fixture
def tinysoc(tmp_path: Path) -> Path:
    """A fresh, git-initialized copy of `examples/tinysoc` under `tmp_path`."""
    return copy_tinysoc(tmp_path / "tinysoc")


__all__ = ["copy_tinysoc", "requires_eda_tools", "tinysoc"]
