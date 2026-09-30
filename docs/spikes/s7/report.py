"""S7 spike: summarise one run from its stream-json, hook log and server state.

    python3 report.py OUT_DIR

Answers the five S7 questions from evidence: did the loop run, did subagents run (and in
parallel), was a write outside `outputs` refused, which model each part used, and how
many tokens / how much cost the run reported.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(out: Path) -> None:
    events = [
        json.loads(line)
        for line in (out / "stream.jsonl").read_text().splitlines()
        if line.strip().startswith("{")
    ]
    tool_calls: list[tuple[str, str]] = []
    for ev in events:
        msg = ev.get("message") or {}
        for block in msg.get("content") or [] if isinstance(msg, dict) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name", "")
                detail = json.dumps(block.get("input", {}))[:80]
                tool_calls.append((name, detail))
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})

    proj = out / "tinysoc"
    state_file = proj / ".s7" / "state.json"
    state = json.loads(state_file.read_text()) if state_file.is_file() else {}
    hook_file = proj / ".s7" / "hook.log"
    hook = (
        [json.loads(x) for x in hook_file.read_text().splitlines()] if hook_file.is_file() else []
    )

    print("== tool calls (main session stream)")
    for name, detail in tool_calls:
        print(f"  {name:32} {detail}")
    print("== submit results", json.dumps(state.get("submitted", {}), indent=1))
    print("== hook decisions")
    for h in hook:
        print(f"  {h['decision']:5} agent={h.get('agent_type')} {h['detail']}")
    print("== result")
    for key in ("subtype", "num_turns", "duration_ms", "total_cost_usd", "usage", "modelUsage"):
        if key in result:
            print(f"  {key}: {json.dumps(result[key])[:400]}")
    print(
        "== timing",
        (out / "timing.txt").read_text().strip() if (out / "timing.txt").exists() else "",
    )
    # Which tools each subagent really called: from the hook log (every tool, with
    # agent_id) and from the stream (tool_use blocks carrying parent_tool_use_id).
    by_agent: dict[str, dict[str, int]] = {}
    for h in hook:
        who = f"{h.get('agent_type') or 'main'}:{(h.get('agent_id') or '-')[:8]}"
        tools = by_agent.setdefault(who, {})
        tools[str(h.get("tool"))] = tools.get(str(h.get("tool")), 0) + 1
    print("== tools per agent (hook log)")
    for who, tools in sorted(by_agent.items()):
        print(f"  {who:24} {json.dumps(tools, sort_keys=True)}")
    sub_bash = [
        e
        for e in events
        if e.get("parent_tool_use_id")
        for b in ((e.get("message") or {}).get("content") or [])
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Bash"
    ]
    hook_bash = [h for h in hook if h.get("agent_id") and h.get("tool") == "Bash"]
    print(f"== subagent Bash calls: stream={len(sub_bash)} hook={len(hook_bash)}")
    agents = [c for c in tool_calls if c[0] == "Agent"]
    denied = [h for h in hook if h["decision"] == "deny"]
    print(
        f"== verdicts: agent_calls={len(agents)} denied_writes={len(denied)} "
        f"accepted={sum(1 for s in state.get('submitted', {}).values() if s.get('accepted'))}"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
