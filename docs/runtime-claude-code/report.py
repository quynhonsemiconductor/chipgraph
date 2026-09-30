"""Summarise one M1-11 acceptance run (`run.sh`): evidence for each accept criterion.

    uv run python docs/runtime-claude-code/report.py OUT_DIR

Reads OUT_DIR/stream.jsonl (the `claude -p` stream), the project's chipgraph state
(task queue, agent bindings, guard log), OUT_DIR/build.json (the `chipgraph build`
after the run) and `git status` of the project copy.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

from chipgraph.core.runtime import TaskQueue
from chipgraph.core.state.layout import StateLayout


def _events(out: Path) -> list[dict]:
    path = out / "stream.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text().splitlines() if line.strip().startswith("{")
    ]


def _tool_uses(events: list[dict]) -> list[tuple[str | None, str, dict]]:
    uses = []
    for ev in events:
        msg = ev.get("message")
        if not isinstance(msg, dict):
            continue
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                uses.append(
                    (ev.get("parent_tool_use_id"), block.get("name", ""), block.get("input", {}))
                )
    return uses


def main(out: Path) -> int:
    proj = out / "tinysoc"
    events = _events(out)
    uses = _tool_uses(events)
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})

    print("== session")
    print(f"  plugins: {[p.get('name') for p in init.get('plugins', [])]}")
    if init.get("plugin_errors"):
        print(f"  plugin_errors: {init['plugin_errors']}")
    print(f"  mcp_servers: {init.get('mcp_servers')}")

    print("== main-session tool calls")
    for parent, name, args in uses:
        if parent is None:
            print(f"  {name:48} {json.dumps(args)[:100]}")
    agents = [
        (a.get("subagent_type"), a.get("model")) for p, n, a in uses if p is None and n == "Agent"
    ]
    print(f"== Agent calls (subagent_type, model): {agents}")
    sub = Counter(name for parent, name, _ in uses if parent is not None)
    print(f"== subagent tool calls (stream): {dict(sub)}")

    queue = TaskQueue(StateLayout(proj))
    print("== task queue")
    for record in queue.all():
        print(
            f"  {record.task_id}: status={record.status} dispatches={record.dispatches} "
            f"attempts={record.attempts}/{record.tries} reasons={list(record.reasons)}"
        )
    for binding in queue.bindings():
        print(
            f"  bound {binding.agent_type}:{binding.agent_id[:10]} -> {binding.task_id} "
            f"(dispatch {binding.dispatch}, {binding.tool_calls} tool calls)"
        )

    log = queue.runtime_dir / "guard.log"
    lines = [json.loads(x) for x in log.read_text().splitlines()] if log.is_file() else []
    print(f"== guard decisions: {dict(Counter(line['decision'] for line in lines))}")
    for line in lines:
        if line["decision"] == "deny":
            print(f"  deny {line.get('agent_type') or 'main'} {line['tool']}: {line['detail']}")
    per_agent: dict[str, Counter] = {}
    for line in lines:
        who = f"{line.get('agent_type') or 'main'}:{(line.get('agent_id') or '-')[:10]}"
        per_agent.setdefault(who, Counter())[str(line["tool"])] += 1
    for who, tools in sorted(per_agent.items()):
        print(f"  {who:32} {dict(tools)}")
    sub_bash = [x for x in lines if x.get("agent_id") and x["tool"] in ("Bash", "PowerShell")]

    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=proj,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines()
    print(f"== git status of the project: {status}")
    build = (out / "build.json").read_text() if (out / "build.json").is_file() else ""
    print(f"== chipgraph build after the run: {build.strip()[:600]}")

    print("== result event")
    for key in (
        "subtype",
        "is_error",
        "num_turns",
        "duration_ms",
        "total_cost_usd",
        "usage",
        "modelUsage",
    ):
        if key in result:
            print(f"  {key}: {json.dumps(result[key])[:600]}")
    timing = out / "timing.txt"
    print(f"== timing: {timing.read_text().strip() if timing.is_file() else '?'}")

    records = queue.all()
    accepted = [r for r in records if r.status == "accepted"]
    try:
        summary = json.loads(build)
        build_ok = not summary.get("failed") and not summary.get("waiting_gate")
    except json.JSONDecodeError:
        build_ok = False
    changed = sorted(line[3:] for line in status)
    print("== verdict")
    print(f"  agent task accepted:            {bool(accepted)}")
    print(f"  build finished after the run:   {build_ok}")
    print(f"  only the output changed:        {changed == ['rtl/tiny_pulse.sv']} {changed}")
    print(f"  subagent shell calls (guard):   {len(sub_bash)}")
    print("  no API key used:                ANTHROPIC_API_KEY unset by run.sh")
    return 0 if accepted and build_ok else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
