"""Summarise one M2-11b acceptance run (`run.sh`): a block added from its MAS with Claude
Code, from `/chipgraph:plan pwm` to a green PR (offline: a branch), the script acting as
the person at the two gates.

    uv run python docs/rtl-target-claude-code/report.py OUT_DIR
    uv run python docs/rtl-target-claude-code/report.py --check-plan PLAN_SHOW_JSON

`--check-plan` is the person's sanity check of `chipgraph plan show pwm --json` before
approving: the plan exists, passes `plan_check`, its gate waits, its top is `tiny_pwm`,
every module writes only `.sv` files under `rtl/`, and every REQ-PWM requirement is
covered by a module. Exit 0 when sane.

The verdict reads OUT_DIR/plan.jsonl and OUT_DIR/rtl.jsonl (the two `claude -p` streams),
OUT_DIR/plan_show.json, the copy OUT_DIR/tinysoc (its files, git, the chipgraph state:
task queue, journals, gate decisions), OUT_DIR/fixture.json (the user's commit and index
before the run), OUT_DIR/final.* (offline) or OUT_DIR/sandbox.json (SANDBOX=1). PASS iff:

(a) planner: the plan task was done by the `chipgraph:planner` subagent, which wrote the
    plan file, and accepted with `plan_check` passing; `plan_show` says ok;
(b) headless gate: after `/chipgraph:plan` the gate `plan:pwm` still waited, and the
    command's last message gives the `chipgraph approve plan:pwm` command;
(c) no model approved anything: no `approve` tool call in either stream (main session
    or subagent), and every decision on `plan:pwm` and `pr:pwm` is by the script's
    people (`e2e-human`, `e2e-reviewer`);
(d) modules: every module of the approved plan accepted, the last submit's `lint`,
    `naming` and `style` passing;
(e) testbench: accepted, `tb_static` passing;
(f) Critic: its task accepted and `reports/review/pwm.json` a valid `ReviewReport`;
(g) branch: `pr/pwm.json` names `chipgraph/<run>/pwm`, whose diff from `main` is exactly
    the block's files (the modules' write sets and the testbench), each the same bytes as
    in the working tree; the user's HEAD and index are unchanged and every other change
    of the tree is under `plan/`, `pr/`, `reports/` or `.chipgraph/decisions/` (the
    people's gate decisions);
(h) independence: no testbench-author call is read-type and none of its prompt or tool
    results holds RTL text; no RTL-author call is read-type and none of its prompt or
    results holds testbench text (the planted fingerprints and every distinctive line of
    24+ characters, as in the M2-04 and M2-06 reports);
(i) the main sessions used only `next_task`, `submit`, `plan_show`, `approve`, `status`
    (read-only), `Agent`, `AskUserQuestion` (and `ToolSearch`, which only loads schemas);
(j) each session ended by itself (`success`) within its turn bound: plan 30, rtl 60
    main-session turns (`num_turns`);
(k) offline: after the reviewer's `chipgraph approve pr:pwm`, `chipgraph rtl pwm` exits 0
    and `pr/pwm.approved.json` holds the branch's head. SANDBOX=1: the PR was open on
    GitHub with exactly the block's files (then closed, its branches deleted).

Also printed: per task the subagent, model per attempt and tries; the cost and turns of
each session; the wall time.
"""

from __future__ import annotations

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


fx = _sibling("rtl_target_acceptance_fixture", "fixture.py")

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
"""What may change in the user's tree besides the block's files: the plan and its approved
artifact, the PR record, the review report, and the people's gate decisions."""


# --- streams ------------------------------------------------------------------------------


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


# --- the project ----------------------------------------------------------------------------


def _git(proj: Path, *args: str) -> tuple[int, str]:
    done = subprocess.run(["git", *args], cwd=proj, capture_output=True, text=True, check=False)
    return done.returncode, done.stdout


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def last_checks(proj: Path, task_id: str) -> dict[str, str]:
    """Check id -> status of the task's last submit (the journal of its last dispatch)."""
    from chipgraph.core.runtime import TaskQueue
    from chipgraph.core.state import journal as journal_mod
    from chipgraph.core.state.layout import StateLayout

    layout = StateLayout(proj)
    record = TaskQueue(layout).get(task_id)
    if record is None or record.run_id is None or not layout.journal(record.run_id).is_file():
        return {}
    checks: dict[str, str] = {}
    for event in journal_mod.read(layout.journal(record.run_id)).events:
        if event.rule_instance != task_id:
            continue
        if event.type == "agent_turn" and event.payload.get("phase") == "dispatch":
            checks = {}
        elif event.type == "check_result":
            checks[str(event.payload.get("check_id"))] = str(event.payload.get("status"))
    return checks


def planned(proj: Path) -> tuple[list[str], list[str]]:
    """The approved plan's modules (build order) and their write sets."""
    data = _read_json(proj / fx.APPROVED_PLAN)
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


def _distinct(proj: Path, prefix: str, extra: str) -> list[str]:
    """The planted fingerprint plus every line of 24+ characters of the files under
    `prefix` (the working tree) found in no committed file outside it."""
    _, listed = _git(proj, "ls-files", "-z")
    committed = [p for p in listed.split("\0") if p]
    elsewhere = "\n".join(
        (proj / rel).read_text(encoding="utf-8", errors="replace")
        for rel in committed
        if not rel.startswith(prefix) and (proj / rel).is_file()
    )
    lines = {extra}
    for path in sorted((proj / prefix).rglob("*")):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            text = line.strip()
            if len(text) >= MIN_LINE and text not in elsewhere:
                lines.add(text)
    return sorted(lines)


def leaks(stream: Stream, task_id: str, fingerprints: list[str], what: str) -> list[str]:
    """Every way the task's subagent tried to read, or got to see, `what` text."""
    found: list[str] = []
    agents = stream.agents_for(task_id)
    for agent in agents:
        if any(fp in json.dumps(agent.input) for fp in fingerprints):
            found.append(f"its Agent prompt holds {what} text")
    for use in stream.calls_of(agents):
        if use.name in READ_TOOLS:
            found.append(f"{use.name} {json.dumps(use.input)[:120]}")
        hits = [fp for fp in fingerprints if fp in use.result]
        if hits:
            found.append(f"the result of {use.name} holds {what} text: {hits[0][:80]!r}")
    return found


def check_plan(summary: Any) -> list[str]:
    """Why `chipgraph plan show pwm --json` is not a plan a person would approve."""
    if not isinstance(summary, dict):
        return ["no plan_show output"]
    problems: list[str] = []
    if not summary.get("exists"):
        return ["there is no plan"]
    if not summary.get("ok"):
        problems.append(f"plan_check fails: {summary.get('errors')}")
    if summary.get("gate", {}).get("status") != "waiting":
        problems.append(f"the gate is {summary.get('gate', {}).get('status')}, not waiting")
    if summary.get("top") != fx.TOP:
        problems.append(f"top is {summary.get('top')!r}, not {fx.TOP!r}")
    modules = summary.get("modules") or []
    if not modules:
        problems.append("no modules")
    if fx.TOP not in [m.get("name") for m in modules]:
        problems.append(f"no module {fx.TOP}")
    for m in modules:
        for w in m.get("writes", []):
            path = str(w.get("path", ""))
            if not (path.startswith("rtl/") and path.endswith(".sv")):
                problems.append(f"module {m.get('name')} writes {path}")
    covered = {row.get("req") for row in summary.get("coverage", []) if row.get("modules")}
    missing = [r for r in fx.REQS if r not in covered]
    if missing:
        problems.append(f"requirements not covered by a module: {missing}")
    return problems


def tree_problems(proj: Path, out: Path, block_files: list[str]) -> list[str]:
    """How the user's tree changed beyond the block's files, plan/, pr/ and reports/."""
    state = _read_json(out / "fixture.json") or {}
    problems: list[str] = []
    _, head = _git(proj, "rev-parse", "HEAD")
    if head.strip() != state.get("head"):
        problems.append("HEAD moved")
    _, index = _git(proj, "ls-files", "-s")
    if index.strip() != state.get("index"):
        problems.append("the index changed")
    _, status = _git(proj, "status", "--porcelain", "--untracked-files=all")
    for line in status.splitlines():
        path = line[3:].strip()
        if path in block_files or path.startswith(ALLOWED_CHANGES):
            continue
        problems.append(f"changed outside the block: {path}")
    return problems


def branch_problems(proj: Path, record: Any, block_files: list[str]) -> list[str]:
    if not isinstance(record, dict):
        return ["no PR record"]
    branch = str(record.get("branch") or "")
    if not re.fullmatch(rf"chipgraph/[^/]+/{fx.BLOCK}", branch):
        return [f"branch {branch!r} is not chipgraph/<run>/{fx.BLOCK}"]
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


def decisions(proj: Path, gate_id: str) -> list[str]:
    from chipgraph.adapters.review.file import FileReview
    from chipgraph.core.state.layout import StateLayout

    review = FileReview(StateLayout(proj).decisions_dir)
    return [a.by for a in review.approvals(gate_id)]


# --- the verdict --------------------------------------------------------------------------


def _verdict(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  ({name}) {'ok  ' if ok else 'FAIL'} {detail}".rstrip())
    return ok


def report(out: Path) -> bool:
    from chipgraph.core.runtime import TaskQueue
    from chipgraph.core.runtime.roles import parse_review
    from chipgraph.core.state.layout import StateLayout

    proj = out / "tinysoc"
    streams = {name: load_stream(out, name) for name in STREAMS}
    if not proj.is_dir():
        print("no project copy: the run did not start")
        return False
    queue = TaskQueue(StateLayout(proj))
    records = {r.task_id: r for r in queue.all()}
    fixture = _read_json(out / "fixture.json") or {}
    sandbox = fixture.get("review") == "github"
    modules, writes = planned(proj)
    module_tasks = [fx.module_task(m) for m in modules]
    block_files = sorted({*writes, fx.TB_FILE})

    print("== sessions")
    for name, stream in streams.items():
        result = stream.result
        print(
            f"  {name}: subtype={result.get('subtype')} turns={result.get('num_turns')} "
            f"cost_usd={result.get('total_cost_usd')} duration_ms={result.get('duration_ms')}"
        )
        print(f"    main-session tools: {dict(Counter(u.name for u in stream.main_calls()))}")
    print("== tasks (subagent, model per attempt, tries)")
    for task_id in [fx.PLAN_TASK, *module_tasks, fx.TB_TASK, fx.REVIEW_TASK]:
        record = records.get(task_id)
        runs = [a for s in streams.values() for a in s.agents_for(task_id)]
        models = [f"{a.input.get('subagent_type')}:{a.input.get('model')}" for a in runs]
        state = record.state if record else None
        print(
            f"  {task_id}: {record.status if record else 'never queued'}"
            + (f", tries {state.tries_used}/{record.tries}" if record and state else "")
            + f", agents {models}"
        )
    timing = out / "timing.txt"
    print(f"== timing: {timing.read_text().split() if timing.is_file() else '?'}")
    cost = sum(float(s.result.get("total_cost_usd") or 0.0) for s in streams.values())
    print(f"== cost: total_cost_usd {cost:.4f}")

    print("== criteria")
    plan_stream = streams["plan"]
    planner = plan_stream.agents_for(fx.PLAN_TASK)
    plan_writes = [
        u
        for u in plan_stream.calls_of(planner)
        if u.name in WRITE_TOOLS and str(u.input.get("file_path", "")).endswith(fx.PLAN_FILE)
    ]
    plan_record = records.get(fx.PLAN_TASK)
    shown = _read_json(out / "plan_show.json")
    a = _verdict(
        "a",
        bool(planner)
        and all(p.input.get("subagent_type") == "chipgraph:planner" for p in planner)
        and bool(plan_writes)
        and plan_record is not None
        and plan_record.status == "accepted"
        and last_checks(proj, fx.PLAN_TASK).get("plan_check") == "pass"
        and isinstance(shown, dict)
        and bool(shown.get("ok")),
        f"planner runs {len(planner)}, plan writes {len(plan_writes)}, "
        f"plan_check {last_checks(proj, fx.PLAN_TASK).get('plan_check')}",
    )
    last_text = str(plan_stream.result.get("result") or "")
    b = _verdict(
        "b",
        isinstance(shown, dict)
        and shown.get("gate", {}).get("status") == "waiting"
        and "chipgraph approve plan:pwm" in last_text,
        "gate after /chipgraph:plan: "
        + str(shown.get("gate", {}).get("status") if isinstance(shown, dict) else None),
    )
    approvals = [u for s in streams.values() for u in s.uses if u.name.endswith("__approve")]
    who = decisions(proj, "plan:pwm") + decisions(proj, "pr:pwm")
    c = _verdict(
        "c",
        not approvals and bool(who) and set(who) <= PEOPLE,
        f"approve calls {len(approvals)}, decisions by {sorted(set(who))}",
    )
    module_ok: dict[str, bool] = {}
    for task_id in module_tasks:
        checks = last_checks(proj, task_id)
        record = records.get(task_id)
        module_ok[task_id] = (
            record is not None
            and record.status == "accepted"
            and all(checks.get(cid) == "pass" for cid in ("lint", "naming", "style"))
        )
    d = _verdict(
        "d",
        bool(module_ok) and all(module_ok.values()),
        f"{sum(module_ok.values())}/{len(module_ok)} modules: "
        + ", ".join(f"{m} {last_checks(proj, fx.module_task(m))}" for m in modules),
    )
    tb_record = records.get(fx.TB_TASK)
    e = _verdict(
        "e",
        tb_record is not None
        and tb_record.status == "accepted"
        and last_checks(proj, fx.TB_TASK).get("tb_static") == "pass",
        f"{tb_record.status if tb_record else 'never queued'}, {last_checks(proj, fx.TB_TASK)}",
    )
    review_text = (proj / fx.REVIEW_FILE).read_text() if (proj / fx.REVIEW_FILE).is_file() else ""
    parsed, review_errors = parse_review(review_text) if review_text else (None, ["missing"])
    review_record = records.get(fx.REVIEW_TASK)
    f = _verdict(
        "f",
        parsed is not None and review_record is not None and review_record.status == "accepted",
        f"verdict {parsed.verdict if parsed else None}, "
        f"{len(parsed.comments) if parsed else 0} comment(s) {review_errors[:1]}",
    )
    record = _read_json(proj / fx.PR_RECORD)
    g_problems = branch_problems(proj, record, block_files) + tree_problems(proj, out, block_files)
    g = _verdict(
        "g",
        bool(writes) and not g_problems,
        f"block files {block_files}; {g_problems[:3]}",
    )
    rtl_stream = streams["rtl"]
    rtl_lines = _distinct(proj, "rtl/", fx.RTL_FINGERPRINT)
    tb_lines = _distinct(proj, "dv/", fx.TB_FINGERPRINT)
    seen = leaks(rtl_stream, fx.TB_TASK, rtl_lines, "RTL")
    for task_id in module_tasks:
        seen += leaks(rtl_stream, task_id, tb_lines, "testbench")
    ran = bool(rtl_stream.agents_for(fx.TB_TASK)) and all(
        bool(rtl_stream.agents_for(t)) for t in module_tasks
    )
    h = _verdict(
        "h",
        ran and bool(module_tasks) and not seen,
        f"{len(rtl_lines)} RTL and {len(tb_lines)} testbench lines checked; {seen[:3]}",
    )
    outside = sorted(
        {u.name for s in streams.values() for u in s.main_calls() if u.name not in MAIN_ALLOWED}
    )
    i = _verdict("i", not outside, f"other main-session tools: {outside}")
    turns_ok = []
    for name, stream in streams.items():
        result = stream.result
        turns = result.get("num_turns")
        turns_ok.append(
            result.get("subtype") == "success"
            and isinstance(turns, int)
            and turns <= TURN_BOUNDS[name]
        )
    j = _verdict(
        "j",
        all(turns_ok),
        ", ".join(f"{n} {s.result.get('num_turns')}/{TURN_BOUNDS[n]}" for n, s in streams.items()),
    )
    if sandbox:
        sandbox_state = _read_json(out / "sandbox.json") or {}
        pr = sandbox_state.get("pr") or {}
        k = _verdict(
            "k",
            pr.get("state") == "open"
            and not pr.get("merged")
            and pr.get("files") == block_files
            and isinstance(record, dict)
            and pr.get("head_sha") == record.get("head_sha"),
            f"PR {pr.get('url')} {pr.get('state')} files {pr.get('files')}; "
            f"cleanup {sandbox_state.get('cleanup')}",
        )
    else:
        exit_file = out / "final.exit"
        final_exit = exit_file.read_text().strip() if exit_file.is_file() else "?"
        approved = _read_json(proj / fx.PR_APPROVED)
        k = _verdict(
            "k",
            final_exit == "0"
            and isinstance(approved, dict)
            and isinstance(record, dict)
            and approved.get("head_sha") == record.get("head_sha"),
            f"chipgraph rtl pwm exit {final_exit}; approved head "
            f"{(approved or {}).get('head_sha')}",
        )
    passed = all((a, b, c, d, e, f, g, h, i, j, k))
    print("== verdict")
    print(f"  {'PASS' if passed else 'FAIL'}")
    return passed


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "--check-plan":
        problems = check_plan(_read_json(Path(argv[1])))
        for problem in problems:
            print(f"not sane: {problem}")
        print("sane" if not problems else "not approved")
        return 0 if not problems else 1
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    return 0 if report(Path(argv[0])) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
