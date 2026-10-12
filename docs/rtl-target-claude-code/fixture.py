"""The M2-11b acceptance fixture: a tinysoc copy with a MAS-only block `pwm`, set up for the
target `rtl:pwm` (plan, gate, RTL per module, testbench, Critic, branch or PR, gate).

`make_target_project(dest, origin=...)` copies `examples/tinysoc` to `dest` (a new git
repo; the example itself is not changed) and

- adds the block `pwm` exactly as the M2-04 fixture does (`docs/rtl-claude-code/`): its
  MAS `TINY_PWM_MAS.md` as `doc/specs/TINY_PWM_MAS.md`, an entry in `chip.yml`, a filelist.
  Nothing else of `pwm` exists: no plan, no RTL, no testbench;
- turns on the built-in packs `digital-rtl` and `dv` (not tinysoc's own pack, whose
  `human` rule owns each block's top RTL file), runtime `claude-code`, and the checks:
  `plan_check`; for each module `format` (the whitespace-only `fmt_trim.py` as
  `style.formatter`), `lint` (Verilator `--lint-only -Wall` on the module, its
  dependencies found in `rtl/` with `-y`), `naming` (tinysoc's rule), `style` (the M2-04
  exemplar set `a` as `style.exemplars`), `hardcode` (tinysoc's); for the testbench
  `tb_static` (no simulator);
- `review: {use: file}` (offline: `open_pr` builds the branch, nothing is pushed, a person
  approves `pr:pwm` with `chipgraph approve`), or, with `sandbox_repo`, `review: {use:
  github}` against that repository with `sandbox_base` as the PR's base branch;
- plants fingerprints: `chipgraph-tb-fingerprint` in the body of every RTL file
  (`docs/tb-claude-code/fixture.py`) and `chipgraph-rtl-fingerprint` in every testbench
  file under `dv/` (`docs/rtl-claude-code/fixture.py`), so `report.py` can tell whether
  the testbench author saw RTL or the RTL author saw a testbench;
- commits on `main`, ingests the Design Model, and adds `origin`: a local bare repository
  `origin` (with `main` pushed), or the sandbox's `https://github.com/<repo>.git`.

`tiers` maps the role tiers in the copy's profile (`medium` for the authors, `large` for
the planner and the Critic).

Command line, used by `run.sh`::

    python docs/rtl-target-claude-code/fixture.py DEST --origin BARE.git
        [--medium-model M] [--large-model M] [--sandbox-repo OWNER/NAME --sandbox-base B]
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

BLOCK = "pwm"
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
EXEMPLARS = "a"
REQS = ("REQ-PWM-001", "REQ-PWM-002", "REQ-PWM-003", "REQ-PWM-004")

STATE_FILE = "fixture.json"
"""What the fixture records next to the copy (`<dest>/../fixture.json`): the commit and
index of the user's tree before the run, for `report.py`."""

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


rtl_fx = _sibling("rtl_acceptance_fixture", REPO / "docs" / "rtl-claude-code" / "fixture.py")
tb_fx = _sibling("tb_acceptance_fixture", REPO / "docs" / "tb-claude-code" / "fixture.py")
RTL_FINGERPRINT: str = tb_fx.FINGERPRINT
"""Planted in every RTL file: a testbench author that sees it saw RTL."""
TB_FINGERPRINT: str = rtl_fx.FINGERPRINT
"""Planted in every testbench file: an RTL author that sees it saw a testbench."""


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _profile(
    root: Path,
    tiers: dict[str, str] | None,
    sandbox_repo: str | None,
    sandbox_base: str | None,
) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((root / ".chipgraph.yml").read_text(encoding="utf-8"))
    data["packs"] = ["digital-rtl", "dv"]
    data["runtime"] = "claude-code"
    data["layout"]["rtl"] = "rtl/tiny_{block}*.sv"
    adapters = data["adapters"]
    adapters["plan_check"] = {"use": "plan_check"}
    adapters["format"] = {"use": "format"}
    adapters["style"] = {"use": "style"}
    adapters["lint"] = dict(LINT)
    adapters["tb_static"] = {"use": "tb_static", "top": "tiny_{block}"}
    data["style"] = {
        "exemplars": list(rtl_fx.exemplar_files(EXEMPLARS)),
        "formatter": {"use": "cmd", "cmd": ["python3", "scripts/fmt_trim.py", "{file}"]},
    }
    data["blocks"][BLOCK] = {}
    if sandbox_repo is not None:
        data["review"] = {
            "use": "github",
            "repo": sandbox_repo,
            "base": sandbox_base or "main",
            "remote": "origin",
            "allow_gh_fallback": False,
        }
    else:
        data["review"] = {"use": "file"}
    if tiers:
        data.setdefault("models", {})["tiers"] = dict(tiers)
    return data


def make_target_project(
    dest: Path,
    *,
    origin: Path | None = None,
    tiers: dict[str, str] | None = None,
    sandbox_repo: str | None = None,
    sandbox_base: str | None = None,
    ingest: bool = True,
    lint: dict[str, Any] | None = None,
) -> Path:
    """Build the fixture at `dest` (see the module docstring). `lint` replaces the lint
    adapter (the tests use one that always passes where Verilator is not installed)."""
    from chipgraph.app.context import AppContext
    from chipgraph.app.ingest import run_ingest

    shutil.copytree(EXAMPLE, dest, ignore=shutil.ignore_patterns(".chipgraph/state"))
    with (dest / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write(".chipgraph/state/\n")
    rtl_fx._add_block(dest)
    tb_fx.plant_fingerprints(dest)
    rtl_fx.plant_fingerprints(dest)
    for rel, text in rtl_fx.exemplar_files(EXEMPLARS).items():
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_text(text, encoding="utf-8")
    (dest / "scripts").mkdir(exist_ok=True)
    shutil.copy(REPO / "docs" / "rtl-claude-code" / "fmt_trim.py", dest / "scripts" / "fmt_trim.py")
    data = _profile(dest, tiers, sandbox_repo, sandbox_base)
    if lint is not None:
        data["adapters"]["lint"] = dict(lint)
    (dest / ".chipgraph.yml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    git(dest, "init", "-q", "-b", "main")
    git(dest, "config", "user.email", "m2-11b@example.invalid")
    git(dest, "config", "user.name", "m2-11b")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "tinysoc with a MAS-only block pwm")
    if sandbox_repo is not None:
        git(dest, "remote", "add", "origin", f"https://github.com/{sandbox_repo}.git")
    elif origin is not None:
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        git(dest, "remote", "add", "origin", str(origin))
        git(dest, "push", "-q", "origin", "main")
    if ingest:
        run_ingest(AppContext.load(dest))
    state = {
        "schema_version": 1,
        "head": git(dest, "rev-parse", "HEAD"),
        "index": git(dest, "ls-files", "-s"),
        "review": data["review"]["use"],
    }
    (dest.parent / STATE_FILE).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return dest


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dest", type=Path)
    parser.add_argument("--origin", type=Path, default=None, help="a bare repo to create")
    parser.add_argument("--medium-model", default=None, help="model for tier medium")
    parser.add_argument("--large-model", default=None, help="model for tier large")
    parser.add_argument("--sandbox-repo", default=None, help="owner/name (SANDBOX=1 only)")
    parser.add_argument("--sandbox-base", default=None, help="the PR's base branch there")
    args = parser.parse_args(argv)
    tiers = {
        tier: model
        for tier, model in (("medium", args.medium_model), ("large", args.large_model))
        if model
    }
    make_target_project(
        args.dest,
        origin=args.origin,
        tiers=tiers or None,
        sandbox_repo=args.sandbox_repo,
        sandbox_base=args.sandbox_base,
    )
    print(args.dest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
