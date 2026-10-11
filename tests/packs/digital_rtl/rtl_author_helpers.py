"""Shared by the M2-04 tests: the acceptance fixture (`docs/rtl-claude-code/fixture.py`,
loaded under a unique module name), the two exemplar sets, a scripted RTL author that
writes `tiny_pwm` in the style of a `StyleProfile`, and short calls of the runtime tools.

Not a test module itself; imported by the `test_rtl_*.py` modules.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from chipgraph.adapters.runtime.claude_code import service
from chipgraph.app.context import AppContext
from chipgraph.checks.style import ResetStyle, StyleProfile, style_profile
from chipgraph.core.contracts import AgentResult
from chipgraph.core.plugin_api.types import AgentTask
from chipgraph.core.runtime import AgentTaskRecord, TaskQueue
from chipgraph.core.state.layout import StateLayout

REPO = Path(__file__).resolve().parents[3]
RTL_DOCS = REPO / "docs" / "rtl-claude-code"


def load_module(name: str, path: Path) -> ModuleType:
    """Import `path` as module `name` (once)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fx = load_module("rtl_acceptance_fixture", RTL_DOCS / "fixture.py")
TASK: str = fx.TASK
OUTPUT: str = fx.OUTPUT
HAS_VERILATOR = shutil.which("verilator") is not None and shutil.which("make") is not None
PASS = {"use": "cmd", "cmd": ["python3", "-c", "pass"]}
"""A check adapter that always passes."""


def make_project(
    dest: Path, *, exemplars: str = "a", runtime: str = "claude-code", **profile: Any
) -> Path:
    """The acceptance fixture at `dest`; without Verilator its `lint` always passes."""
    if not HAS_VERILATOR:
        adapters = yaml.safe_load((fx.EXAMPLE / ".chipgraph.yml").read_text())["adapters"]
        profile.setdefault("adapters", {**adapters, **fx.CHECKS, "lint": PASS})
    project: Path = fx.make_rtl_project(
        dest, exemplars=exemplars, runtime=runtime, profile=profile or None
    )
    return project


def exemplars(name: str) -> dict[str, str]:
    """The exemplar set `name` (`a` or `b`): path in a fixture copy to its text."""
    files: dict[str, str] = fx.exemplar_files(name)
    return files


def profile_of(name: str) -> StyleProfile:
    """The style profile of exemplar set `name`."""
    return style_profile(exemplars(name))


# --- a scripted RTL author -------------------------------------------------------------------


def render_pwm(
    profile: StyleProfile,
    *,
    name: str = "tiny_pwm",
    bad_signal: str | None = None,
    drop_output: bool = False,
) -> str:
    """`tiny_pwm` (docs/rtl-claude-code/TINY_PWM_MAS.md), written in `profile`'s style.

    What the fake author reads from the profile: indent char and width, `begin`
    placement, port alignment, the reset (kind, level, name), the register and
    next-value suffixes, the comment header. `bad_signal` adds a signal of that name (to
    break the naming rule); `drop_output` leaves `pwm_out` undriven (to break lint).
    """
    ind = "\t" if profile.indent_char == "tab" else " " * (profile.indent_width or 2)
    same = profile.begin != "next_line"
    reg = profile.reg_suffix or "_q"
    nxt = profile.next_suffix or "_d"
    reset = profile.reset or ResetStyle(kind="async", active="low", name="rst_n")
    aligned = profile.ports.aligned if profile.ports is not None else True
    out: list[str] = []

    def emit(level: int = 0, text: str = "") -> None:
        out.append(ind * level + text if text else "")

    def open_(level: int, head: str) -> None:
        if same:
            emit(level, f"{head} begin")
        else:
            emit(level, head)
            emit(level, "begin")

    def else_(level: int, head: str = "else") -> None:
        if same:
            emit(level, f"end {head} begin")
        else:
            emit(level, "end")
            emit(level, head)
            emit(level, "begin")

    if profile.header is not False:
        out += [
            "// SPDX-License-Identifier: Apache-2.0",
            "// tiny_pwm: one PWM channel behind the word address bus (TINY_PWM_MAS).",
            "",
        ]
    ports = [
        ("input", "", "clk"),
        ("input", "", reset.name),
        ("input", "[1:0]", "addr"),
        ("input", "", "wr_en"),
        ("input", "[31:0]", "wdata"),
        ("output", "[31:0]", "rdata"),
        ("output", "", "pwm_out"),
    ]
    out.append(f"module {name} (")
    for i, (direction, width, port) in enumerate(ports):
        comma = "," if i < len(ports) - 1 else ""
        if aligned:
            decl = f"{direction:<6} logic {width:<6} {port}{comma}"
        else:
            decl = f"{direction} logic {width + ' ' if width else ''}{port}{comma}"
        if port == "wdata":
            emit(1, "/* verilator lint_off UNUSEDSIGNAL */")
            emit(1, decl)
            emit(1, "/* verilator lint_on UNUSEDSIGNAL */")
        else:
            emit(1, decl)
    out += [");", ""]
    regs = [("[7:0]", "period"), ("[7:0]", "duty"), ("", "enable"), ("[7:0]", "count")]
    for width, sig in regs:
        emit(1, f"logic {width + ' ' if width else ''}{sig}{nxt};")
        emit(1, f"logic {width + ' ' if width else ''}{sig}{reg};")
    if bad_signal is not None:
        emit(1, f"logic {bad_signal};")
        emit(1, f"assign {bad_signal} = 1'b0;")
    emit()
    emit(1, "// REQ-PWM-003: a write loads PERIOD, DUTY or CTRL.")
    open_(1, "always_comb")
    for _, sig in regs[:3]:
        emit(2, f"{sig}{nxt} = {sig}{reg};")
    open_(2, "if (wr_en)")
    emit(3, "case (addr)")
    emit(4, f"2'd0: period{nxt} = wdata[7:0];")
    emit(4, f"2'd1: duty{nxt} = wdata[7:0];")
    emit(4, f"2'd2: enable{nxt} = wdata[0];")
    emit(4, "default: ;")
    emit(3, "endcase")
    emit(2, "end")
    emit(1, "end")
    emit()
    emit(1, "// REQ-PWM-001: count while enabled, wrap from PERIOD to 0, hold at 0 otherwise.")
    open_(1, "always_comb")
    emit(2, f"count{nxt} = count{reg} + 8'd1;")
    open_(2, f"if (!enable{reg} || count{reg} == period{reg})")
    emit(3, f"count{nxt} = 8'd0;")
    emit(2, "end")
    emit(1, "end")
    emit()
    low = reset.active == "low"
    cond = f"!{reset.name}" if low else reset.name
    if reset.kind == "async":
        edge = "negedge" if low else "posedge"
        head = f"always_ff @(posedge clk or {edge} {reset.name})"
    else:
        head = "always_ff @(posedge clk)"
    open_(1, head)
    open_(2, f"if ({cond})")
    for _, sig in regs:
        emit(3, f"{sig}{reg} <= '0;")
    else_(2)
    for _, sig in regs:
        emit(3, f"{sig}{reg} <= {sig}{nxt};")
    emit(2, "end")
    emit(1, "end")
    emit()
    emit(1, "// REQ-PWM-004: read back each register, zero extended; offset 0x3 reads 0.")
    open_(1, "always_comb")
    emit(2, "case (addr)")
    emit(3, f"2'd0: rdata = {{24'd0, period{reg}}};")
    emit(3, f"2'd1: rdata = {{24'd0, duty{reg}}};")
    emit(3, f"2'd2: rdata = {{31'd0, enable{reg}}};")
    emit(3, "default: rdata = 32'd0;")
    emit(2, "endcase")
    emit(1, "end")
    emit()
    if drop_output:
        emit(1, "// pwm_out is not driven")
    else:
        emit(1, "// REQ-PWM-002: high while enabled and the count is below DUTY.")
        emit(1, f"assign pwm_out = enable{reg} && (count{reg} < duty{reg});")
    out += ["", "endmodule", ""]
    return "\n".join(out)


@dataclass
class RtlRuntime:
    """A scripted in-process `AgentRuntime` for an RTL task: call n writes `texts[n]` (the
    last repeats) to the task's output, and remembers the tasks it was given."""

    root: Path
    texts: list[str]
    name: str = "generic"
    tasks: list[AgentTask] = field(default_factory=list)

    async def run_task(self, task: AgentTask) -> AgentResult:
        text = self.texts[min(len(self.tasks), len(self.texts) - 1)]
        self.tasks.append(task)
        for rel in task.allowed_writes:
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return AgentResult(status="done", files_written=task.allowed_writes)


# --- the runtime tools -----------------------------------------------------------------------


def ctx(root: Path) -> AppContext:
    return AppContext.load(root)


def next_task(root: Path, target: str = fx.RULE) -> dict[str, Any]:
    return asyncio.run(service.next_task(ctx(root), target))


def get_context(root: Path, task_id: str = TASK) -> dict[str, Any]:
    return asyncio.run(service.get_context(ctx(root), task_id))


def submit(root: Path, task_id: str = TASK, **report: Any) -> dict[str, Any]:
    return asyncio.run(service.submit(ctx(root), task_id, service.SubmitReport(**report)))


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def record(root: Path, task_id: str = TASK) -> AgentTaskRecord:
    return TaskQueue(StateLayout(root)).require(task_id)


def commit_all(root: Path, message: str = "change") -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=root, check=True)


def tb_lines(root: Path) -> list[str]:
    """Distinctive testbench lines of a fixture copy: the planted fingerprints and every
    line of 24+ characters under `dv/` found in no other committed file."""
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, check=True
    ).stdout
    files = [p for p in listed.split("\0") if p]
    elsewhere = "\n".join(
        (root / p).read_text(encoding="utf-8", errors="replace")
        for p in files
        if not p.startswith("dv/") and (root / p).is_file()
    )
    found = {fx.FINGERPRINT}
    for rel in files:
        if rel.startswith("dv/"):
            for line in (root / rel).read_text(encoding="utf-8").splitlines():
                text = line.strip()
                if len(text) >= 24 and text not in elsewhere:
                    found.add(text)
    return sorted(found)


def leaks(answer: object, lines: list[str]) -> list[str]:
    """The testbench lines found anywhere in `answer` (as JSON)."""
    text = json.dumps(answer, default=str)
    return [line for line in lines if line in text or json.dumps(line)[1:-1] in text]


__all__ = [
    "HAS_VERILATOR",
    "OUTPUT",
    "PASS",
    "REPO",
    "RTL_DOCS",
    "TASK",
    "RtlRuntime",
    "commit_all",
    "ctx",
    "exemplars",
    "fx",
    "get_context",
    "leaks",
    "load_module",
    "make_project",
    "next_task",
    "profile_of",
    "record",
    "render_pwm",
    "submit",
    "tb_lines",
    "write",
]
