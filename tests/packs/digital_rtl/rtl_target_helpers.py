"""Shared by the M2-11a tests: a tinysoc copy set up for target `rtl:<block>`, a scripted
runtime for every agent of the chain, a bare git remote, and a fake GitHub that follows it.

Not a test module itself; imported by the `test_rtl_target_*.py` modules.

- `target_project`: tinysoc with the `digital-rtl` and `dv` packs, the checks of
  `digital-rtl/rtl_module` that always pass (Verilator's `lint` when it is on PATH), the
  `tb_static` check, and `review:` = the `file` adapter, or `github` against a fake API;
- `ChainRuntime`: the planner writes a plan (the texts it is given in turn), the RTL
  author a module, the testbench author a cocotb test, the Critic its review as JSON;
- `bare_remote`: a bare repository as the project's `origin`;
- `RemoteGitHub`: `FakeGitHub` whose branches are the bare remote's, so a PR can only be
  opened from a branch that was really pushed, and its head follows later pushes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from plan_helpers import FIXTURES, TINYSOC, approve_plan, tinysoc_project

_REVIEW_TESTS = Path(__file__).resolve().parents[2] / "adapters" / "review"
if str(_REVIEW_TESTS) not in sys.path:
    sys.path.insert(0, str(_REVIEW_TESTS))

from github_api_helpers import REPO, TOKEN, FakeGitHub  # noqa: E402

from chipgraph.app.build import make_scheduler  # noqa: E402
from chipgraph.app.context import AppContext  # noqa: E402
from chipgraph.core.contracts import AgentResult  # noqa: E402
from chipgraph.core.engine.scheduler import RunSummary  # noqa: E402
from chipgraph.core.plugin_api.types import AgentTask  # noqa: E402

HAS_VERILATOR = shutil.which("verilator") is not None and shutil.which("make") is not None

BLOCK = "timer"
MODULES = ("tiny_timer_counter", "tiny_timer_irq", "tiny_timer")
"""The modules of `plan_fixtures/timer.plan.yml`, in build order."""
RTL_FILES = tuple(f"rtl/{name}.sv" for name in MODULES)
TB_FILE = "dv/timer/test_timer.py"
REVIEW_FILE = "reports/review/timer.json"
PR_RECORD = "pr/timer.json"

PLAN = "digital-rtl/plan[block=timer]"
EXPAND = "digital-rtl/plan_expand[block=timer]"
REVIEW = "digital-rtl/review[block=timer]"
OPEN_PR = "digital-rtl/open_pr[block=timer]"
PR_GATE = "digital-rtl/pr_gate[block=timer]"
TB = "dv/tb_module[block=timer]"


def module_id(name: str) -> str:
    return f"digital-rtl/rtl_module[block=timer,module={name}]"


MODULE_IDS = tuple(module_id(name) for name in MODULES)

# --- what the scripted agents write ---------------------------------------------------------

_COUNTER = """\
// SPDX-License-Identifier: Apache-2.0
// The COUNT register: counts while enabled, loads on a write.
module tiny_timer_counter (
    input  logic        clk,
    input  logic        rst_n,
    input  logic        en,
    input  logic        load,
    input  logic [31:0] load_value,
    output logic [31:0] count
);

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      count <= 32'd0;
    end else if (load) begin
      count <= load_value;
    end else if (en) begin
      count <= count + 32'd1;
    end
  end

endmodule
"""

_IRQ = """\
// SPDX-License-Identifier: Apache-2.0
// The pending flag: set when COUNT reaches COMPARE while enabled, cleared by IRQ_CLR.
module tiny_timer_irq (
    input  logic        clk,
    input  logic        rst_n,
    input  logic        en,
    input  logic [31:0] count,
    input  logic [31:0] compare,
    input  logic        clr,
    output logic        irq
);

  logic irq_q;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      irq_q <= 1'b0;
    end else if (clr) begin
      irq_q <= 1'b0;
    end else if (en && count == compare) begin
      irq_q <= 1'b1;
    end
  end

  assign irq = irq_q;

endmodule
"""

TB_TEXT = '''\
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

COUNT = 0x0
COMPARE = 0x1
CTRL = 0x2


async def reset(dut) -> None:
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    dut.addr.value = 0
    dut.wr_en.value = 0
    dut.wdata.value = 0
    await ClockCycles(dut.clk, 2)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


async def write(dut, addr: int, data: int) -> None:
    dut.addr.value = addr
    dut.wdata.value = data
    dut.wr_en.value = 1
    await RisingEdge(dut.clk)
    dut.wr_en.value = 0


async def read(dut, addr: int) -> int:
    dut.addr.value = addr
    await RisingEdge(dut.clk)
    await ReadOnly()
    value = dut.rdata.value.to_unsigned()
    await RisingEdge(dut.clk)
    return value


@cocotb.test()
async def test_timer(dut) -> None:
    """The timer counts, loads, compares and clears."""
    # verifies: REQ-TIM-001, REQ-TIM-002, REQ-TIM-003, REQ-TIM-004, REQ-TIM-005
    await reset(dut)
    await write(dut, COUNT, 5)
    await write(dut, COMPARE, 8)
    await write(dut, CTRL, 1)
    await ClockCycles(dut.clk, 6)
    await ReadOnly()
    assert dut.irq.value == 1, "REQ-TIM-003: irq expected 1 once COUNT reaches COMPARE"
    await RisingEdge(dut.clk)
    await write(dut, CTRL, 3)
    assert await read(dut, CTRL) & 2 == 0, "REQ-TIM-004: pending expected cleared"
    assert await read(dut, 0x3) == 0, "REQ-TIM-005: offset 0x3 reads 0"
'''


def module_text(name: str) -> str:
    """What the scripted RTL author writes for module `name` (lint-clean on its own)."""
    if name == "tiny_timer_counter":
        return _COUNTER
    if name == "tiny_timer_irq":
        return _IRQ
    top = (TINYSOC / "rtl" / "tiny_timer.sv").read_text(encoding="utf-8")
    return top + "// Re-planned by chipgraph: the register block of the timer.\n"


def review_json(
    *,
    verdict: str = "changes_requested",
    comments: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """A Critic reply (a `ReviewReport`): by default one major and one minor comment."""
    if comments is None:
        comments = [
            {
                "id": "R1",
                "severity": "major",
                "category": "requirement",
                "file": "rtl/tiny_timer_irq.sv",
                "line": 16,
                "claim": "The pending flag is set when COUNT equals COMPARE, one cycle late.",
                "evidence": "doc/specs/TINY_TIMER_MAS.md:12 irq goes high after COUNT == COMPARE",
                "req_id": "REQ-TIM-003",
            },
            {
                "id": "R2",
                "severity": "minor",
                "category": "naming",
                "file": "rtl/tiny_timer_counter.sv",
                "line": 5,
                "claim": "Port `en` could be named `enable`.",
                "evidence": "rtl/tiny_timer_counter.sv:5 input logic en",
            },
        ]
    body = {
        "schema_version": 1,
        "target": BLOCK,
        "base": "main",
        "head": "HEAD",
        "reviewed": [*RTL_FILES, TB_FILE],
        "comments": list(comments),
        "summary": "The timer is split in three modules; one off-by-one cycle in the irq.",
        "verdict": verdict,
    }
    return json.dumps(body, indent=2) + "\n"


@dataclass
class ChainRuntime:
    """A scripted `AgentRuntime` for every agent of the chain, by rule id.

    The planner writes `plans[n]` (the last repeats); an RTL author, the module's text;
    the testbench author, `TB_TEXT`; the Critic, `review`. `calls` has every task's rule
    id in order; `tasks` the tasks themselves.
    """

    root: Path
    plans: Sequence[str]
    review: str = field(default_factory=review_json)
    name: str = "generic"
    tasks: list[AgentTask] = field(default_factory=list)

    @property
    def calls(self) -> list[str]:
        return [task.instance.rule_id for task in self.tasks]

    def of(self, rule_id: str) -> list[AgentTask]:
        return [t for t in self.tasks if t.instance.rule_id == rule_id]

    async def run_task(self, task: AgentTask) -> AgentResult:
        n_plans = len(self.of("digital-rtl/plan"))
        self.tasks.append(task)
        rule = task.instance.rule_id
        if rule == "digital-rtl/plan":
            text = self.plans[min(n_plans, len(self.plans) - 1)]
        elif rule == "digital-rtl/rtl_module":
            text = module_text(task.instance.params["module"])
        elif rule == "dv/tb_module":
            text = TB_TEXT
        elif rule == "digital-rtl/review":
            text = self.review
        else:
            raise AssertionError(f"no script for {rule}")
        for rel in task.allowed_writes:
            out = self.root / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
        return AgentResult(status="done", files_written=task.allowed_writes)


def register(ctx: AppContext, runtime: ChainRuntime) -> AppContext:
    """Give `ctx` the scripted runtime under the profile's runtime name."""
    ctx.registry.register("runtime", runtime.name, runtime)
    return ctx


# --- the project --------------------------------------------------------------------------


def target_project(
    dest: Path,
    *,
    review: Mapping[str, Any] | None = None,
    real_lint: bool = True,
    profile: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> Path:
    """A git copy of tinysoc set up for target `rtl:timer`, ingested.

    The packs are `digital-rtl` and `dv`; the checks of `rtl_module` always pass, except
    `lint` (tinysoc's own `make lint`) when `real_lint` and Verilator is on PATH;
    `tb_static` runs for real (no simulator). `review` is the profile's `review:`
    section (default: none, so the `file` adapter).
    """
    extra: dict[str, Any] = dict(profile or {})
    if review is not None:
        extra["review"] = dict(review)
    root = tinysoc_project(
        dest, packs=("digital-rtl", "dv"), stub=True, runtime="generic", ingest=False, **kwargs
    )
    path = root / ".chipgraph.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["adapters"]["tb_static"] = {"use": "tb_static", "top": "tiny_{block}"}
    if real_lint and HAS_VERILATOR:
        data["adapters"]["lint"] = yaml.safe_load(
            (TINYSOC / ".chipgraph.yml").read_text(encoding="utf-8")
        )["adapters"]["lint"]
    data.update(extra)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "profile for the target")
    from chipgraph.app.ingest import run_ingest

    run_ingest(AppContext.load(root))
    return root


def planned_timer(root: Path | None = None) -> str:
    """The fixture plan of `timer`, as text."""
    return (FIXTURES / "timer.plan.yml").read_text(encoding="utf-8")


def drive(root: Path, runtime: ChainRuntime, target: str) -> RunSummary:
    """Build `target` in the project at `root` with the scripted runtime."""
    ctx = register(AppContext.load(root), runtime)
    return asyncio.run(make_scheduler(ctx, target).run(target))


def approve(root: Path) -> None:
    """A person approves the plan of `timer`."""
    approve_plan(root, BLOCK)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def bare_remote(project: Path, where: Path, *, name: str = "origin") -> Path:
    """A bare repository at `where`, added as remote `name` of `project`, `main` pushed."""
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(where)], check=True)
    git(project, "remote", "add", name, str(where))
    git(project, "push", "-q", name, "main")
    return where


def tree_digest(root: Path, skip: Sequence[str] = ("pr", ".chipgraph", "plan", "reports")) -> str:
    """HEAD, the index, `git status` and the bytes of every working file except `skip`."""
    parts = [
        git(root, "rev-parse", "HEAD"),
        git(root, "ls-files", "-s"),
        git(root, "status", "--porcelain", "--untracked-files=all"),
    ]
    digest = hashlib.sha256("\n".join(parts).encode())
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts[0] == ".git" or rel.parts[0] in skip or not path.is_file():
            continue
        digest.update(str(rel).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


# --- a fake GitHub that follows the bare remote ------------------------------------------------


class RemoteGitHub(FakeGitHub):
    """`FakeGitHub` whose branches are those of a bare remote, read on every request."""

    def __init__(self, remote: Path, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.remote = remote

    def sync(self) -> None:
        out = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads"],
            cwd=self.remote,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.branches = dict(line.split(" ", 1) for line in out.splitlines() if line)
        for pr in self.prs.values():
            if pr["state"] == "open" and pr["head"]["ref"] in self.branches:
                pr["head"]["sha"] = self.branches[pr["head"]["ref"]]

    def handle(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, dict[str, str], bytes]:
        self.sync()
        return super().handle(method, url, headers, body)

    def approve(self, number: int, *, login: str = "reviewer") -> None:
        """A review that approves PR `number` at its head now."""
        self.sync()
        self.review(number, login, "APPROVED")


@contextmanager
def github_for(remote: Path) -> Iterator[RemoteGitHub]:
    """A `RemoteGitHub` served on 127.0.0.1; `fake.base_url` is the profile's `api_url`."""
    fake = RemoteGitHub(remote)
    with fake.serve():
        yield fake


def github_profile(fake: RemoteGitHub) -> dict[str, Any]:
    """The profile's `review:` section for `fake` (no `gh` fallback)."""
    return {
        "use": "github",
        "repo": REPO,
        "base": "main",
        "api_url": fake.base_url,
        "allow_gh_fallback": False,
        "remote": "origin",
    }


__all__ = [
    "BLOCK",
    "EXPAND",
    "HAS_VERILATOR",
    "MODULES",
    "MODULE_IDS",
    "OPEN_PR",
    "PLAN",
    "PR_GATE",
    "PR_RECORD",
    "REVIEW",
    "REVIEW_FILE",
    "RTL_FILES",
    "TB",
    "TB_FILE",
    "TB_TEXT",
    "TOKEN",
    "ChainRuntime",
    "RemoteGitHub",
    "approve",
    "bare_remote",
    "drive",
    "git",
    "github_for",
    "github_profile",
    "module_id",
    "module_text",
    "planned_timer",
    "register",
    "review_json",
    "target_project",
    "tree_digest",
]
