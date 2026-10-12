"""Summarise one M2-12 acceptance run (`run.sh`): the wrapper rule for vendor IP.

    uv run python docs/wrapper-claude-code/report.py OUT_DIR

Reads OUT_DIR/stream.jsonl (the `claude -p` stream-json), the copy's chipgraph state (task
queue, run journals) and files (OUT_DIR/tinysoc). `run.sh` has run the generator after
the session (`chipgraph build digital-rtl/wrapper_gen[...]`).

Prints the subagent, its model, the tools it used and those refused, the checks of each
attempt, the cost of the run, and

(a) the source `rtl/emacs/tiny_fifo_wrap.src.sv` was written by the subagent (a `Write` of
    that path, whose text is the file) and the task `digital-rtl/wrapper[...]` was accepted;
(b) the generated wrapper `rtl/tiny_fifo_wrap.sv` exists and `wrap_check`, `lint` and
    `naming` pass on it (run again here, as the engine runs them);
(c) the vendor files are byte-identical to `vendor/MANIFEST.sha256` (the `vendor_guard`
    check) and unchanged in git;
(d) the task ran as `chipgraph:rtl-author` and the subagent used only `get_context` and
    `Write`: no read tool, no shell, no other tool.

PASS needs (a) to (d).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from chipgraph.app.build import make_scheduler
from chipgraph.app.checks import ProfileCheckRunner
from chipgraph.app.context import AppContext
from chipgraph.core.runtime import TaskQueue
from chipgraph.core.state import journal as journal_mod
from chipgraph.core.state.layout import StateLayout


def _sibling(name: str, file: str) -> ModuleType:
    """The sibling `file`, imported under the unique module name `name`."""
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / file)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


_fx = _sibling("wrapper_acceptance_fixture", "fixture.py")
TASK: str = _fx.TASK
GEN_TASK: str = _fx.GEN_TASK
SRC: str = _fx.SRC
OUTPUT: str = _fx.OUTPUT
MANIFEST: str = _fx.MANIFEST
IP_FILES: tuple[str, ...] = _fx.IP_FILES

CHIPGRAPH_MCP = "mcp__plugin_chipgraph_chipgraph__"
ALLOWED_TOOLS = frozenset({CHIPGRAPH_MCP + "get_context", "Write"})
AGENT_TOOLS = frozenset({"Agent", "Task"})
SUBAGENT = "chipgraph:rtl-author"
GEN_CHECKS = ("wrap_check", "lint", "naming")


@dataclass
class Use:
    """One tool call in the stream, and its result if the stream has one."""

    id: str
    parent: str | None
    name: str
    input: dict[str, Any]
    result: str = ""
    is_error: bool = False


@dataclass
class TaskRun:
    """What the task's subagent(s) did."""

    agents: list[Use] = field(default_factory=list)
    calls: list[Use] = field(default_factory=list)


def load_events(out: Path) -> list[dict[str, Any]]:
    path = out / "stream.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("{")
    ]


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            _text(c.get("text", c.get("content", ""))) for c in content if isinstance(c, dict)
        )
    return ""


def tool_uses(events: list[dict[str, Any]]) -> list[Use]:
    """Every tool call, in order, with its result joined on."""
    uses: dict[str, Use] = {}
    for ev in events:
        msg = ev.get("message")
        if not isinstance(msg, dict):
            continue
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                uses[block["id"]] = Use(
                    id=block["id"],
                    parent=ev.get("parent_tool_use_id"),
                    name=str(block.get("name", "")),
                    input=block.get("input") or {},
                )
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in uses:
                use = uses[block["tool_use_id"]]
                use.result = _text(block.get("content"))
                use.is_error = bool(block.get("is_error"))
    return list(uses.values())


def task_run(uses: list[Use], task_id: str = TASK) -> TaskRun:
    """The subagents started for the task (by its id in the Agent call) and their calls."""
    run = TaskRun()
    owners = set()
    for use in uses:
        if use.name in AGENT_TOOLS and task_id in json.dumps(use.input):
            run.agents.append(use)
            owners.add(use.id)
    run.calls = [u for u in uses if u.parent in owners]
    return run


def source_writes(run: TaskRun) -> list[Use]:
    """The subagent's `Write` calls of the wrapper source (not refused)."""
    return [
        u
        for u in run.calls
        if u.name == "Write"
        and not u.is_error
        and str(u.input.get("file_path", "")).replace("\\", "/").endswith(SRC)
    ]


def unexpected_tools(run: TaskRun) -> list[str]:
    """Every call of the subagent beyond `get_context` and `Write`."""
    return [
        f"{u.name} {json.dumps(u.input)[:100]}" for u in run.calls if u.name not in ALLOWED_TOOLS
    ]


def attempts(proj: Path, task_id: str = TASK) -> list[str]:
    """One line per check result and per agent turn of the task, over every run's journal."""
    layout = StateLayout(proj)
    items: list[tuple[str, str]] = []
    if not layout.runs_dir.is_dir():
        return []
    for run_dir in sorted(layout.runs_dir.iterdir()):
        journal = run_dir / "journal.jsonl"
        if not journal.is_file():
            continue
        for event in journal_mod.read(journal).events:
            if event.rule_instance != task_id:
                continue
            payload = event.payload
            if event.type == "check_result":
                issues = payload.get("issues") or []
                errors = [i for i in issues if i.get("severity") == "error"]
                first = f"; first: {errors[0]['rule']} {errors[0]['msg'][:90]}" if errors else ""
                line = f"check {payload.get('check_id')}: {payload.get('status')}{first}"
            elif event.type == "agent_turn" and payload.get("phase") == "submit":
                line = (
                    f"submit: label={payload.get('label')} tier={payload.get('tier')} "
                    f"failed={payload.get('failed_checks')}"
                )
            else:
                continue
            items.append((event.ts.isoformat(), line))
    return [line for _, line in sorted(items, key=lambda item: item[0])]


def rerun_checks(proj: Path, checks: tuple[str, ...] = GEN_CHECKS) -> dict[str, str]:
    """Run the generator rule's `checks` on the wrapper as the engine does; status by id."""
    ctx = AppContext.load(proj)
    graph = make_scheduler(ctx, GEN_TASK).graph
    instance = graph.instances.get(GEN_TASK)
    if instance is None:
        return dict.fromkeys(checks, "no instance")
    runner = ProfileCheckRunner(ctx)
    return {c: asyncio.run(runner.run(c, instance)).status for c in checks}


def vendor_status(proj: Path) -> tuple[bool, list[str]]:
    """(unchanged, notes): `vendor_guard` passes and git sees no change under vendor/."""
    from chipgraph.checks.vendor_guard import VendorGuardCheck
    from chipgraph.core.contracts import CheckSpec
    from chipgraph.core.plugin_api.types import ToolContext

    class _NoRunner:
        name = "none"

        async def run(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("vendor_guard runs no subprocess")

    spec = CheckSpec(id="vendor_guard", capability="vendor_guard", adapter="vendor_guard")
    result = asyncio.run(
        VendorGuardCheck().run(spec, ToolContext(repo_root=proj, runner=_NoRunner()))
    )
    notes = [f"vendor_guard: {result.status}"] + [
        f"{i.file}: {i.rule}" for i in result.issues if i.severity == "error"
    ]
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", _fx.IP_DIR],
        cwd=proj,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if dirty:
        notes.append(f"git status of vendor/: {dirty}")
    return result.status == "pass" and not dirty, notes


def main(out: Path) -> int:
    proj = out / "tinysoc"
    events = load_events(out)
    uses = tool_uses(events)
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    print("== session")
    if not proj.is_dir():
        print("  no project copy: the run did not start")
        print("  FAIL")
        return 1
    print(f"  plugins: {[p.get('name') for p in init.get('plugins', [])]}")
    print("== main-session tool calls")
    for use in uses:
        if use.parent is None:
            print(f"  {use.name:48} {json.dumps(use.input)[:100]}")

    queue = TaskQueue(StateLayout(proj))
    record = next((r for r in queue.all() if r.task_id == TASK), None)
    run = task_run(uses)
    print(f"== task {TASK}")
    print(
        f"  status: {record.status if record else 'never queued'}"
        + (f" attempts={record.attempts}/{record.tries}" if record else "")
    )
    if record is not None and record.reasons:
        print(f"  last reasons: {[r[:200] for r in record.reasons]}")
    for agent in run.agents:
        print(f"  subagent: {agent.input.get('subagent_type')} model={agent.input.get('model')}")
    print(f"  tools used: {dict(Counter(u.name for u in run.calls))}")
    for use in run.calls:
        if use.is_error:
            print(f"  refused {use.name}: {use.result[:160]}")
    print("== checks, per attempt")
    for line in attempts(proj):
        print(f"  {line}")

    source = proj / SRC
    writes = source_writes(run)
    wrote = (
        bool(writes) and source.is_file() and writes[-1].input.get("content") == source.read_text()
    )
    accepted = record is not None and record.status == "accepted"
    generated = proj / OUTPUT
    statuses = rerun_checks(proj) if generated.is_file() and source.is_file() else {}
    checks_ok = bool(statuses) and all(s == "pass" for s in statuses.values())
    unchanged, vendor_notes = vendor_status(proj)
    extra = unexpected_tools(run)
    used = {u.name for u in run.calls}
    ran = bool(run.agents) and all(u.input.get("subagent_type") == SUBAGENT for u in run.agents)
    only = ran and not extra and used >= ALLOWED_TOOLS

    build = out / "build.json"
    built = build.read_text().strip()[:300] if build.is_file() else "?"
    print(f"== chipgraph build after the run: {built}")
    print("== result event")
    for key in ("subtype", "is_error", "num_turns", "duration_ms", "total_cost_usd", "modelUsage"):
        if key in result:
            print(f"  {key}: {json.dumps(result[key])[:400]}")
    cost = result.get("total_cost_usd")
    print(f"  cost: {f'${cost:.4f}' if isinstance(cost, int | float) else 'not reported'}")
    timing = out / "timing.txt"
    print(f"== timing: {timing.read_text().strip() if timing.is_file() else '?'}")

    print("== verdict")
    print(f"  (a) {SRC} written by the subagent: {wrote}; task accepted: {accepted}")
    print(f"  (b) {OUTPUT} passes wrap_check, lint and naming: {checks_ok} {statuses}")
    print(f"  (c) vendor files byte-identical to {MANIFEST}: {unchanged}")
    for note in vendor_notes:
        print(f"      {note}")
    print(f"  (d) ran as {SUBAGENT} and used only get_context and Write: {only}")
    for item in extra:
        print(f"      - {item}")
    ok = wrote and accepted and checks_ok and unchanged and only
    print(f"  {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
