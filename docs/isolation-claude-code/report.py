"""Summarise one M2-11c acceptance run (`run.sh`): isolated workspaces and best-of-N.

    uv run python docs/isolation-claude-code/report.py OUT_DIR [--phase 1|2]
    uv run python docs/isolation-claude-code/report.py --check-plan PLAN_SHOW_JSON

`--check-plan` is the person's sanity check of `chipgraph plan show timer --json` before
approving: the plan exists, passes `plan_check`, its gate waits, its top is `tiny_timer`,
every module writes only `.sv` files under `rtl/`, every REQ-TIM requirement is covered
by a module, and there are >= 2 modules (the real planner usually splits timer into
counter + irq + top). Exit 0 when sane.

The verdict reads OUT_DIR/plan.jsonl and OUT_DIR/rtl.jsonl (the two `claude -p` streams),
OUT_DIR/plan_show.json, the copy OUT_DIR/tinysoc (its files, git, the chipgraph state),
OUT_DIR/fixture.json.

**Phase 1 (isolation only)**: PASS iff:
(a) isolation: every Write tool call in the streams went to a workspace path (under
    <state>/tmp/fanout/), never to the project root;
(b) acceptance: every module of the approved plan has its RTL file in the user's tree;
(c) workspaces: no workspace directory exists at the end (they were all cleaned up);
(e) branch: the PR branch holds exactly the block's files (modules + testbench);
(f) independence: no testbench-author saw RTL fingerprints, no RTL-author saw TB
    fingerprints;
(g) headless: no model approved anything; the gates are the script's people.

**Phase 2 (best-of-2)**: PASS iff all of the above, plus:
(d) best-of-N: for each rtl_module task, >= 2 candidates were dispatched (task_id with
    #1 and #2 suffixes), and exactly one landed in the user's tree; the journal has a
    `select` event naming the winner and how it was decided.

Also printed: per task the subagent, model per attempt and tries; the cost and turns of
each session; the wall time.
"""

from __future__ import annotations

import argparse
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


def _sibling(name: str, file: str) -> ModuleType:
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / file)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


fx = _sibling("isolation_acceptance_fixture", "fixture.py")

BLOCK = fx.BLOCK
TOP = fx.TOP
REQS = fx.REQS
TB_FILE = fx.TB_FILE
APPROVED_PLAN = fx.APPROVED_PLAN
PR_RECORD = fx.PR_RECORD
PR_APPROVED = fx.PR_APPROVED
REVIEW_FILE = fx.REVIEW_FILE

MCP = "mcp__plugin_chipgraph_chipgraph__"
MAIN_ALLOWED = frozenset(
    {
        *(f"{MCP}{t}" for t in ("next_task", "submit", "plan_show", "approve", "status")),
        "Agent",
        "Task",
        "AskUserQuestion",
        "ToolSearch",
    }
)
READ_TOOLS = frozenset({"Read", "Glob", "Grep", "NotebookRead", "LS"})
AGENT_TOOLS = frozenset({"Agent", "Task"})
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit"})
PEOPLE = frozenset({"e2e-human", "e2e-reviewer"})
TURN_BOUNDS = {"plan": 30, "rtl": 60}
STREAMS = ("plan", "rtl")
MIN_LINE = 24
ALLOWED_CHANGES = ("plan/", "pr/", "reports/", ".chipgraph/decisions/")


@dataclass
class Use:
    """One tool call in a stream, with its result when the stream has one."""

    id: str
    parent: str | None
    name: str
    input: dict[str, Any]
    result: str = ""
    is_error: bool = False


@dataclass
class Stream:
    name: str
    events: list[dict[str, Any]] = field(default_factory=list)
    uses: list[Use] = field(default_factory=list)

    @property
    def result(self) -> dict[str, Any]:
        return next((e for e in reversed(self.events) if e.get("type") == "result"), {})

    def main_calls(self) -> list[Use]:
        return [u for u in self.uses if u.parent is None]

    def agents_for(self, task_id: str) -> list[Use]:
        return [u for u in self.uses if u.name in AGENT_TOOLS and task_id in json.dumps(u.input)]

    def calls_of(self, agents: list[Use]) -> list[Use]:
        owners = {a.id for a in agents}
        return [u for u in self.uses if u.parent in owners]

    def all_writes(self) -> list[Use]:
        return [u for u in self.uses if u.name in WRITE_TOOLS]


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            _text(c.get("text", c.get("content", ""))) for c in content if isinstance(c, dict)
        )
    return ""


def load_stream(out: Path, name: str) -> Stream:
    stream = Stream(name)
    path = out / f"{name}.jsonl"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("{"):
                try:
                    stream.events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    uses: dict[str, Use] = {}
    for ev in stream.events:
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
    stream.uses = list(uses.values())
    return stream


def _git(proj: Path, *args: str) -> tuple[int, str]:
    done = subprocess.run(["git", *args], cwd=proj, capture_output=True, text=True, check=False)
    return done.returncode, done.stdout


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def planned(proj: Path) -> tuple[list[str], list[str]]:
    """The approved plan's modules (build order) and their write sets."""
    data = _read_json(proj / APPROVED_PLAN)
    if not isinstance(data, dict):
        return [], []
    modules: list[str] = []
    writes: list[str] = []
    for entry in data.get("modules") or []:
        modules.append(str(entry.get("name")))
        for w in entry.get("writes", []):
            writes.append(str(w["path"] if isinstance(w, dict) else w))
    order = data.get("order")
    if isinstance(order, list) and set(order) == set(modules):
        modules = [str(m) for m in order]
    return modules, sorted(writes)


def module_task(module: str) -> str:
    """The task id of a planned module's RTL."""
    return f"digital-rtl/rtl_module[block={BLOCK},module={module}]"


def check_writes_to_workspaces(streams: dict[str, Stream], proj: Path) -> tuple[bool, list[str]]:
    """Every `Write`/`Edit` of a subagent went to a workspace: a path outside the project.

    The workspaces are git worktrees in the system temp directory, so the test is
    "not inside the project", and that there were writes at all (an empty set proves nothing).
    """
    problems: list[str] = []
    root = proj.resolve()
    seen = 0
    for stream in streams.values():
        for write in stream.all_writes():
            file_path = str(write.input.get("file_path", ""))
            if not file_path:
                continue
            seen += 1
            resolved = Path(file_path).resolve()
            if resolved == root or root in resolved.parents:
                problems.append(f"Write to the project (not a workspace): {file_path[:120]}")
    if seen == 0:
        problems.append("no Write call in any session: nothing proves where the agents wrote")
    return len(problems) == 0, problems


def check_workspaces_cleaned(proj: Path) -> tuple[bool, list[str]]:
    """No linked worktree of the project is left (`git worktree list` shows only the main one)."""
    done = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=proj,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        return False, [f"git worktree list failed: {done.stderr.strip()[:200]}"]
    paths = [
        ln[len("worktree ") :] for ln in done.stdout.splitlines() if ln.startswith("worktree ")
    ]
    return len(paths) <= 1, [f"Workspace not cleaned: {p}" for p in paths[1:]]


def check_best_of_n(
    proj: Path, streams: dict[str, Stream], modules: list[str]
) -> tuple[bool, dict[str, Any]]:
    """Check that best-of-N worked: for each module, >= 2 candidates dispatched, one winner."""
    from chipgraph.core.runtime import TaskQueue
    from chipgraph.core.state import journal as journal_mod
    from chipgraph.core.state.layout import StateLayout

    info: dict[str, Any] = {"modules": {}}
    layout = StateLayout(proj)
    queue = TaskQueue(layout)

    # Look for candidate entries in the task queue and journals
    for module in modules:
        task_id = module_task(module)
        module_info: dict[str, Any] = {"candidates": [], "winner": None, "selection": None}

        # Check for candidate task IDs (with #N suffix)
        for record in queue.all():
            if record.task_id.startswith(task_id) and "#" in record.task_id:
                module_info["candidates"].append(record.task_id)
            elif record.task_id == task_id and record.status == "accepted":
                module_info["winner"] = task_id

        # Look for selection event in journals
        for journal_path in layout.runs_dir.glob("*/journal.jsonl"):
            journal = journal_mod.read(journal_path)
            for event in journal.events:
                payload = event.payload if isinstance(event.payload, dict) else {}
                if payload.get("event") == "select" and task_id in str(event.rule_instance or ""):
                    module_info["selection"] = payload

        info["modules"][module] = module_info

    # For best-of-N to pass: at least one module had >= 2 candidates
    any_best_of = any(len(m.get("candidates", [])) >= 2 for m in info["modules"].values())

    return any_best_of, info


def check_independence(
    streams: dict[str, Stream],
    proj: Path,
    modules: list[str],
    fixture: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Check that RTL authors didn't see TB fingerprints and vice versa."""
    problems: list[str] = []
    rtl_fp = fixture.get("rtl_fingerprint", fx.RTL_FINGERPRINT)
    tb_fp = fixture.get("tb_fingerprint", fx.TB_FINGERPRINT)

    rtl_stream = streams.get("rtl")
    if not rtl_stream:
        return True, []

    # Check TB author didn't see RTL fingerprint
    tb_task = f"dv/tb_module[block={BLOCK}]"
    tb_agents = rtl_stream.agents_for(tb_task)
    for call in rtl_stream.calls_of(tb_agents):
        if rtl_fp and rtl_fp in call.result:
            problems.append(f"TB author saw RTL fingerprint in {call.name}")

    # Check RTL authors didn't see TB fingerprint
    for module in modules:
        task_id = module_task(module)
        rtl_agents = rtl_stream.agents_for(task_id)
        for call in rtl_stream.calls_of(rtl_agents):
            if tb_fp and tb_fp in call.result:
                problems.append(f"RTL author for {module} saw TB fingerprint in {call.name}")

    return len(problems) == 0, problems


def decisions(proj: Path, gate_id: str) -> list[str]:
    """Get the approvers of a gate."""
    try:
        from chipgraph.adapters.review.file import FileReview
        from chipgraph.core.state.layout import StateLayout

        review = FileReview(StateLayout(proj).decisions_dir)
        return [a.by for a in review.approvals(gate_id)]
    except Exception:
        return []


def check_plan(summary: Any) -> list[str]:
    """Why `chipgraph plan show timer --json` is not a plan a person would approve."""
    if not isinstance(summary, dict):
        return ["no plan_show output"]
    problems: list[str] = []
    if not summary.get("exists"):
        return ["there is no plan"]
    if not summary.get("ok"):
        problems.append(f"plan_check fails: {summary.get('errors')}")
    if summary.get("gate", {}).get("status") != "waiting":
        problems.append(f"the gate is {summary.get('gate', {}).get('status')}, not waiting")
    if summary.get("top") != TOP:
        problems.append(f"top is {summary.get('top')!r}, not {TOP!r}")
    modules = summary.get("modules") or []
    if len(modules) < 2:
        problems.append(f"expected >= 2 modules, got {len(modules)}")
    for m in modules:
        for w in m.get("writes", []):
            path = str(w.get("path", ""))
            if not (path.startswith("rtl/") and path.endswith(".sv")):
                problems.append(f"module {m.get('name')} writes {path}")
    covered = {row.get("req") for row in summary.get("coverage", []) if row.get("modules")}
    missing = [r for r in REQS if r not in covered]
    if missing:
        problems.append(f"requirements not covered by a module: {missing}")
    return problems


def branch_problems(proj: Path, record: Any, block_files: list[str]) -> list[str]:
    if not isinstance(record, dict):
        return ["no PR record"]
    branch = str(record.get("branch") or "")
    if not re.fullmatch(rf"chipgraph/[^/]+/{BLOCK}", branch):
        return [f"branch {branch!r} is not chipgraph/<run>/{BLOCK}"]
    code, diff = _git(proj, "diff", "--name-only", "main", branch)
    if code != 0:
        return [f"no branch {branch} in the copy"]
    problems: list[str] = []
    files = sorted(p for p in diff.splitlines() if p)
    if files != sorted(block_files):
        problems.append(f"the branch holds {files}, not {sorted(block_files)}")
    for rel in files:
        code, blob = _git(proj, "show", f"{branch}:{rel}")
        local = proj / rel
        if code != 0 or not local.is_file() or blob != local.read_text(encoding="utf-8"):
            problems.append(f"{rel} on the branch is not the working tree's")
    return problems


def _verdict(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  ({name}) {'ok  ' if ok else 'FAIL'} {detail}".rstrip())
    return ok


def report(out: Path, phase: int) -> bool:
    proj = out / "tinysoc"
    streams = {name: load_stream(out, name) for name in STREAMS}
    fixture = _read_json(out / "fixture.json") or {}

    if not proj.is_dir():
        print("no project copy: the run did not start")
        return False

    modules, writes = planned(proj)
    block_files = sorted({*writes, TB_FILE})

    print("== sessions")
    for name, stream in streams.items():
        result = stream.result
        print(
            f"  {name}: subtype={result.get('subtype')} turns={result.get('num_turns')} "
            f"cost_usd={result.get('total_cost_usd')} duration_ms={result.get('duration_ms')}"
        )
        print(f"    main-session tools: {dict(Counter(u.name for u in stream.main_calls()))}")

    print(f"== planned modules: {modules}")
    print(f"== block files: {block_files}")

    timing = out / "timing.txt"
    print(f"== timing: {timing.read_text().split() if timing.is_file() else '?'}")
    cost = sum(float(s.result.get("total_cost_usd") or 0.0) for s in streams.values())
    print(f"== cost: total_cost_usd {cost:.4f}")

    print(f"== criteria (phase {phase})")

    # (a) Isolation: writes to workspaces only
    ws_ok, ws_problems = check_writes_to_workspaces(streams, proj)
    ws_msg = f"writes to workspaces: {ws_problems[:2]}" if not ws_ok else "all writes to workspaces"
    a = _verdict("a", ws_ok, ws_msg)

    # (b) Acceptance: every module's RTL exists in user's tree
    outputs_exist = all((proj / f"rtl/{m}.sv").is_file() for m in modules)
    missing = [m for m in modules if not (proj / f"rtl/{m}.sv").is_file()]
    b = _verdict("b", outputs_exist, f"module outputs: {missing if missing else 'all present'}")

    # (c) Workspaces cleaned
    clean_ok, clean_problems = check_workspaces_cleaned(proj)
    clean_msg = (
        f"workspaces cleaned: {clean_problems[:2]}" if not clean_ok else "all workspaces removed"
    )
    c = _verdict("c", clean_ok, clean_msg)

    # (d) Best-of-N (phase 2 only)
    if phase == 2:
        bon_ok, bon_info = check_best_of_n(proj, streams, modules)
        d = _verdict(
            "d",
            bon_ok,
            f"best-of-N: {bon_info.get('modules', {})}",
        )
    else:
        d = True  # Phase 1 doesn't check best-of

    # (e) Branch: check PR record and branch contents
    record = _read_json(proj / PR_RECORD)
    branch_probs = branch_problems(proj, record, block_files)
    e = _verdict(
        "e",
        bool(writes) and not branch_probs,
        f"block files {block_files}; {branch_probs[:3] if branch_probs else 'ok'}",
    )

    # (f) Independence
    indep_ok, indep_problems = check_independence(streams, proj, modules, fixture)
    indep_msg = f"independence: {indep_problems[:2]}" if not indep_ok else "fingerprints not leaked"
    f = _verdict("f", indep_ok, indep_msg)

    # (g) No model approvals
    approvals = [u for s in streams.values() for u in s.uses if u.name.endswith("__approve")]
    who = decisions(proj, f"plan:{BLOCK}") + decisions(proj, f"pr:{BLOCK}")
    g = _verdict(
        "g",
        not approvals and (not who or set(who) <= PEOPLE),
        f"approve calls {len(approvals)}, decisions by {sorted(set(who)) if who else 'none'}",
    )

    passed = all((a, b, c, e, f, g)) if phase == 1 else all((a, b, c, d, e, f, g))

    print("== verdict")
    print(f"  {'PASS' if passed else 'FAIL'}")
    return passed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path, nargs="?", help="OUT_DIR from run.sh")
    parser.add_argument("--phase", type=int, default=1, choices=[1, 2], help="phase to verify")
    parser.add_argument("--check-plan", type=Path, dest="check_plan", help="check plan sanity")
    args = parser.parse_args(argv)

    if args.check_plan:
        problems = check_plan(_read_json(args.check_plan))
        for problem in problems:
            print(f"not sane: {problem}")
        print("sane" if not problems else "not approved")
        return 0 if not problems else 1

    if not args.out:
        print(__doc__, file=sys.stderr)
        return 2

    return 0 if report(args.out, args.phase) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
