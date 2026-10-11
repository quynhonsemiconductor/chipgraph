"""Shared by the M2-03 planner tests: a tinysoc copy set up for the Planner, the fixture
plans, the downstream rule over `plan.modules`, and a scripted agent runtime.

Not a test module itself; imported by the `test_plan_*.py` modules.
"""

from __future__ import annotations

import copy
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from chipgraph.app.context import AppContext
from chipgraph.app.ingest import run_ingest
from chipgraph.core.contracts import AgentResult
from chipgraph.core.engine import gate as gate_mod
from chipgraph.core.plugin_api.types import AgentTask
from chipgraph.packs.digital_rtl.plan.resolver import plan_gates

REPO = Path(__file__).resolve().parents[3]
TINYSOC = REPO / "examples" / "tinysoc"
FIXTURES = Path(__file__).resolve().parent / "plan_fixtures"

STUB_RULE = "digital-rtl/rtl_module"
"""The rule over `plan.modules` the M2-03 tests expand: M2-04's RTL rule. With `stub=True`
its checks pass trivially and `PlanRuntime` writes a placeholder module for it."""

RTL_CHECKS = ("format", "lint", "naming", "style", "hardcode")
"""The checks of `digital-rtl/rtl_module`."""
_PASS = {"use": "cmd", "cmd": ["python3", "-c", "pass"]}


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def fixture_plan(block: str) -> dict[str, Any]:
    """The fixture plan of `block` (`timer` or `gpio`), as data."""
    return yaml.safe_load((FIXTURES / f"{block}.plan.yml").read_text(encoding="utf-8"))


def write_plan(root: Path, block: str, plan: dict[str, Any] | str | None = None) -> Path:
    """Write `block`'s plan (default: its fixture) to `plan/<block>.plan.yml`."""
    path = root / "plan" / f"{block}.plan.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if plan is None:
        text = (FIXTURES / f"{block}.plan.yml").read_text(encoding="utf-8")
    elif isinstance(plan, str):
        text = plan
    else:
        text = yaml.safe_dump(plan, sort_keys=False)
    path.write_text(text, encoding="utf-8")
    return path


def overlapping(plan: dict[str, Any]) -> dict[str, Any]:
    """`plan` with its second module also writing the first module's first file."""
    bad = copy.deepcopy(plan)
    bad["modules"][1]["writes"].append(dict(bad["modules"][0]["writes"][0]))
    return bad


def write_extra_rule(root: Path, output: str) -> None:
    """Add a test-only pack `extra` with one `gen` rule over `plan.modules` writing `output`,
    and turn it on in the profile."""
    pack = root / ".chipgraph" / "packs" / "extra"
    (pack / "rules").mkdir(parents=True, exist_ok=True)
    (pack / "pack.yml").write_text(
        yaml.safe_dump({"name": "extra", "version": "0.1.0", "provides": {"rules": ["rules"]}})
    )
    rule = {
        "rule": "module_extra",
        "kind": "gen",
        "description": "Test only: one more file per planned module.",
        "foreach": "plan.modules",
        "outputs": [output],
        "run": {"use": "cmd", "args": {"cmd": ["python3", "-c", "pass"]}},
    }
    (pack / "rules" / "module_extra.yml").write_text(yaml.safe_dump(rule, sort_keys=False))
    path = root / ".chipgraph.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["packs"] = [*data["packs"], "extra"]
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def tinysoc_project(
    dest: Path,
    *,
    packs: Sequence[str] = ("digital-rtl",),
    stub: bool = False,
    runtime: str | None = None,
    profile: dict[str, Any] | None = None,
    ingest: bool = True,
) -> Path:
    """A git copy of tinysoc set up for the Planner, ingested.

    The profile turns on `packs` (not tinysoc's own pack, whose `human` rule owns each
    block's top RTL file), adds an `rtl` layout path (`rtl/tiny_{block}*.sv`) and the
    `plan_check` adapter, and merges `profile` over that. With `stub`, the checks of
    `digital-rtl/rtl_module` (`foreach: plan.modules`) are commands that always pass.
    """
    shutil.copytree(TINYSOC, dest)
    data = yaml.safe_load((dest / ".chipgraph.yml").read_text(encoding="utf-8"))
    data["packs"] = list(packs)
    data["layout"]["rtl"] = "rtl/tiny_{block}*.sv"
    data["adapters"]["plan_check"] = {"use": "plan_check"}
    if stub:
        for check in RTL_CHECKS:
            data["adapters"][check] = dict(_PASS)
    if runtime is not None:
        data["runtime"] = runtime
    for key, value in (profile or {}).items():
        data[key] = value
    (dest / ".chipgraph.yml").write_text(yaml.safe_dump(data, sort_keys=False))
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "config", "user.email", "t@example.invalid")
    _git(dest, "config", "user.name", "t")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "init")
    if ingest:
        run_ingest(AppContext.load(dest))
    return dest


def approve_plan(root: Path, block: str, *, decision: str = "approve") -> None:
    """Record a person's decision on gate `plan:<block>` (as `chipgraph approve` does)."""
    ctx = AppContext.load(root)
    from chipgraph.app.build import load_rules

    gate = plan_gates(load_rules(ctx), ctx.require_profile().profile.blocks)[block]
    gate_mod.approve(
        ctx.review,
        ctx.store,
        gate.gate_id,
        gate.instance,
        by="reviewer",
        decision=decision,  # type: ignore[arg-type]
    )


@dataclass
class PlanRuntime:
    """A scripted `AgentRuntime`: planner call n writes `texts[n]` (the last repeats) to the
    output; a task of a planned module (param `module`) writes a placeholder module."""

    root: Path
    texts: Sequence[str]
    name: str = "fake"
    tasks: list[AgentTask] = field(default_factory=list)
    modules: list[AgentTask] = field(default_factory=list)

    async def run_task(self, task: AgentTask) -> AgentResult:
        module = task.instance.params.get("module")
        if module is not None:
            self.modules.append(task)
            text = f"module {module};\nendmodule\n"
        else:
            text = self.texts[min(len(self.tasks), len(self.texts) - 1)]
            self.tasks.append(task)
        for rel in task.allowed_writes:
            out = self.root / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
        return AgentResult(status="done", files_written=task.allowed_writes)


__all__ = [
    "FIXTURES",
    "REPO",
    "RTL_CHECKS",
    "STUB_RULE",
    "TINYSOC",
    "PlanRuntime",
    "approve_plan",
    "fixture_plan",
    "overlapping",
    "tinysoc_project",
    "write_extra_rule",
    "write_plan",
]
