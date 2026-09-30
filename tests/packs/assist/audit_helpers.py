"""Helpers for the M1-20 `/audit` tests (`tests/packs/assist/test_audit.py`).

Not a `conftest.py` (unique test module names, no shared conftest here): the audit tests
import these directly. The core helper copies `examples/tinysoc` into a `tmp_path`,
`git init`s and commits it, so `chipgraph audit` sees the copy as both the profile root
and the repo root and never writes into the chipgraph checkout. Seeders inject one
error per DESIGN.md 4.8 layer (1, 4, 5) on the copy, matching the accept criterion.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from chipgraph.app.context import AppContext

_EXAMPLE_ROOT = Path(__file__).resolve().parents[3] / "examples" / "tinysoc"

# A real Verilator `%Error` (an undeclared signal), copied from `tests/e2e/test_tinysoc.py`:
# a genuine file:line lint error that only Verilator itself can catch (layer 4, `lint`).
_LINT_BUG_MARKER = "  assign pin_out = out_q;\n"
_LINT_BUG_LINE = "  assign pin_out = bogus_signal;\n"


def copy_tinysoc(dest: Path) -> Path:
    """Copy `examples/tinysoc` into `dest` and `git init -b main` + commit it."""
    shutil.copytree(_EXAMPLE_ROOT, dest)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=dest, check=True)
    subprocess.run(["git", "config", "user.email", "audit@example.invalid"], cwd=dest, check=True)
    subprocess.run(["git", "config", "user.name", "audit"], cwd=dest, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial import of tinysoc"], cwd=dest, check=True)
    return dest


def load_ctx(root: Path) -> AppContext:
    """A fresh `AppContext` for the project at `root`."""
    ctx = AppContext.load(root)
    ctx.require_profile()
    return ctx


def git_status(root: Path) -> str:
    """`git status --porcelain` output for `root`."""
    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout


def commit_all(root: Path, message: str) -> None:
    """Stage everything and commit it (used after baselining)."""
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=root, check=True)


# --- per-layer seeders --------------------------------------------------------------


def seed_layer1_overlap(root: Path) -> None:
    """Layer 1 (`cross_chip`): give `gpio` the same base as `timer` -> address overlap."""
    path = root / "chip.yml"
    text = path.read_text(encoding="utf-8")
    # gpio's `base: 0x4` clashes with timer's `base: 0x0` once set to 0x0.
    marker = "    base: 0x4"
    assert text.count(marker) == 1, "expected gpio base 0x4 exactly once"
    path.write_text(text.replace(marker, "    base: 0x0"), encoding="utf-8")


def seed_layer4_lint(root: Path) -> None:
    """Layer 4 (`lint`): a real Verilator error (undeclared signal) in `tiny_gpio.sv`."""
    path = root / "rtl" / "tiny_gpio.sv"
    text = path.read_text(encoding="utf-8")
    assert _LINT_BUG_MARKER in text
    path.write_text(text.replace(_LINT_BUG_MARKER, _LINT_BUG_MARKER + _LINT_BUG_LINE, 1))


def seed_layer5_trace(root: Path) -> None:
    """Layer 5 (`trace`): remove a declared REQ from a dv stub -> `req.no_test`."""
    path = root / "dv" / "test_tiny_timer.py"
    text = path.read_text(encoding="utf-8")
    assert "REQ-TIM-001" in text
    path.write_text(text.replace("REQ-TIM-001", "REQ-TIM-00X", 1), encoding="utf-8")


__all__ = [
    "commit_all",
    "copy_tinysoc",
    "git_status",
    "load_ctx",
    "seed_layer1_overlap",
    "seed_layer4_lint",
    "seed_layer5_trace",
]
