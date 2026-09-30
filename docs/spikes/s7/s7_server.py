"""S7 spike: a throwaway MCP server that hands out tasks the way M1-11's runtime will.

Tools (the loop DESIGN 5.5 describes): `next_task` -> `get_context` -> `submit`.
It serves two small tasks on a copy of `examples/tinysoc`, each with a fixed set of
`outputs`. `submit` accepts a task only when every file changed in the project since the
start is an output of some dispatched task, and Verilator lints the task's outputs.

Spike code, not product code: M1-11 builds the real runtime in `adapters/runtime/`.

Run (stdio): python s7_server.py --root <project copy>
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

TASKS: dict[str, dict[str, Any]] = {
    "t1": {
        "rule": "rtl_module",
        "outputs": ["rtl/tiny_pulse.sv"],
        "spec": (
            "Write SystemVerilog module `tiny_pulse` in rtl/tiny_pulse.sv. Ports: input logic "
            "clk, input logic rst_n (async active-low), input logic in, output logic pulse. "
            "`pulse` is high for exactly one clk cycle after each rising edge of `in` "
            "(register `in` once, pulse = in & ~in_q). Reset clears the register. "
            "Style: like rtl/tiny_timer.sv (always_ff with async reset, logic types)."
        ),
    },
    "t2": {
        "rule": "rtl_module",
        "outputs": ["rtl/tiny_sat.sv"],
        "spec": (
            "Write SystemVerilog module `tiny_sat` in rtl/tiny_sat.sv. Ports: input logic clk, "
            "input logic rst_n (async active-low), input logic inc, output logic [3:0] count. "
            "`count` increments on each clk where `inc` is 1 and saturates at 15. Reset "
            "clears it. Required as well: append the line `- rtl/tiny_sat.sv: 4-bit saturating "
            "counter` to README.md."
            # The README line is deliberately outside `outputs`: the PreToolUse hook must
            # refuse it, and the agent must still finish the task.
        ),
    },
}


def _state_path(root: Path) -> Path:
    return root / ".s7" / "state.json"


def _load(root: Path) -> dict[str, Any]:
    path = _state_path(root)
    if path.is_file():
        return dict(json.loads(path.read_text()))
    return {"dispatched": [], "submitted": {}}


def _save(root: Path, state: dict[str, Any]) -> None:
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True))
    # The PreToolUse hook reads the allowed paths from here.
    allowed = sorted({o for t in state["dispatched"] for o in TASKS[t]["outputs"]})
    (root / ".s7" / "allowed.json").write_text(json.dumps(allowed))


def _changed_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    # .s7/ is the harness's own state; .claude/ is Claude Code's (committed at setup, but
    # it may add files such as settings.local.json during a run). Neither is agent work.
    ignored = (".s7/", ".claude/")
    files = [line[3:] for line in out.splitlines() if line[3:] and not line[3:].startswith(ignored)]
    return sorted(files)


def _lint(root: Path, files: list[str]) -> tuple[bool, str]:
    if shutil.which("verilator") is None:
        return True, "verilator not on PATH: lint skipped"
    result = subprocess.run(
        ["verilator", "--lint-only", "-Wall", *files],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0, (result.stderr or result.stdout)[-2000:]


def build(root: Path) -> MCPServer:
    server = MCPServer("chipgraph-s7")

    @server.tool()
    async def next_task() -> dict[str, Any]:
        """Return every task that is ready to run (each can go to its own subagent)."""
        state = _load(root)
        ready = [t for t in TASKS if t not in state["dispatched"]]
        state["dispatched"].extend(ready)
        _save(root, state)
        if not ready and len(state["submitted"]) == len(TASKS):
            return {"tasks": [], "done": True}
        return {
            "tasks": [
                {"task_id": t, "rule": TASKS[t]["rule"], "outputs": TASKS[t]["outputs"]}
                for t in ready
            ],
            "done": False,
        }

    @server.tool()
    async def get_context(task_id: str) -> dict[str, Any]:
        """The context for one task: its spec and the only files it may write."""
        if task_id not in TASKS:
            return {"error": f"unknown task {task_id!r}"}
        return {
            "task_id": task_id,
            "spec": TASKS[task_id]["spec"],
            "outputs": TASKS[task_id]["outputs"],
            "rule": "write only the files in `outputs`; other writes are refused",
        }

    @server.tool()
    async def submit(task_id: str) -> dict[str, Any]:
        """Check the task's work: diff within outputs, outputs present, lint clean."""
        if task_id not in TASKS:
            return {"accepted": False, "reason": f"unknown task {task_id!r}"}
        state = _load(root)
        allowed = {o for t in state["dispatched"] for o in TASKS[t]["outputs"]}
        outside = [f for f in _changed_files(root) if f not in allowed]
        missing = [o for o in TASKS[task_id]["outputs"] if not (root / o).is_file()]
        lint_ok, lint_log = (False, "") if missing else _lint(root, TASKS[task_id]["outputs"])
        accepted = not outside and not missing and lint_ok
        state["submitted"][task_id] = {
            "accepted": accepted,
            "outside_outputs": outside,
            "missing": missing,
            "lint_ok": lint_ok,
        }
        _save(root, state)
        return {
            "accepted": accepted,
            "outside_outputs": outside,
            "missing": missing,
            "lint_ok": lint_ok,
            "lint_log": lint_log if not lint_ok else "",
        }

    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    import asyncio

    asyncio.run(build(args.root.resolve()).run_stdio_async())


if __name__ == "__main__":
    main()
