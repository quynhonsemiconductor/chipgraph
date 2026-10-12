"""The M2-12 acceptance fixture: a tinysoc copy whose sample wrapper is left to write.

`make_wrapper_project(dest)` copies `examples/tinysoc` to `dest` (a new git repo; the
example itself is not changed) and

- deletes the wrapper the example ships: its source `rtl/emacs/tiny_fifo_wrap.src.sv` and
  its expansion `rtl/tiny_fifo_wrap.sv`; the vendored IP (`vendor/vendor_fifo/`) and its
  `vendor/MANIFEST.sha256` stay, and are what the task wraps;
- turns on the built-in pack `digital-rtl` only (tinysoc's own pack has a `human` rule
  that owns each block's top RTL file); `blocks.top.wrappers` already lists the wrapper
  in the example's profile, and so do the adapters `wrap_check`, `lint` (which also lints
  the wrapper once it exists), `naming` and `vendor_guard`;
- ingests the Design Model, then sets the profile's runtime (`claude-code` by default)
  and commits, so `digital-rtl/wrapper[block=top,wrapper=tiny_fifo_wrap]` is ready.

`tiers` maps the role's model tiers in the copy's profile.

Command line, used by `run.sh`::

    python docs/wrapper-claude-code/fixture.py DEST [--medium-model M] [--large-model M]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
EXAMPLE = REPO / "examples" / "tinysoc"

BLOCK = "top"
WRAPPER = "tiny_fifo_wrap"
VENDOR_MODULE = "vendor_fifo"
RULE = "digital-rtl/wrapper"
GEN_RULE = "digital-rtl/wrapper_gen"
TASK = f"{RULE}[block={BLOCK},wrapper={WRAPPER}]"
GEN_TASK = f"{GEN_RULE}[block={BLOCK},wrapper={WRAPPER}]"
SRC = f"rtl/emacs/{WRAPPER}.src.sv"
OUTPUT = f"rtl/{WRAPPER}.sv"
IP_DIR = "vendor"
MANIFEST = "vendor/MANIFEST.sha256"
IP_FILES = ("vendor/vendor_fifo/vendor_fifo.sv", "vendor/vendor_fifo/vendor_fifo.f")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def make_wrapper_project(
    dest: Path, *, tiers: dict[str, str] | None = None, runtime: str = "claude-code"
) -> Path:
    """Build the fixture at `dest` (see the module docstring)."""
    from chipgraph.app.context import AppContext
    from chipgraph.app.ingest import run_ingest

    shutil.copytree(EXAMPLE, dest, ignore=shutil.ignore_patterns(".chipgraph/state"))
    with (dest / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write(".chipgraph/state/\n")
    (dest / SRC).unlink()
    (dest / OUTPUT).unlink()
    path = dest / ".chipgraph.yml"
    profile: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    profile["packs"] = ["digital-rtl"]
    profile["runtime"] = runtime
    if tiers:
        profile.setdefault("models", {})["tiers"] = dict(tiers)
    path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")

    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "config", "user.email", "m2-12@example.invalid")
    _git(dest, "config", "user.name", "m2-12")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "tinysoc with a vendor wrapper to write")
    run_ingest(AppContext.load(dest))
    return dest


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dest", type=Path)
    parser.add_argument("--medium-model", default=None, help="model for tier medium")
    parser.add_argument("--large-model", default=None, help="model for tier large")
    args = parser.parse_args(argv)
    tiers = {
        tier: model
        for tier, model in (("medium", args.medium_model), ("large", args.large_model))
        if model
    }
    make_wrapper_project(args.dest, tiers=tiers or None)
    print(args.dest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
