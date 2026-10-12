"""A fresh eval project per problem and phase: a small chipgraph project of its own.

`rtl_project(problem, dest)` is what the RTL Author works in (`digital-rtl/rtl_module`):

    .chipgraph.yml          packs [digital-rtl]; QSoC's layout; the rule's checks
    chip.yml                the block, its clock and reset (chip-yaml)
    doc/specs/QNSC_<B>_MAS.md        the MAS, as committed in QSoC (the fixture's text)
    design/<b>/<b>.f        the module alone (the block's own filelist lists vendor IP)
    style/<exemplar>        the style exemplars (`style.exemplars`)
    scripts/fmt_trim.py     a whitespace-only formatter (`style.formatter`)
    eval/interface.yml      the module's interface (the problem's)
    plan/<b>.plan.yml       the approved-plan fixture, written by a scripted planner task,
                            approved at the gate `plan:<b>` and expanded (`plan_expand`)

The module's file does not exist: the task writes it. No testbench is anywhere in it.

`tb_project(problem, dest)` is what the testbench Author works in (`dv/tb_module`): the
same, with packs [dv] and the `tb_static` check, no plan, no exemplar and **no RTL at
all** (the interface it needs is in the model).

Both are git repositories (branch main, committed) with the Design Model ingested. One
thing is not ingest's: the block's spec ports. The MAS interface tables of these blocks
list the wrapper's ports (and, for ROM, the image's), with `i_bus_axi_aw_*` style rows,
not the leaf module's; so after ingest the block's `port:spec.<block>.*` entities are
replaced by the problem's interface (provenance `eval/interface.yml`), which both rules
then show and check as the block's interface.

`runtime` is the profile's runtime once the project is ready (`claude-code` for both
runtimes: the fake one calls the same MCP service functions in process); `tiers` are the
profile's `models.tiers` (a real run).
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from .fixtures import strip_header
from .problems import Problem, ProblemSet, load_problems

Phase = Literal["rtl", "tb"]

REPO = Path(__file__).resolve().parents[2]
FORMATTER = REPO / "docs" / "rtl-claude-code" / "fmt_trim.py"
INTERFACE_FILE = "eval/interface.yml"
NAMING_RULES = "org:qnsc/naming-v1.yml"
EXTRACTOR = "chipgraph-eval"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=eval@example.invalid", "-c", "user.name=eval", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def spec_path(problem: Problem) -> str:
    return problem.files.spec


def filelist_path(problem: Problem) -> str:
    return f"design/{problem.block}/{problem.block}.f"


def profile(problem: Problem, phase: Phase, problems: ProblemSet) -> dict[str, Any]:
    """The eval project's `.chipgraph.yml` (before its runtime is set)."""
    block = problem.block
    data: dict[str, Any] = {
        "project": f"qsoc-eval-{problem.id}",
        "packs": ["digital-rtl"] if phase == "rtl" else ["dv"],
        "spec": {
            "chip": {"path": "chip.yml", "format": "chip-yaml"},
            "requirements": {"id_pattern": "{BLOCK}_\\d{3}"},
        },
        "layout": {
            "filelist": "design/{block}/{block}.f",
            "spec": "doc/specs/QNSC_{BLOCK}_MAS.md",
            "rtl": "design/{block}/rtl/*.sv",
            "tb": ["dv/{block}/**"],
        },
        "adapters": {},
        "blocks": {block: {}},
        "runtime": "generic",
    }
    if Path(problem.files.spec).name != f"QNSC_{block.upper()}_MAS.md":
        data["blocks"][block] = {"layout": {"spec": problem.files.spec}}
    if phase == "rtl":
        data["style"] = {
            "exemplars": list(problems.exemplar_paths),
            "formatter": {"use": "cmd", "cmd": ["python3", "scripts/fmt_trim.py", "{file}"]},
        }
        data["adapters"] = {
            "plan_check": {"use": "plan_check"},
            "format": {"use": "format"},
            # QSoC's lint (flow/lint/lint_all.sh): -Wall, warnings reported, errors fail.
            "lint": {
                "use": "cmd",
                "cmd": "verilator --lint-only -Wall -Wno-fatal -F design/{block}/{block}.f",
                "parser": "verilator",
            },
            "naming": {
                "use": "naming",
                "rules": NAMING_RULES,
                "scope": ["design/{block}/rtl/**/*.sv"],
            },
            "style": {"use": "style"},
            "hardcode": {"use": "hardcode", "scope": ["design/**/rtl/**/*.sv"]},
        }
    else:
        data["adapters"] = {"tb_static": {"use": "tb_static"}}
    return data


def chip_yml(problem: Problem) -> dict[str, Any]:
    return {
        "project": f"qsoc-eval-{problem.id}",
        "data_width": 32,
        "addr_width": 32,
        "blocks": [{"name": problem.block, "clock": problem.clock, "reset": problem.reset.name}],
        "clocks": [{"name": problem.clock}],
        "resets": [{"name": problem.reset.name, "active_low": problem.reset.active_low}],
    }


def interface_yml(problem: Problem) -> str:
    lines = [
        f"# The interface of {problem.module} (evals/rtl/problems.yml, problem {problem.id}):",
        f"# the block {problem.block}'s ports in this project's Design Model.",
        "ports:",
    ]
    lines += [
        f"  - {{ name: {p.name}, direction: {p.direction}, width: {p.width} }}"
        for p in problem.interface
    ]
    return "\n".join(lines) + "\n"


def replace_spec_ports(root: Path, problem: Problem) -> int:
    """Swap the block's spec ports in the stored Design Model for the problem's interface;
    returns how many spec ports ingest had found."""
    from chipgraph.core.model import DesignModel, PortEntity
    from chipgraph.core.model.provenance import Provenance
    from chipgraph.core.model.store import ModelStore, default_model_db_path

    store = ModelStore(default_model_db_path(root))
    model = store.read()
    prefix = f"port:spec.{problem.block}."
    kept = [e for e in model.entities.values() if not e.key.startswith(prefix)]
    dropped = len(model.entities) - len(kept)
    ports = [
        PortEntity(
            key=f"{prefix}{port.name}",
            name=port.name,
            direction=port.direction,
            width=port.width,
            module=None,
            source=Provenance(file=INTERFACE_FILE, line=index + 4, extractor=EXTRACTOR),
            attrs={
                "block": f"block:{problem.block}",
                "origin": "spec",
                "description": f"{problem.module} port (eval interface)",
            },
        )
        for index, port in enumerate(problem.interface)
    ]
    keys = {e.key for e in kept} | {p.key for p in ports}
    relations = [r for r in model.relations if r.src in keys and r.dst in keys]
    store.write(DesignModel.build([*kept, *ports], relations), build_inputs_hash=None)
    return dropped


@dataclass
class _Planner:
    """A scripted in-process runtime: the planner task writes the plan fixture."""

    root: Path
    text: str
    name: str = "generic"
    tasks: list[Any] = field(default_factory=list)

    async def run_task(self, task: Any) -> Any:
        from chipgraph.core.contracts import AgentResult

        self.tasks.append(task)
        for rel in task.allowed_writes:
            out = self.root / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(self.text, encoding="utf-8")
        return AgentResult(status="done", files_written=task.allowed_writes)


def _build(root: Path, target: str, planner: _Planner) -> Any:
    from chipgraph.app.build import make_scheduler
    from chipgraph.app.context import AppContext

    ctx = AppContext.load(root)
    ctx.registry.register("runtime", "generic", planner)
    return asyncio.run(make_scheduler(ctx, target).run(target))


def _approve_plan(root: Path, block: str) -> None:
    """Record a person's approval of the gate `plan:<block>` (as `chipgraph approve`)."""
    from chipgraph.app.build import load_rules
    from chipgraph.app.context import AppContext
    from chipgraph.core.engine import gate as gate_mod
    from chipgraph.packs.digital_rtl.plan.resolver import plan_gates

    ctx = AppContext.load(root)
    gate = plan_gates(load_rules(ctx), ctx.require_profile().profile.blocks)[block]
    gate_mod.approve(
        ctx.review, ctx.store, gate.gate_id, gate.instance, by="eval", decision="approve"
    )


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _base(problem: Problem, dest: Path, phase: Phase, problems: ProblemSet) -> dict[str, Any]:
    from chipgraph.app.context import AppContext
    from chipgraph.app.ingest import run_ingest

    if dest.exists():
        raise FileExistsError(f"{dest} exists")
    dest.mkdir(parents=True)
    _write(dest, ".gitignore", ".chipgraph/state/\n")
    _write(dest, "chip.yml", yaml.safe_dump(chip_yml(problem), sort_keys=False))
    _write(dest, spec_path(problem), strip_header(problem.fixture(problem.files.spec)))
    _write(dest, INTERFACE_FILE, interface_yml(problem))
    if phase == "rtl":
        module_file = Path(problem.rtl_output)
        listed = module_file.relative_to(Path(filelist_path(problem)).parent).as_posix()
        _write(dest, filelist_path(problem), f"{listed}\n")
        _write(dest, f"{module_file.parent.as_posix()}/.gitkeep", "")
        for exemplar, rel in zip(problems.exemplars, problems.exemplar_paths, strict=True):
            _write(dest, rel, strip_header(exemplar.fixture))
        _write(dest, "scripts/fmt_trim.py", FORMATTER.read_text(encoding="utf-8"))
    data = profile(problem, phase, problems)
    _write(dest, ".chipgraph.yml", yaml.safe_dump(data, sort_keys=False))
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", f"eval project {problem.id} ({phase})")
    run_ingest(AppContext.load(dest))
    replace_spec_ports(dest, problem)
    return data


def _finish(dest: Path, data: dict[str, Any], runtime: str, tiers: dict[str, str] | None) -> None:
    data["runtime"] = runtime
    if tiers:
        data["models"] = {"tiers": dict(tiers)}
    _write(dest, ".chipgraph.yml", yaml.safe_dump(data, sort_keys=False))
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "ready")


def rtl_project(
    problem: Problem,
    dest: Path,
    *,
    runtime: str = "claude-code",
    tiers: dict[str, str] | None = None,
    problems: ProblemSet | None = None,
) -> Path:
    """The RTL Author's project for `problem` at `dest` (see the module docstring)."""
    problems = problems or load_problems()
    data = _base(problem, dest, "rtl", problems)
    planner = _Planner(dest, problem.plan_path.read_text(encoding="utf-8"))
    expand = f"digital-rtl/plan_expand[block={problem.block}]"
    first = _build(dest, expand, planner)
    if first.waiting_gate != (expand,):
        raise RuntimeError(f"{problem.id}: the plan fixture did not reach its gate: {first}")
    _approve_plan(dest, problem.block)
    second = _build(dest, expand, planner)
    if expand not in second.done:
        raise RuntimeError(f"{problem.id}: plan_expand did not run: {second}")
    _finish(dest, data, runtime, tiers)
    return dest


def tb_project(
    problem: Problem,
    dest: Path,
    *,
    runtime: str = "claude-code",
    tiers: dict[str, str] | None = None,
    problems: ProblemSet | None = None,
) -> Path:
    """The testbench Author's project for `problem` at `dest` (no RTL in it)."""
    problems = problems or load_problems()
    data = _base(problem, dest, "tb", problems)
    _finish(dest, data, runtime, tiers)
    return dest


def make_project(
    problem: Problem,
    phase: Phase,
    dest: Path,
    *,
    runtime: str = "claude-code",
    tiers: dict[str, str] | None = None,
) -> Path:
    build = rtl_project if phase == "rtl" else tb_project
    return build(problem, dest, runtime=runtime, tiers=tiers)


def target(problem: Problem, phase: Phase) -> str:
    return problem.rtl_target if phase == "rtl" else problem.tb_target


def output(problem: Problem, phase: Phase) -> str:
    return problem.rtl_output if phase == "rtl" else problem.tb_output


__all__ = [
    "INTERFACE_FILE",
    "Phase",
    "chip_yml",
    "make_project",
    "output",
    "profile",
    "replace_spec_ports",
    "rtl_project",
    "target",
    "tb_project",
]


def _main() -> None:  # pragma: no cover - a manual helper
    import sys

    pid, phase, dest = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    make_project(load_problems().problem(pid), phase, dest)  # type: ignore[arg-type]
    print(dest)


if __name__ == "__main__":  # pragma: no cover
    _main()
