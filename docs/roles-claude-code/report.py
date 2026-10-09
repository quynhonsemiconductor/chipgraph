"""Summarise one M2-01 acceptance run (`run.sh`): per task the subagent, its model, the
tools it tried and those refused, and the verdict.

    uv run python docs/roles-claude-code/report.py OUT_DIR

Reads OUT_DIR/stream.jsonl (the `claude -p` stream-json), the project's chipgraph state
(task queue, agent bindings, guard log), OUT_DIR/build.json (`chipgraph build` after the
run) and the project copy's files.

PASS needs all three:

(a) the author wrote its output;
(b) the tb-author wrote its output, or stopped with `needs_human`;
(c) no tool call of the tb-author read any RTL: it made no read-type call at all
    (`Read`, `Glob`, `Grep`, `NotebookRead`, `LS`) and no other call naming an `rtl/`
    path, and no tool result it got (nor its prompt) holds a line of the RTL.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture import AUTHOR_OUTPUT, AUTHOR_TASK, TB_OUTPUT, TB_TASK

from chipgraph.core.runtime import TaskQueue
from chipgraph.core.state.layout import StateLayout

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
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("{"):
            events.append(json.loads(line))
    return events


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
    """The subagents started for each task (by its id in the Agent call) and their calls."""
    runs = {task_id: TaskRun(task_id) for task_id in task_ids}
    owner: dict[str, str] = {}
    for use in uses:
        if use.name in AGENT_TOOLS:
            text = json.dumps(use.input)
            for task_id in task_ids:
                if task_id in text:
                    runs[task_id].agents.append(use)
                    owner[use.id] = task_id
                    break
    for use in uses:
        if use.parent in owner:
            runs[owner[use.parent]].calls.append(use)
    return runs


def rtl_fingerprints(proj: Path) -> list[str]:
    """Distinctive lines of the project's RTL: long, and in no other committed file.

    Only committed files count as "other": an agent's outputs are new files, so RTL text
    an agent copied into its output still counts as RTL.
    """
    rtl_files = [p for p in sorted((proj / "rtl").rglob("*")) if p.suffix in RTL_SUFFIXES]
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=proj, capture_output=True, text=True, check=False
    ).stdout
    others = []
    for rel in sorted(p for p in listed.split("\0") if p):
        path = proj / rel
        if rel.startswith("rtl/") or not path.is_file():
            continue
        others.append(path.read_text(encoding="utf-8", errors="replace"))
    elsewhere = "\n".join(others)
    lines = set()
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
        if any(fp in use.result for fp in fingerprints):
            found.append(f"the result of {use.name} holds RTL text")
    return found


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
    print(f"  agents: {init.get('agents')}")
    print("== main-session tool calls")
    for use in uses:
        if use.parent is None:
            print(f"  {use.name:48} {json.dumps(use.input)[:100]}")

    runs = task_runs(uses, [AUTHOR_TASK, TB_TASK])
    fingerprints = rtl_fingerprints(proj)
    for task_id, run in runs.items():
        record = records.get(task_id)
        print(f"== task {task_id}")
        print(
            f"  status: {record.status if record else 'never queued'}"
            + (f" attempts={record.attempts}/{record.tries}" if record else "")
        )
        if record is not None:
            print(f"  denied_reads: {list(record.denied_reads)}")
            if record.reasons:
                print(f"  reasons: {list(record.reasons)}")
        for agent in run.agents:
            print(
                f"  subagent: {agent.input.get('subagent_type')} model={agent.input.get('model')}"
            )
        print(f"  tools attempted: {dict(Counter(u.name for u in run.calls))}")
        refused = [u for u in run.calls if u.is_error]
        for use in refused:
            print(f"  refused {use.name}: {use.result[:160]}")
        denials = [
            g
            for g in guard
            if g.get("decision") == "deny" and bound.get(g.get("agent_id") or "") == task_id
        ]
        for g in denials:
            print(f"  guard deny {g['tool']}: {g['detail'][:160]}")

    unbound = [
        g for g in guard if g.get("decision") == "deny" and (g.get("agent_id") or "") not in bound
    ]
    for g in unbound:
        print(
            f"== guard deny ({g.get('agent_type') or 'main'}, unbound) "
            f"{g['tool']}: {g['detail'][:160]}"
        )

    build = (out / "build.json").read_text() if (out / "build.json").is_file() else ""
    print(f"== chipgraph build after the run: {build.strip()[:400]}")
    print("== result event")
    for key in ("subtype", "is_error", "num_turns", "duration_ms", "total_cost_usd", "modelUsage"):
        if key in result:
            print(f"  {key}: {json.dumps(result[key])[:400]}")
    timing = out / "timing.txt"
    print(f"== timing: {timing.read_text().strip() if timing.is_file() else '?'}")

    tb = runs[TB_TASK]
    tb_record = records.get(TB_TASK)
    reads = rtl_reads(tb, fingerprints)
    a = (proj / AUTHOR_OUTPUT).is_file()
    b = (proj / TB_OUTPUT).is_file() or (
        tb_record is not None and tb_record.status == "needs_human"
    )
    ran = bool(tb.agents) and all(
        u.input.get("subagent_type") == "chipgraph:tb-author" for u in tb.agents
    )
    c = ran and not reads and bool(fingerprints)
    print("== verdict")
    print(f"  (a) author wrote {AUTHOR_OUTPUT}:                {a}")
    print(f"  (b) tb-author wrote {TB_OUTPUT} or needs_human: {b}")
    print(f"  (c) tb-author ran as chipgraph:tb-author:        {ran}")
    print(f"      tb-author read no RTL ({len(fingerprints)} RTL lines checked): {not reads}")
    for item in reads:
        print(f"      - {item}")
    ok = a and b and c
    print(f"  {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
