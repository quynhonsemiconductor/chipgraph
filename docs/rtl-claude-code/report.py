"""Summarise one M2-04 acceptance run (`run.sh`): the `digital-rtl/rtl_module` task done
twice, on two tinysoc copies that differ only in their style exemplars.

    uv run python docs/rtl-claude-code/report.py OUT_DIR

Reads, for each exemplar set `<s>` (`a`, `b`), OUT_DIR/<s>/stream.jsonl (the `claude -p`
stream-json), the copy's chipgraph state (task queue, agent bindings, guard log) and its
files (OUT_DIR/<s>/tinysoc).

Per run it prints the subagent, its model, the tools it used and those refused, and

(a) the module `rtl/tiny_pwm.sv` was written and the task accepted;
(b) `lint` and `naming` pass on it (run again here, as the engine runs them);
(c) the task ran as `chipgraph:rtl-author`, no tool call of it read a testbench (no
    read-type call at all, `Read`/`Glob`/`Grep`/`NotebookRead`/`LS`, and no other call
    naming a `dv/` path or a test file), and no testbench line is found in its Agent
    prompt or in any tool result it got: the fingerprint comment `fixture.py` planted in
    each testbench file, and every other testbench line of 24+ characters found in no
    other committed file;
(d) the style distance of the module to its own exemplar set is smaller than to the
    other set (`chipgraph.checks.style.style_distance`): the style follows the exemplars.

PASS needs (a) to (d) for both runs. The two distances of each run are printed.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
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
from chipgraph.checks.style import style_distance, style_profile
from chipgraph.core.runtime import TaskQueue
from chipgraph.core.state.layout import StateLayout


def _sibling(name: str, file: str) -> ModuleType:
    """The sibling `file`, imported under the unique module name `name` (not `fixture`:
    other acceptance reports import theirs under that name)."""
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / file)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


_fx = _sibling("rtl_acceptance_fixture", "fixture.py")
EXEMPLAR_SETS: tuple[str, ...] = _fx.EXEMPLAR_SETS
FINGERPRINT: str = _fx.FINGERPRINT
OUTPUT: str = _fx.OUTPUT
TASK: str = _fx.TASK

READ_TOOLS = frozenset({"Read", "Glob", "Grep", "NotebookRead", "LS"})
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
AGENT_TOOLS = frozenset({"Agent", "Task"})
CHIPGRAPH_MCP = "mcp__plugin_chipgraph_chipgraph__"
SUBAGENT = "chipgraph:rtl-author"
_TEST_NAME = re.compile(r"(?:^|[/\s\"'])(?:test_\w*\.py|tb_\w*\.(?:py|sv|v)|\w*_tb\.(?:py|sv|v))")


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


def load_events(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "stream.jsonl"
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


def tb_fingerprints(proj: Path) -> list[str]:
    """Distinctive testbench lines: the planted fingerprints, and every line of 24+
    characters under `dv/` found in no other committed file."""
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=proj, capture_output=True, text=True, check=False
    ).stdout
    files = sorted(p for p in listed.split("\0") if p)
    elsewhere = "\n".join(
        (proj / rel).read_text(encoding="utf-8", errors="replace")
        for rel in files
        if not rel.startswith("dv/") and (proj / rel).is_file()
    )
    lines = {FINGERPRINT}
    for rel in files:
        if rel.startswith("dv/") and (proj / rel).is_file():
            for line in (proj / rel).read_text(encoding="utf-8", errors="replace").splitlines():
                text = line.strip()
                if len(text) >= 24 and text not in elsewhere:
                    lines.add(text)
    return sorted(lines)


def tb_reads(run: TaskRun, fingerprints: list[str]) -> list[str]:
    """Every way the run's subagent tried to read, or got to see, a testbench."""
    found = []
    for agent in run.agents:
        prompt = json.dumps(agent.input)
        if any(fp in prompt for fp in fingerprints):
            found.append("its Agent prompt holds testbench text")
    for use in run.calls:
        args = json.dumps(use.input)
        if use.name in READ_TOOLS:
            found.append(f"{use.name} {args[:120]}")
        elif (
            use.name not in WRITE_TOOLS
            and not use.name.startswith(CHIPGRAPH_MCP)
            and ("dv/" in args or _TEST_NAME.search(args))
        ):
            found.append(f"{use.name} names a testbench path: {args[:120]}")
        hits = [fp for fp in fingerprints if fp in use.result]
        if hits:
            found.append(f"the result of {use.name} holds testbench text: {hits[0][:80]!r}")
    return found


def rerun_checks(proj: Path, checks: tuple[str, ...] = ("lint", "naming")) -> dict[str, str]:
    """Run the task's `checks` on the module as the engine does; status by check id."""
    ctx = AppContext.load(proj)
    graph = make_scheduler(ctx, TASK).graph
    instance = graph.instances.get(TASK)
    if instance is None:
        return dict.fromkeys(checks, "no instance")
    runner = ProfileCheckRunner(ctx)
    return {c: asyncio.run(runner.run(c, instance)).status for c in checks}


def distances(text: str) -> dict[str, float]:
    """The style distance of `text` to each exemplar set."""
    return {
        name: style_distance(style_profile(_fx.exemplar_files(name)), text)
        for name in EXEMPLAR_SETS
    }


def report_run(out: Path, name: str) -> bool:
    """Print the report of the run with exemplar set `name`; whether it passed."""
    run_dir = out / name
    proj = run_dir / "tinysoc"
    events = load_events(run_dir)
    uses = tool_uses(events)
    print(f"== run {name} (exemplars: style/{name}/)")
    if not proj.is_dir():
        print("  no project copy: the run did not start")
        return False
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    print(f"  plugins: {[p.get('name') for p in init.get('plugins', [])]}")
    print("  main-session tool calls:")
    for use in uses:
        if use.parent is None:
            print(f"    {use.name:48} {json.dumps(use.input)[:100]}")
    queue = TaskQueue(StateLayout(proj))
    record = next((r for r in queue.all() if r.task_id == TASK), None)
    bound = {b.agent_id: b.task_id for b in queue.bindings()}
    log = queue.runtime_dir / "guard.log"
    guard = [json.loads(x) for x in log.read_text().splitlines()] if log.is_file() else []
    run = task_run(uses)
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
    for g in guard:
        if g.get("decision") == "deny" and bound.get(g.get("agent_id") or "") == TASK:
            print(f"  guard deny {g['tool']}: {g['detail'][:160]}")

    path = proj / OUTPUT
    written = path.is_file()
    accepted = record is not None and record.status == "accepted"
    statuses = rerun_checks(proj) if written else {}
    checks_ok = bool(statuses) and all(s == "pass" for s in statuses.values())
    ran = bool(run.agents) and all(u.input.get("subagent_type") == SUBAGENT for u in run.agents)
    fingerprints = tb_fingerprints(proj)
    reads = tb_reads(run, fingerprints)
    dist = distances(path.read_text(encoding="utf-8")) if written else {}
    other = next(s for s in EXEMPLAR_SETS if s != name)
    follows = written and dist[name] < dist[other]
    a, b, c, d = written and accepted, checks_ok, ran and not reads and bool(fingerprints), follows
    print(f"  (a) wrote {OUTPUT}: {written}; task accepted: {accepted}")
    print(f"  (b) lint and naming passed: {b} {statuses}")
    print(
        f"  (c) ran as {SUBAGENT}: {ran}; read no testbench and saw none "
        f"({len(fingerprints)} testbench lines checked): {not reads}"
    )
    for item in reads:
        print(f"      - {item}")
    if dist:
        print(
            f"  (d) style distance to set {name}: {dist[name]:.4f}; to set {other}: "
            f"{dist[other]:.4f}; follows its own exemplars: {follows}"
        )
    else:
        print("  (d) no module to measure")
    passed = a and b and c and d
    print(f"  run {name}: {'PASS' if passed else 'FAIL'}")
    return passed


def main(out: Path) -> int:
    verdicts = {name: report_run(out, name) for name in EXEMPLAR_SETS}
    for name in EXEMPLAR_SETS:
        timing = out / name / "timing.txt"
        print(f"== timing {name}: {timing.read_text().strip() if timing.is_file() else '?'}")
    ok = all(verdicts.values())
    print("== verdict")
    for name, passed in verdicts.items():
        print(f"  {TASK} with exemplars {name}: {'PASS' if passed else 'FAIL'}")
    print(f"  {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
