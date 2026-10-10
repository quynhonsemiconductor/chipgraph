"""Summarise one M2-02b acceptance run (`run.sh`): per task the dispatches, labels, tiers
and models, and final status; then the verdict.

    uv run python docs/agent-loop-claude-code/report.py OUT_DIR

Reads OUT_DIR/stream.jsonl (the `claude -p` stream-json), the project's chipgraph state
(task queue, run journals, HANDOFF.md) and OUT_DIR/build.json.

PASS needs all of:

(a) `loop/fixable` accepted on its 2nd dispatch, after a rejection with a label, and
    the subagent's 2nd `get_context` result carried that label (`previous_label` and
    the redo text, which names it);
(b) `loop/hopeless` `budget_exhausted` after exactly 2 dispatches; its HANDOFF.md
    exists and names the instance, the label and the stop reason;
(c) no Agent call for `loop/hopeless` after the submit that exhausted it, and at most
    2 Agent calls for it in all;
(d) the main session stayed below `MAX_MAIN_TURNS` turns and made at most
    `MAX_ROUNDS` `next_task` calls;
(e) the run's total cost is printed (`total_cost_usd` of the result event).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from chipgraph.core.runtime import AgentTaskRecord, TaskQueue
from chipgraph.core.state import journal as journal_mod
from chipgraph.core.state.layout import StateLayout

HERE = Path(__file__).resolve().parent
MCP = "mcp__plugin_chipgraph_chipgraph__"
AGENT_TOOLS = frozenset({"Agent", "Task"})
MAX_MAIN_TURNS = 30
"""PASS bound on the main session's turns. The loop needs about 4 rounds of
`next_task`, Agent calls and submits (~14 tool calls); 30 leaves room for the summary
and a stray call, and stays below run.sh's hard `--max-turns 40`."""
MAX_ROUNDS = 12
"""The prompt-level backstop of `/chipgraph:run`: at most 12 `next_task` calls."""


def _load_fixture() -> ModuleType:
    name = "agent_loop_fixture"  # not `fixture`: other acceptance scripts have one
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HERE / "fixture.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fx = _load_fixture()


@dataclass
class Use:
    """One tool call in the stream, and its result if the stream has one."""

    id: str
    parent: str | None
    name: str
    input: dict[str, Any]
    order: int
    result: str = ""
    is_error: bool = False


@dataclass
class TaskRun:
    """What happened to one task: its Agent calls, its submits, its contexts."""

    task_id: str
    agents: list[Use] = field(default_factory=list)
    submits: list[Use] = field(default_factory=list)
    contexts: list[Use] = field(default_factory=list)


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
    """Every tool call, in stream order, with its result joined on."""
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
                    order=len(uses),
                )
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in uses:
                use = uses[block["tool_use_id"]]
                use.result = _text(block.get("content"))
                use.is_error = bool(block.get("is_error"))
    return list(uses.values())


def _json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def task_runs(uses: list[Use], task_ids: list[str]) -> dict[str, TaskRun]:
    """Per task: the Agent calls naming it, the submits of it, its subagents' contexts."""
    runs = {task_id: TaskRun(task_id) for task_id in task_ids}
    owner: dict[str, str] = {}
    for use in uses:
        if use.parent is None and use.name in AGENT_TOOLS:
            text = json.dumps(use.input)
            for task_id in task_ids:
                if task_id in text:
                    runs[task_id].agents.append(use)
                    owner[use.id] = task_id
                    break
        elif use.parent is None and use.name == f"{MCP}submit":
            task_id = str(use.input.get("task_id", ""))
            if task_id in runs:
                runs[task_id].submits.append(use)
    for use in uses:
        if use.name == f"{MCP}get_context" and use.parent in owner:
            runs[owner[use.parent]].contexts.append(use)
    return runs


def submit_turns(proj: Path, task_id: str) -> list[dict[str, Any]]:
    """The `agent_turn` submit payloads of a task, over every run's journal, in order."""
    layout = StateLayout(proj)
    turns: list[tuple[str, dict[str, Any]]] = []
    if not layout.runs_dir.is_dir():
        return []
    for run_dir in sorted(layout.runs_dir.iterdir()):
        journal = run_dir / "journal.jsonl"
        if not journal.is_file():
            continue
        for event in journal_mod.read(journal).events:
            if (
                event.type == "agent_turn"
                and event.rule_instance == task_id
                and event.payload.get("phase") == "submit"
            ):
                turns.append((event.ts.isoformat(), dict(event.payload)))
    return [payload for _, payload in sorted(turns, key=lambda item: item[0])]


def _exhausting_submit(run: TaskRun) -> Use | None:
    for use in run.submits:
        if _json(use.result).get("status") == "budget_exhausted":
            return use
    return None


def verdict_fixable(
    record: AgentTaskRecord | None, run: TaskRun, turns: list[dict[str, Any]]
) -> tuple[bool, list[str]]:
    """(ok, notes) for criterion (a)."""
    notes: list[str] = []
    accepted = record is not None and record.status == "accepted"
    second = record is not None and record.dispatches == 2
    first_label = turns[0].get("label") if turns else None
    labelled = len(turns) >= 2 and first_label is not None and turns[-1].get("label") is None
    carried = False
    if len(run.contexts) >= 2 and first_label:
        context = _json(run.contexts[1].result)
        redo = " ".join(context.get("previous_rejection") or [])
        carried = context.get("previous_label") == first_label and f"({first_label})" in redo
    notes.append(f"accepted={accepted} dispatches={record.dispatches if record else None}")
    notes.append(f"first rejection label={first_label} second context carried it={carried}")
    return accepted and second and labelled and carried, notes


def verdict_hopeless(record: AgentTaskRecord | None) -> tuple[bool, list[str], str]:
    """(ok, notes, HANDOFF text) for criterion (b)."""
    if record is None:
        return False, ["never queued"], ""
    state = record.state
    handoff = Path(state.handoff) if state.handoff else None
    text = handoff.read_text(encoding="utf-8") if handoff and handoff.is_file() else ""
    names = (
        bool(text)
        and f"`{record.task_id}` [{state.label}]" in text
        and f"reason `{state.stop}`" in text
    )
    ok = record.status == "budget_exhausted" and record.dispatches == fx.HOPELESS_TRIES and names
    notes = [
        f"status={record.status} dispatches={record.dispatches} label={state.label} "
        f"reason={state.stop}",
        f"HANDOFF {handoff} exists={bool(text)} names instance, label, reason={names}",
    ]
    return ok, notes, text


def verdict_no_agent_after(run: TaskRun) -> tuple[bool, list[str]]:
    """(ok, notes) for criterion (c)."""
    last = _exhausting_submit(run)
    after = [a for a in run.agents if last is not None and a.order > last.order]
    ok = last is not None and not after and len(run.agents) <= fx.HOPELESS_TRIES
    return ok, [f"Agent calls={len(run.agents)}, after exhaustion={len(after)}"]


def main(out: Path) -> int:
    proj = out / "tinysoc"
    events = load_events(out)
    uses = tool_uses(events)
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    queue = TaskQueue(StateLayout(proj))
    records = {r.task_id: r for r in queue.all()}

    print("== session")
    print(f"  plugins: {[p.get('name') for p in init.get('plugins', [])]}")
    if init.get("plugin_errors"):
        print(f"  plugin_errors: {init['plugin_errors']}")
    print("== main-session tool calls")
    for use in uses:
        if use.parent is None:
            print(f"  {use.name:48} {json.dumps(use.input)[:100]}")

    runs = task_runs(uses, [fx.FIXABLE_TASK, fx.HOPELESS_TASK])
    turns = {task_id: submit_turns(proj, task_id) for task_id in runs}
    for task_id, run in runs.items():
        record = records.get(task_id)
        print(f"== task {task_id}")
        if record is None:
            print("  never queued")
            continue
        state = record.state
        print(
            f"  final status: {record.status}; dispatches: {record.dispatches}; "
            f"tries used {state.tries_used}/{record.tries}; infra retries {state.infra_failures}"
        )
        print(f"  labels per submit: {[t.get('label') for t in turns[task_id]]}")
        print(f"  tiers per submit:  {[t.get('tier') for t in turns[task_id]]}")
        print(
            "  Agent calls (subagent_type, model): "
            f"{[(a.input.get('subagent_type'), a.input.get('model')) for a in run.agents]}"
        )
        if state.stop:
            print(f"  stopped: {state.stop}; HANDOFF: {state.handoff}")

    rounds = sum(1 for u in uses if u.parent is None and u.name == f"{MCP}next_task")
    turns_used = result.get("num_turns")
    cost = result.get("total_cost_usd")
    build = (out / "build.json").read_text() if (out / "build.json").is_file() else ""
    print(f"== chipgraph build after the run: {build.strip()[:400]}")
    print("== result event")
    for key in ("subtype", "is_error", "num_turns", "duration_ms", "total_cost_usd", "modelUsage"):
        if key in result:
            print(f"  {key}: {json.dumps(result[key])[:400]}")
    timing = out / "timing.txt"
    print(f"== timing: {timing.read_text().strip() if timing.is_file() else '?'}")

    a, a_notes = verdict_fixable(
        records.get(fx.FIXABLE_TASK), runs[fx.FIXABLE_TASK], turns[fx.FIXABLE_TASK]
    )
    b, b_notes, handoff = verdict_hopeless(records.get(fx.HOPELESS_TASK))
    c, c_notes = verdict_no_agent_after(runs[fx.HOPELESS_TASK])
    d = isinstance(turns_used, int) and turns_used < MAX_MAIN_TURNS and rounds <= MAX_ROUNDS
    e = isinstance(cost, int | float)
    if handoff:
        print("== HANDOFF.md of the exhausted task")
        print("  " + handoff.replace("\n", "\n  ").rstrip())
    print("== verdict")
    print(f"  (a) fixable accepted on attempt 2 with the label in its context: {a}")
    for note in a_notes:
        print(f"      {note}")
    print(f"  (b) hopeless budget_exhausted after {fx.HOPELESS_TRIES} dispatches, HANDOFF: {b}")
    for note in b_notes:
        print(f"      {note}")
    print(f"  (c) no Agent call for hopeless after exhaustion: {c}")
    for note in c_notes:
        print(f"      {note}")
    print(
        f"  (d) main-session turns {turns_used} < {MAX_MAIN_TURNS}, "
        f"next_task rounds {rounds} <= {MAX_ROUNDS}: {d}"
    )
    print(f"  (e) total cost: {f'${cost:.4f}' if e else 'not reported'}")
    ok = a and b and c and d and e
    print(f"  {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
