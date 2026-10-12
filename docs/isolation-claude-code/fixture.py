"""The M2-11c acceptance fixture: a tinysoc copy with isolation enabled for the `timer`
block, and optionally best-of-N selection.

`make_isolation_project(dest, origin, ...)` copies `examples/tinysoc` to `dest` (a new git
repo) and:

- uses the **real** `timer` block with its MAS (TINY_TIMER_MAS.md, REQ-TIM-001..005) exactly
  as `tests/packs/digital_rtl/plan_helpers.py` and `rtl_target_helpers.py` do: the timer's
  existing human RTL is stubbed out so the planner/authors build it;
- enables `fanout.isolate: true` so all agent tasks run in isolated workspaces;
- sets `max_parallel: 4` so modules can run in parallel;
- uses real Verilator lint (as `docs/rtl-target-claude-code/fixture.py` does);
- uses `review: {use: file}` (offline, no GitHub);
- plants fingerprints for independence checking (RTL files get the TB fingerprint, TB files
  get the RTL fingerprint, so report.py can tell which author saw what).

**best_of mode** (env var `BEST_OF=1` or `--best-of`): adds `select.rules` to enable best-of-2
selection on `digital-rtl/rtl_module`. This is a separate profile variant for phase 2 testing.

The fixture does NOT pre-write a plan: the real planner (Sonnet) writes it. The reference plan
(tests/packs/digital_rtl/plan_fixtures/timer.plan.yml) has modules tiny_timer_counter,
tiny_timer_irq (independent), and tiny_timer (depends on both).

Command line, used by `run.sh`::

    python docs/isolation-claude-code/fixture.py DEST --origin BARE.git
        [--medium-model M] [--large-model M] [--best-of]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
EXAMPLE = REPO / "examples" / "tinysoc"

BLOCK = "timer"
TARGET = f"rtl:{BLOCK}"
PLAN_TARGET = f"plan:{BLOCK}"
PLAN_TASK = f"digital-rtl/plan[block={BLOCK}]"
EXPAND = f"digital-rtl/plan_expand[block={BLOCK}]"
TB_TASK = f"dv/tb_module[block={BLOCK}]"
REVIEW_TASK = f"digital-rtl/review[block={BLOCK}]"
OPEN_PR = f"digital-rtl/open_pr[block={BLOCK}]"
PR_GATE = f"digital-rtl/pr_gate[block={BLOCK}]"
PLAN_FILE = f"plan/{BLOCK}.plan.yml"
APPROVED_PLAN = f"plan/{BLOCK}.approved.json"
TB_FILE = f"dv/{BLOCK}/test_{BLOCK}.py"
REVIEW_FILE = f"reports/review/{BLOCK}.json"
PR_RECORD = f"pr/{BLOCK}.json"
PR_APPROVED = f"pr/{BLOCK}.approved.json"
TOP = f"tiny_{BLOCK}"
REQS = ("REQ-TIM-001", "REQ-TIM-002", "REQ-TIM-003", "REQ-TIM-004", "REQ-TIM-005")

STATE_FILE = "fixture.json"
"""What the fixture records next to the copy (`<dest>/../fixture.json`): the commit and
index of the user's tree before the run, for `report.py`."""


def _sibling(name: str, path: Path) -> ModuleType:
    """`path` imported under the unique module name `name` (other acceptance scripts
    import their own `fixture.py`)."""
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


# Import fingerprint constants and exemplar functions from sibling fixtures
rtl_fx = _sibling("rtl_acceptance_fixture", REPO / "docs" / "rtl-claude-code" / "fixture.py")
tb_fx = _sibling("tb_acceptance_fixture", REPO / "docs" / "tb-claude-code" / "fixture.py")
RTL_FINGERPRINT: str = tb_fx.FINGERPRINT
"""Planted in every RTL file: a testbench author that sees it saw RTL."""
TB_FINGERPRINT: str = rtl_fx.FINGERPRINT
"""Planted in every testbench file: an RTL author that sees it saw a testbench."""

EXEMPLARS = "a"
"""The exemplar set to use for style checking."""

LINT = {
    "use": "cmd",
    "cmd": [
        "verilator",
        "--lint-only",
        "-Wall",
        "-y",
        "rtl",
        "rtl/{module}.sv",
        "--top-module",
        "{module}",
    ],
    "parser": "verilator",
}
"""Per-module lint: the module's own file, its dependencies found in `rtl/` by name."""


def module_task(module: str) -> str:
    """The task id of a planned module's RTL."""
    return f"digital-rtl/rtl_module[block={BLOCK},module={module}]"


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _stub_timer_rtl(root: Path) -> None:
    """Remove the existing timer RTL so the planner/authors must build it.

    This is the key: the MAS exists (doc/specs/TINY_TIMER_MAS.md) but the RTL doesn't,
    so the planner must decompose it into modules and the authors must write them.
    """
    timer_rtl = root / "rtl" / "tiny_timer.sv"
    if timer_rtl.is_file():
        timer_rtl.unlink()
    # Isolation and best-of-N need independent modules. The planner sees the requirements and
    # the interface, not the MAS prose, so the split is a requirement of the MAS (the human's
    # spec); without it Sonnet plans one module for a block this small.
    mas = root / "doc" / "specs" / "TINY_TIMER_MAS.md"
    text = mas.read_text(encoding="utf-8")
    req = (
        "`REQ-TIM-006` The block is built as three modules: `tiny_timer_counter` (the COUNT "
        "register), `tiny_timer_irq` (the COMPARE match and the pending flag) and the top "
        "`tiny_timer`; the counter and the irq module do not depend on each other, and a "
        "plan with a single module is not acceptable.\n\n"
    )
    assert "# 8. Instances" in text
    mas.write_text(
        text.replace("# 8. Instances", "## 7.4 Partitioning\n\n" + req + "# 8. Instances", 1),
        encoding="utf-8",
    )
    # Also remove any existing dv/timer directory for a clean start
    timer_dv = root / "dv" / "timer"
    if timer_dv.is_dir():
        shutil.rmtree(timer_dv)


def _profile(
    dest: Path,
    tiers: dict[str, str] | None,
    best_of: bool,
) -> dict[str, Any]:
    """Build the profile for isolation testing."""
    data: dict[str, Any] = yaml.safe_load((dest / ".chipgraph.yml").read_text(encoding="utf-8"))
    data["packs"] = ["digital-rtl", "dv"]
    data["runtime"] = "claude-code"
    data["layout"]["rtl"] = "rtl/tiny_{block}*.sv"

    # Enable isolation
    data["fanout"] = {"isolate": True, "max_parallel": 4}

    # Adapters: real Verilator lint, plan_check, format, style, tb_static
    adapters = data["adapters"]
    adapters["plan_check"] = {"use": "plan_check"}
    adapters["format"] = {"use": "format"}
    adapters["style"] = {"use": "style"}
    adapters["lint"] = dict(LINT)
    adapters["tb_static"] = {"use": "tb_static", "top": "tiny_{block}"}

    # Style exemplars
    data["style"] = {
        "exemplars": list(rtl_fx.exemplar_files(EXEMPLARS)),
        "formatter": {"use": "cmd", "cmd": ["python3", "scripts/fmt_trim.py", "{file}"]},
    }

    # Only the timer block
    data["blocks"] = {BLOCK: {}}

    # Review: file adapter (offline)
    data["review"] = {"use": "file"}

    # Best-of-N: only if requested (phase 2)
    if best_of:
        # Enable best-of-2 on all rtl_module tasks for the timer block
        data["select"] = {"rules": {"digital-rtl/rtl_module": 2}}

    if tiers:
        data.setdefault("models", {})["tiers"] = dict(tiers)

    return data


def plant_fingerprints(dest: Path) -> None:
    """Plant fingerprints for independence checking.

    RTL files get the TB fingerprint (so an RTL author who sees it saw testbench code).
    Testbench files get the RTL fingerprint (so a TB author who sees it saw RTL code).
    """
    # Plant TB fingerprint in RTL files (rtl_fx.plant_fingerprints does this)
    rtl_fx.plant_fingerprints(dest)

    # Plant RTL fingerprint in testbench files (tb_fx.plant_fingerprints does this)
    tb_fx.plant_fingerprints(dest)


def make_isolation_project(
    dest: Path,
    *,
    origin: Path | None = None,
    tiers: dict[str, str] | None = None,
    best_of: bool = False,
    ingest: bool = True,
) -> Path:
    """Build the isolation fixture at `dest` (see the module docstring)."""
    from chipgraph.app.context import AppContext
    from chipgraph.app.ingest import run_ingest

    shutil.copytree(EXAMPLE, dest, ignore=shutil.ignore_patterns(".chipgraph/state"))
    with (dest / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write(".chipgraph/state/\n")

    # Stub out the timer RTL so planner/authors must build it
    _stub_timer_rtl(dest)

    # Plant fingerprints for independence checking
    plant_fingerprints(dest)

    # Copy exemplars and formatter script
    for rel, text in rtl_fx.exemplar_files(EXEMPLARS).items():
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_text(text, encoding="utf-8")
    (dest / "scripts").mkdir(exist_ok=True)
    shutil.copy(REPO / "docs" / "rtl-claude-code" / "fmt_trim.py", dest / "scripts" / "fmt_trim.py")

    # Write the profile
    data = _profile(dest, tiers, best_of)
    (dest / ".chipgraph.yml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    # Initialize git
    git(dest, "init", "-q", "-b", "main")
    git(dest, "config", "user.email", "m2-11c@example.invalid")
    git(dest, "config", "user.name", "m2-11c")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", f"tinysoc with isolation-enabled block {BLOCK}")

    # Set up origin if provided
    if origin is not None:
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        git(dest, "remote", "add", "origin", str(origin))
        git(dest, "push", "-q", "origin", "main")

    # Ingest the Design Model
    if ingest:
        run_ingest(AppContext.load(dest))

    # Record the fixture state for report.py
    state = {
        "schema_version": 1,
        "head": git(dest, "rev-parse", "HEAD"),
        "index": git(dest, "ls-files", "-s"),
        "review": data["review"]["use"],
        "isolate": True,
        "best_of": best_of,
        "rtl_fingerprint": RTL_FINGERPRINT,
        "tb_fingerprint": TB_FINGERPRINT,
    }
    (dest.parent / STATE_FILE).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return dest


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dest", type=Path)
    parser.add_argument("--origin", type=Path, default=None, help="a bare repo to create")
    parser.add_argument("--medium-model", default=None, help="model for tier medium")
    parser.add_argument("--large-model", default=None, help="model for tier large")
    parser.add_argument(
        "--best-of", action="store_true", help="enable best-of-2 selection on rtl_module"
    )
    args = parser.parse_args(argv)
    tiers = {
        tier: model
        for tier, model in (("medium", args.medium_model), ("large", args.large_model))
        if model
    }
    make_isolation_project(
        args.dest,
        origin=args.origin,
        tiers=tiers or None,
        best_of=args.best_of,
    )
    print(args.dest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
