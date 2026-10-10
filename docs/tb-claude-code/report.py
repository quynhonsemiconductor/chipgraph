"""Summarise one M2-06 acceptance run (`run.sh`): per `dv/tb_module` task the subagent,
its model, the tools it used and those refused, and the verdict; then, for information
only, the written tests run on the RTL.

    uv run --extra sim python docs/tb-claude-code/report.py OUT_DIR

Reads OUT_DIR/stream.jsonl (the `claude -p` stream-json), the project's chipgraph state
(task queue, agent bindings, guard log) and the project copy's files.

PASS needs, for each of the gpio and timer tasks:

(a) the test file `dv/<block>/test_<block>.py` was written;
(b) it passes `tb_static` (run again here, as the engine runs it);
(c) the task ran as `chipgraph:tb-author`, no tool call of it read any RTL (no read-type
    call at all, `Read`/`Glob`/`Grep`/`NotebookRead`/`LS`, and no other call naming an
    `rtl/` path or an RTL file), and no RTL body line is found in its Agent prompt or in
    any tool result it got: the fingerprint comment `fixture.py` planted in each RTL
    body, and every other RTL line of 24+ characters found in no other committed file.

Then each written test runs on its block's RTL with the `edalize` adapter (Verilator),
and its tests are listed with pass/fail. That part is informational: a test may fail
because the design is wrong, which is what an independent test is for.
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

from chipgraph.adapters.parser.cocotb import CocotbParser
from chipgraph.adapters.runner.local import LocalRunner
from chipgraph.adapters.tool.edalize import EdalizeTool
from chipgraph.app.build import make_scheduler
from chipgraph.app.checks import ProfileCheckRunner
from chipgraph.app.context import AppContext
from chipgraph.core.contracts import CheckSpec
from chipgraph.core.plugin_api.types import ToolContext
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


_fx = _sibling("tb_acceptance_fixture", "fixture.py")
BLOCKS: tuple[str, ...] = _fx.BLOCKS
FINGERPRINT: str = _fx.FINGERPRINT
OUTPUTS: dict[str, str] = _fx.OUTPUTS
SIM: dict[str, Any] = _fx.SIM
TASKS: dict[str, str] = _fx.TASKS

READ_TOOLS = frozenset({"Read", "Glob", "Grep", "NotebookRead", "LS"})
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
AGENT_TOOLS = frozenset({"Agent", "Task"})
CHIPGRAPH_MCP = "mcp__plugin_chipgraph_chipgraph__"
RTL_SUFFIXES = (".sv", ".v", ".svh", ".vh")


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
    """What one task's subagent(s) did."""

    task_id: str
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


def task_runs(uses: list[Use], task_ids: list[str]) -> dict[str, TaskRun]:
    """The subagents started for each task (by its id in the Agent call) and their calls.

    Longer ids are matched first, so no task id that is a prefix of another steals it.
    """
    runs = {task_id: TaskRun(task_id) for task_id in task_ids}
    owner: dict[str, str] = {}
    ordered = sorted(task_ids, key=len, reverse=True)
    for use in uses:
        if use.name in AGENT_TOOLS:
            text = json.dumps(use.input)
            for task_id in ordered:
                if task_id in text:
                    runs[task_id].agents.append(use)
                    owner[use.id] = task_id
                    break
    for use in uses:
        if use.parent in owner:
            runs[owner[use.parent]].calls.append(use)
    return runs


def rtl_fingerprints(proj: Path) -> list[str]:
    """Distinctive lines of the project's RTL: the planted fingerprints, and every line of
    24+ characters found in no other committed file (an agent's outputs are new files, so
    RTL text copied into one still counts)."""
    rtl_files = [p for p in sorted((proj / "rtl").rglob("*")) if p.suffix in RTL_SUFFIXES]
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=proj, capture_output=True, text=True, check=False
    ).stdout
    others = [
        (proj / rel).read_text(encoding="utf-8", errors="replace")
        for rel in sorted(p for p in listed.split("\0") if p)
        if not rel.startswith("rtl/") and (proj / rel).is_file()
    ]
    elsewhere = "\n".join(others)
    lines = {FINGERPRINT}
    for path in rtl_files:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            text = line.strip()
            if len(text) >= 24 and text not in elsewhere:
                lines.add(text)
    return sorted(lines)


def rtl_reads(run: TaskRun, fingerprints: list[str]) -> list[str]:
    """Every way the run's subagent tried to read, or got to see, RTL."""
    found = []
    for agent in run.agents:
        prompt = json.dumps(agent.input)
        if any(fp in prompt for fp in fingerprints):
            found.append("its Agent prompt holds RTL text")
    for use in run.calls:
        args = json.dumps(use.input)
        if use.name in READ_TOOLS:
            found.append(f"{use.name} {args[:120]}")
        elif (
            use.name not in WRITE_TOOLS
            and not use.name.startswith(CHIPGRAPH_MCP)
            and ("rtl/" in args or any(s in args for s in RTL_SUFFIXES))
        ):
            found.append(f"{use.name} names an rtl path: {args[:120]}")
        hits = [fp for fp in fingerprints if fp in use.result]
        if hits:
            found.append(f"the result of {use.name} holds RTL text: {hits[0][:80]!r}")
    return found


def tb_static(proj: Path, block: str) -> tuple[bool, list[str]]:
    """Run `tb_static` on the block's test as the engine does (the profile's config)."""
    ctx = AppContext.load(proj)
    instance = make_scheduler(ctx, TASKS[block]).graph.instances[TASKS[block]]
    result = asyncio.run(ProfileCheckRunner(ctx).run("tb_static", instance))
    shown = [
        f"{i.severity} {i.file}:{i.line or ''} [{i.rule}] {i.msg[:140]}" for i in result.issues
    ]
    return result.status == "pass", shown


def simulate(out: Path, proj: Path, block: str) -> tuple[str, list[str]]:
    """Run the block's written test on its RTL; its status and each test's outcome."""
    work = out / "sim" / block
    args = {**SIM, "work_root": str(work)}
    args.pop("use")
    spec = CheckSpec(id="sim", capability="sim", adapter="edalize", args=args)
    ctx = ToolContext(repo_root=proj, runner=LocalRunner(), params={"block": block})
    result = asyncio.run(EdalizeTool().run(spec, ctx))
    results = work / "results.xml"
    xml = results.read_text(encoding="utf-8") if results.is_file() else None
    outcome = CocotbParser().parse_results(xml)
    lines = [f"{t.outcome:7} {t.name}" for t in outcome.tests]
    lines.extend(f"  {i.rule}: {i.msg[:160]}" for i in result.issues if i.severity == "error")
    if not outcome.tests:
        lines.append(f"  log: {result.log_tail.strip()[-400:]}")
    return result.status, lines


def main(out: Path) -> int:
    proj = out / "tinysoc"
    events = load_events(out)
    uses = tool_uses(events)
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    queue = TaskQueue(StateLayout(proj))
    records = {r.task_id: r for r in queue.all()}
    bound = {b.agent_id: b.task_id for b in queue.bindings()}
    log = queue.runtime_dir / "guard.log"
    guard = [json.loads(x) for x in log.read_text().splitlines()] if log.is_file() else []

    print("== session")
    print(f"  plugins: {[p.get('name') for p in init.get('plugins', [])]}")
    if init.get("plugin_errors"):
        print(f"  plugin_errors: {init['plugin_errors']}")
    print("== main-session tool calls")
    for use in uses:
        if use.parent is None:
            print(f"  {use.name:48} {json.dumps(use.input)[:100]}")

    task_ids = [TASKS[b] for b in BLOCKS]
    runs = task_runs(uses, task_ids)
    fingerprints = rtl_fingerprints(proj)
    verdicts: dict[str, bool] = {}
    for block in BLOCKS:
        task_id = TASKS[block]
        run = runs[task_id]
        record = records.get(task_id)
        print(f"== task {task_id}")
        print(
            f"  status: {record.status if record else 'never queued'}"
            + (f" attempts={record.attempts}/{record.tries}" if record else "")
        )
        if record is not None and record.reasons:
            print(f"  last reasons: {[r[:200] for r in record.reasons]}")
        for agent in run.agents:
            print(
                f"  subagent: {agent.input.get('subagent_type')} model={agent.input.get('model')}"
            )
        print(f"  tools used: {dict(Counter(u.name for u in run.calls))}")
        for use in run.calls:
            if use.is_error:
                print(f"  refused {use.name}: {use.result[:160]}")
        for g in guard:
            if g.get("decision") == "deny" and bound.get(g.get("agent_id") or "") == task_id:
                print(f"  guard deny {g['tool']}: {g['detail'][:160]}")
        written = (proj / OUTPUTS[block]).is_file()
        static_ok, static_issues = tb_static(proj, block) if written else (False, [])
        ran = bool(run.agents) and all(
            u.input.get("subagent_type") == "chipgraph:tb-author" for u in run.agents
        )
        reads = rtl_reads(run, fingerprints)
        a, b, c = written, static_ok, ran and not reads and bool(fingerprints)
        print(f"  (a) wrote {OUTPUTS[block]}: {a}")
        print(f"  (b) tb_static passed: {b}")
        for line in static_issues:
            print(f"      {line}")
        print(f"  (c) ran as chipgraph:tb-author: {ran}; read no RTL and saw none "
              f"({len(fingerprints)} RTL lines checked): {not reads}")  # fmt: skip
        for item in reads:
            print(f"      - {item}")
        verdicts[block] = a and b and c

    print("== result event")
    for key in ("subtype", "is_error", "num_turns", "duration_ms", "total_cost_usd", "modelUsage"):
        if key in result:
            print(f"  {key}: {json.dumps(result[key])[:400]}")
    timing = out / "timing.txt"
    print(f"== timing: {timing.read_text().strip() if timing.is_file() else '?'}")

    print("== the written tests on the RTL (informational, not part of PASS)")
    for block in BLOCKS:
        if not (proj / OUTPUTS[block]).is_file():
            print(f"  {block}: no test written")
            continue
        status, lines = simulate(out, proj, block)
        print(f"  {block}: sim {status}")
        for line in lines:
            print(f"    {line}")

    ok = all(verdicts.values()) and len(verdicts) == len(BLOCKS)
    print("== verdict")
    for block, passed in verdicts.items():
        print(f"  {TASKS[block]}: {'PASS' if passed else 'FAIL'}")
    print(f"  {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
