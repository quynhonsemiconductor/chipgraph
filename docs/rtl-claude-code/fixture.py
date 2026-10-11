"""The M2-04 acceptance fixture: a tinysoc copy with one new module to write, an approved
plan for it, and a set of style exemplars.

`make_rtl_project(dest, exemplars="a")` copies `examples/tinysoc` to `dest` (a new git
repo; the example itself is not changed) and

- adds a block `pwm`: its MAS (`TINY_PWM_MAS.md` here, as `doc/specs/TINY_PWM_MAS.md`),
  an entry in `chip.yml` and a filelist `filelists/pwm.f` naming `rtl/tiny_pwm.sv`,
  which does not exist yet;
- turns on the built-in pack `digital-rtl` only (tinysoc's own pack has a `human` rule
  that owns each block's top RTL file), with `plan_check` and the RTL rule's checks:
  `format` (the whitespace-only `fmt_trim.py` as `style.formatter`, so the formatter
  runs but the measured style is the author's; verible would impose its own), `lint`
  (`make lint BLOCK={block}`), `naming` (tinysoc's rule), `style`, `hardcode`;
- copies the exemplar set `exemplars/<set>/` to `style/<set>/` and lists those files as
  the profile's `style.exemplars`;
- plants a fingerprint comment, `chipgraph-rtl-fingerprint`, in every testbench file
  under `dv/` (`report.py` looks for it, and every other long testbench line, in all the
  subagent saw);
- ingests the Design Model, has the planner's task write the fixture plan
  (`pwm.plan.yml`, through a scripted in-process runtime: no model), approves the gate
  `plan:pwm` and runs `plan_expand`, so `digital-rtl/rtl_module[block=pwm,module=tiny_pwm]`
  is ready; then sets the profile's runtime (`claude-code` by default) and commits.

`tiers` maps the role's model tiers in the copy's profile.

Command line, used by `run.sh`::

    python docs/rtl-claude-code/fixture.py DEST --exemplars a|b [--medium-model M]
        [--large-model M]
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
EXAMPLE = REPO / "examples" / "tinysoc"

BLOCK = "pwm"
MODULE = "tiny_pwm"
RULE = "digital-rtl/rtl_module"
TASK = f"{RULE}[block={BLOCK},module={MODULE}]"
OUTPUT = f"rtl/{MODULE}.sv"
PLAN_RULE = "digital-rtl/plan"
EXPAND = f"digital-rtl/plan_expand[block={BLOCK}]"
SPEC = f"doc/specs/TINY_{BLOCK.upper()}_MAS.md"
EXEMPLAR_SETS = ("a", "b")
FINGERPRINT = "chipgraph-rtl-fingerprint"
"""The comment planted in each testbench file (see the module docstring)."""
REQS = ("REQ-PWM-001", "REQ-PWM-002", "REQ-PWM-003", "REQ-PWM-004")

CHECKS = {
    "plan_check": {"use": "plan_check"},
    "format": {"use": "format"},
    "style": {"use": "style"},
}
"""Adapters the fixture adds; `lint`, `naming` and `hardcode` are tinysoc's own."""


def exemplar_files(name: str) -> dict[str, str]:
    """The exemplar set `name` as the copy has it: `style/<name>/<file>` to its text."""
    src = HERE / "exemplars" / name
    return {
        f"style/{name}/{path.name}": path.read_text(encoding="utf-8")
        for path in sorted(src.glob("*.sv"))
    }


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def plant_fingerprints(root: Path) -> list[str]:
    """Add a fingerprint comment to every file under `dv/`; the files changed."""
    planted = []
    for path in sorted((root / "dv").rglob("*")):
        if not path.is_file():
            continue
        mark = "#" if path.suffix == ".py" else "//"
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        path.write_text(text + f"\n{mark} {FINGERPRINT}: {rel}\n", encoding="utf-8")
        planted.append(rel)
    return planted


def _add_block(root: Path) -> None:
    chip_path = root / "chip.yml"
    chip = yaml.safe_load(chip_path.read_text(encoding="utf-8"))
    chip["blocks"].append(
        {"name": BLOCK, "bus": "regbus", "base": 0x8, "size": 4, "clock": "clk", "reset": "rst_n"}
    )
    for bus in chip.get("buses", []):
        if bus.get("name") == "regbus":
            bus["slaves"] = [*bus["slaves"], BLOCK]
    chip_path.write_text(yaml.safe_dump(chip, sort_keys=False), encoding="utf-8")
    shutil.copy(HERE / "TINY_PWM_MAS.md", root / SPEC)
    (root / "filelists" / f"{BLOCK}.f").write_text(f"{OUTPUT}\n", encoding="utf-8")


def _profile(root: Path, exemplars: str, tiers: dict[str, str] | None) -> dict[str, Any]:
    path = root / ".chipgraph.yml"
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    profile["packs"] = ["digital-rtl"]
    profile["layout"]["rtl"] = "rtl/tiny_{block}*.sv"
    profile["adapters"].update(CHECKS)
    profile["style"] = {
        "exemplars": list(exemplar_files(exemplars)),
        "formatter": {"use": "cmd", "cmd": ["python3", "scripts/fmt_trim.py", "{file}"]},
    }
    profile["blocks"][BLOCK] = {}
    profile["runtime"] = "generic"  # the plan is written in process, then the real runtime
    if tiers:
        profile.setdefault("models", {})["tiers"] = dict(tiers)
    return profile


@dataclass
class _Planner:
    """A scripted in-process runtime: the planner's task writes the fixture plan."""

    root: Path
    name: str = "generic"
    tasks: list[Any] = field(default_factory=list)

    async def run_task(self, task: Any) -> Any:
        from chipgraph.core.contracts import AgentResult

        self.tasks.append(task)
        text = (HERE / f"{BLOCK}.plan.yml").read_text(encoding="utf-8")
        for rel in task.allowed_writes:
            out = self.root / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
        return AgentResult(status="done", files_written=task.allowed_writes)


def _build(root: Path, target: str) -> Any:
    from chipgraph.app.build import make_scheduler
    from chipgraph.app.context import AppContext

    ctx = AppContext.load(root)
    ctx.registry.register("runtime", "generic", _Planner(root))
    return asyncio.run(make_scheduler(ctx, target).run(target))


def approve_plan(root: Path) -> None:
    """Record a person's approval of the gate `plan:pwm` (as `chipgraph approve` does)."""
    from chipgraph.app.build import load_rules
    from chipgraph.app.context import AppContext
    from chipgraph.core.engine import gate as gate_mod
    from chipgraph.packs.digital_rtl.plan.resolver import plan_gates

    ctx = AppContext.load(root)
    gate = plan_gates(load_rules(ctx), ctx.require_profile().profile.blocks)[BLOCK]
    gate_mod.approve(
        ctx.review, ctx.store, gate.gate_id, gate.instance, by="reviewer", decision="approve"
    )


def make_rtl_project(
    dest: Path,
    *,
    exemplars: str = "a",
    tiers: dict[str, str] | None = None,
    runtime: str = "claude-code",
    profile: dict[str, Any] | None = None,
) -> Path:
    """Build the fixture at `dest` (see the module docstring); `profile` is merged over
    the fixture's profile (top-level keys) before the first commit."""
    from chipgraph.app.context import AppContext
    from chipgraph.app.ingest import run_ingest

    if exemplars not in EXEMPLAR_SETS:
        raise ValueError(f"exemplars must be one of {EXEMPLAR_SETS}, not {exemplars!r}")
    shutil.copytree(EXAMPLE, dest, ignore=shutil.ignore_patterns(".chipgraph/state"))
    with (dest / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write(".chipgraph/state/\n")
    _add_block(dest)
    plant_fingerprints(dest)
    for rel, text in exemplar_files(exemplars).items():
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_text(text, encoding="utf-8")
    (dest / "scripts").mkdir(exist_ok=True)
    shutil.copy(HERE / "fmt_trim.py", dest / "scripts" / "fmt_trim.py")
    data = _profile(dest, exemplars, tiers)
    for key, value in (profile or {}).items():
        data[key] = value
    (dest / ".chipgraph.yml").write_text(yaml.safe_dump(data, sort_keys=False))

    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "config", "user.email", "m2-04@example.invalid")
    _git(dest, "config", "user.name", "m2-04")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "tinysoc with a pwm block to write")
    run_ingest(AppContext.load(dest))

    first = _build(dest, EXPAND)  # the planner writes the plan; the gate then waits
    if first.waiting_gate != (EXPAND,):
        raise RuntimeError(f"the fixture plan did not reach its gate: {first}")
    approve_plan(dest)
    second = _build(dest, EXPAND)
    if EXPAND not in second.done:
        raise RuntimeError(f"plan_expand did not run: {second}")

    data["runtime"] = runtime
    (dest / ".chipgraph.yml").write_text(yaml.safe_dump(data, sort_keys=False))
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "approved plan for pwm")
    return dest


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dest", type=Path)
    parser.add_argument("--exemplars", choices=EXEMPLAR_SETS, default="a")
    parser.add_argument("--medium-model", default=None, help="model for tier medium")
    parser.add_argument("--large-model", default=None, help="model for tier large")
    args = parser.parse_args(argv)
    tiers = {
        tier: model
        for tier, model in (("medium", args.medium_model), ("large", args.large_model))
        if model
    }
    make_rtl_project(args.dest, exemplars=args.exemplars, tiers=tiers or None)
    print(args.dest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
