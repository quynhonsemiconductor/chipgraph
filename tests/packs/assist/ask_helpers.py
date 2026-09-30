"""Helpers for the M1-13 `/ask` tests (`tests/packs/assist/test_ask*.py`).

Not a `conftest.py` (unique test module names, no shared conftest here). `ingested_tinysoc`
copies `examples/tinysoc` into a temp dir, `git init -b main`s and commits it, optionally
rewrites its profile, and runs `run_ingest`, so the model store and the `/ask` documents
live in the copy and never in the chipgraph checkout.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from chipgraph.app.context import AppContext
from chipgraph.app.ingest import run_ingest
from chipgraph.packs.assist.ask._project import AskProject

EXAMPLE_ROOT = Path(__file__).resolve().parents[3] / "examples" / "tinysoc"
EVAL_QUESTIONS = Path(__file__).resolve().parents[3] / "evals" / "ask" / "tinysoc.yml"


def copy_tinysoc(dest: Path, *, profile_extra: str = "") -> Path:
    """Copy tinysoc to `dest` (a git repo), appending `profile_extra` to its profile."""
    shutil.copytree(EXAMPLE_ROOT, dest)
    if profile_extra:
        profile = dest / ".chipgraph.yml"
        profile.write_text(profile.read_text() + "\n" + profile_extra)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=dest, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    identity = ["-c", "user.email=ask@example.invalid", "-c", "user.name=ask"]
    subprocess.run(["git", *identity, "commit", "-q", "-m", "tinysoc"], cwd=dest, check=True)
    return dest


def ingested_tinysoc(dest: Path, *, profile_extra: str = "") -> AppContext:
    """A tinysoc copy at `dest`, ingested; returns its `AppContext`."""
    copy_tinysoc(dest, profile_extra=profile_extra)
    ctx = AppContext.load(dest)
    run_ingest(ctx)
    return ctx


def project(ctx: AppContext) -> AskProject:
    return AskProject.load(ctx)
