"""Summarise one M1-16 acceptance run (`run.sh`): every v0 plugin command, once each.

    uv run python docs/plugin-claude-code/report.py OUT_DIR
    uv run python docs/plugin-claude-code/report.py --cases        # id<TAB>project<TAB>prompt
    uv run python docs/plugin-claude-code/report.py --allow        # tools to pre-approve
    uv run python docs/plugin-claude-code/report.py --triage-log   # the sample log to copy

Reads OUT_DIR/streams/<case>.jsonl (one `claude -p "<command>"` stream per case). For each
case it lists the tools the main session and its subagents called, and gives PASS when:

- the case's expected chipgraph MCP tool was called by the main session;
- no tool outside the chipgraph MCP server was used, except `Agent` starting a
  `chipgraph:*` subagent (and `ToolSearch`, which only loads tool schemas;
  `AskUserQuestion` in /chipgraph:init-chipgraph) - the run passes no --disallowedTools,
  so this is the commands' own `disallowed-tools` at work;
- the final output has the expected content (the REQ id and a spec citation, close IDs,
  a verified citation, the triage label, the written file ...).

Prints the total cost. Exit code 0 when every case that ran passed, 1 otherwise.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugin"
MCP = "mcp__plugin_chipgraph_chipgraph__"
TRIAGE_LOG = "log-05"  # a simulation mismatch: no rule decides, the decider must answer
ASK_ID = "q01"
TRACE_REQ = "REQ-TIM-001"
TRACE_CITE = "doc/specs/TINY_TIMER_MAS.md:68"  # where REQ-TIM-001 is declared
HARNESS_TOOLS = {"ToolSearch"}  # loads deferred tool schemas; reads and writes nothing


@dataclass
class Case:
    id: str
    project: str  # directory under OUT_DIR
    prompt: str
    expect_tool: str  # an MCP tool the main session must call
    extra_allowed: set[str] = field(default_factory=set)


def _ask_question() -> dict[str, Any]:
    data = yaml.safe_load((REPO / "evals" / "ask" / "tinysoc.yml").read_text())
    return next(q for q in data["questions"] if q["id"] == ASK_ID)


def _triage_label() -> str:
    data = yaml.safe_load((REPO / "evals" / "triage" / "faults.yml").read_text())
    samples = data["samples"] if isinstance(data, dict) else data
    return str(next(s for s in samples if s["id"] == TRIAGE_LOG)["label"])


def cases() -> list[Case]:
    return [
        Case("status", "tinysoc", "/chipgraph:status", "status"),
        Case("trace", "tinysoc", f"/chipgraph:trace {TRACE_REQ}", "model_trace"),
        Case("trace_unknown", "tinysoc", "/chipgraph:trace REQ-NOPE-999", "model_find"),
        Case("ask", "tinysoc", f"/chipgraph:ask {_ask_question()['question']}", "ask_check"),
        Case("triage", "tinysoc", "/chipgraph:triage logs/triage.log", "triage"),
        Case(
            "init",
            "fresh",
            "/chipgraph:init-chipgraph --yes",
            "init",
            extra_allowed={"AskUserQuestion"},
        ),
    ]


# --- stream parsing (same shape as docs/triage-claude-code/report.py) -------------------


def _events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip().startswith("{"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _json_result(content: Any) -> dict[str, Any] | None:
    texts: list[str] = []
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        texts = [str(c.get("text", "")) for c in content if isinstance(c, dict)]
    for text in texts:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _tool_calls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every tool_use, in order, with its parent (None = main session) and its result."""
    results: dict[str, Any] = {}
    errors: set[str] = set()
    for ev in events:
        msg = ev.get("message")
        if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
            continue
        for block in msg["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                results[str(block.get("tool_use_id"))] = block.get("content")
                if block.get("is_error"):
                    errors.add(str(block.get("tool_use_id")))
    calls = []
    for ev in events:
        msg = ev.get("message")
        if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
            continue
        for block in msg["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                use_id = str(block.get("id"))
                calls.append(
                    {
                        "parent": ev.get("parent_tool_use_id"),
                        "name": str(block.get("name", "")),
                        "input": block.get("input") or {},
                        "result": _json_result(results.get(use_id)),
                        "error": use_id in errors,
                    }
                )
    return calls


def _short(name: str) -> str:
    return name.removeprefix(MCP)


def _main(calls: list[dict[str, Any]], tool: str) -> list[dict[str, Any]]:
    return [c for c in calls if c["parent"] is None and c["name"] == MCP + tool]


# --- per-case content checks: (ok, note) ------------------------------------------------


def _check_status(out: Path, calls: list[dict[str, Any]], text: str) -> tuple[bool, str]:
    build = (out / "build.txt").read_text() if (out / "build.txt").is_file() else ""
    run = re.search(r"^run (\S+)", build, re.M)
    want = [w for w in ("tinysoc", run.group(1) if run else "", "rtl[block=") if w]
    missing = [w for w in want if w not in text]
    return not missing, f"output names {want}; missing {missing}"


def _check_trace(out: Path, calls: list[dict[str, Any]], text: str) -> tuple[bool, str]:
    missing = [w for w in (TRACE_REQ, TRACE_CITE) if w not in text]
    return not missing, f"REQ id and spec citation {TRACE_CITE}; missing {missing}"


def _check_trace_unknown(out: Path, calls: list[dict[str, Any]], text: str) -> tuple[bool, str]:
    said = "REQ-NOPE-999" in text and re.search(r"\bnot\b", text) is not None
    close = re.findall(r"REQ-(?:TIM|GPIO)-\d{3}", text)
    traced = _main(calls, "model_trace")
    ok = said and bool(close) and not any(c["result"] for c in traced)
    return ok, f"says unknown: {said}; close IDs listed: {sorted(set(close))}"


def _check_ask(out: Path, calls: list[dict[str, Any]], text: str) -> tuple[bool, str]:
    checks = [c for c in _main(calls, "ask_check") if c["result"]]
    verdict = checks[-1]["result"] if checks else {}
    answer = verdict.get("answer") or {}
    expected = [str(e["cite"]) for e in _ask_question()["expected"]]
    cited = [c for c in answer.get("citations", []) if any(c.startswith(e) for e in expected)]
    shown = [c for c in cited if c in text]
    ok = bool(verdict.get("ok")) and not answer.get("unknown") and bool(shown)
    return ok, f"ask_check ok={verdict.get('ok')}; expected citations printed: {shown}"


def _check_triage(out: Path, calls: list[dict[str, Any]], text: str) -> tuple[bool, str]:
    triages = [c for c in _main(calls, "triage") if c["result"]]
    report = triages[-1]["result"] if triages else {}
    label = report.get("label")
    deciders = [
        c["input"].get("model")
        for c in calls
        if c["parent"] is None and c["name"] in ("Agent", "Task")
    ]
    ok = report.get("status") == "decided" and bool(label) and str(label) in text
    return ok, (
        f"triage status={report.get('status')} label={label} backend={report.get('backend')} "
        f"(sample label: {_triage_label()}); decider models: {deciders}"
    )


def _check_init(out: Path, calls: list[dict[str, Any]], text: str) -> tuple[bool, str]:
    inits = _main(calls, "init")
    confirms = [bool(c["input"].get("confirm")) for c in inits]
    profile = out / "fresh" / ".chipgraph.yml"
    status = out / "git-status-fresh.txt"
    changed = status.read_text().splitlines() if status.is_file() else []
    other = [
        line for line in changed if line[3:] != ".chipgraph.yml" and ".chipgraph/state/" not in line
    ]
    ok = (
        confirms[:1] == [False]
        and True in confirms
        and profile.is_file()
        and ".chipgraph.yml" in text
        and not other
    )
    return ok, (
        f"init confirm sequence {confirms}; .chipgraph.yml written: {profile.is_file()}; "
        f"other changes: {other}"
    )


CHECKS: dict[str, Callable[[Path, list[dict[str, Any]], str], tuple[bool, str]]] = {
    "status": _check_status,
    "trace": _check_trace,
    "trace_unknown": _check_trace_unknown,
    "ask": _check_ask,
    "triage": _check_triage,
    "init": _check_init,
}


def _foreign(case: Case, calls: list[dict[str, Any]]) -> list[str]:
    """Tools used outside the chipgraph MCP server (plus `Agent(chipgraph:*)`)."""
    bad = []
    for c in calls:
        name = c["name"]
        where = "main" if c["parent"] is None else "subagent"
        if name.startswith(MCP) or name in HARNESS_TOOLS:
            continue
        if name in ("Agent", "Task") and where == "main":
            kind = str(c["input"].get("subagent_type", ""))
            if not kind.startswith("chipgraph:"):
                bad.append(f"main:Agent({kind})")
            continue
        if name in case.extra_allowed and where == "main":
            continue
        bad.append(f"{where}:{name}")
    return bad


def main(out: Path) -> int:
    total = 0.0
    ran = 0
    failed: list[str] = []
    print("== per command")
    for case in cases():
        stream = out / "streams" / f"{case.id}.jsonl"
        if not stream.is_file():
            continue
        ran += 1
        events = _events(stream)
        calls = _tool_calls(events)
        result = next((e for e in reversed(events) if e.get("type") == "result"), {})
        text = str(result.get("result") or "")
        cost = float(result.get("total_cost_usd") or 0.0)
        total += cost
        main_tools = [_short(c["name"]) for c in calls if c["parent"] is None]
        sub_tools = [_short(c["name"]) for c in calls if c["parent"] is not None]
        agents = [
            (c["input"].get("subagent_type"), c["input"].get("model"))
            for c in calls
            if c["parent"] is None and c["name"] in ("Agent", "Task")
        ]
        called = bool(_main(calls, case.expect_tool))
        foreign = _foreign(case, calls)
        content_ok, note = CHECKS[case.id](out, calls, text)
        ok = called and not foreign and content_ok and result.get("subtype") == "success"
        if not ok:
            failed.append(case.id)
        print(f"-- {case.id}: {'PASS' if ok else 'FAIL'}   {case.prompt}")
        print(f"   main tools: {main_tools}")
        print(f"   subagents (type, model): {agents}")
        print(f"   subagent tools: {sub_tools}")
        print(f"   expected tool {case.expect_tool!r} called: {called}")
        print(f"   tools outside chipgraph (must be none): {foreign}")
        print(f"   content: {'ok' if content_ok else 'NOT ok'} - {note}")
        print(
            f"   result: subtype={result.get('subtype')} turns={result.get('num_turns')} "
            f"cost_usd={cost:.4f}"
        )
        if not ok:
            print("   output: " + text.strip().replace("\n", "\n           ")[:1500])
    status = out / "git-status-tinysoc.txt"
    timing = out / "timing.txt"
    print("== totals")
    print(f"  commands run: {ran} of {len(cases())}; failed: {failed}")
    print(f"  total_cost_usd: {total:.4f}")
    print(f"  timing: {timing.read_text().strip() if timing.is_file() else '?'}")
    if status.is_file():
        print(f"  tinysoc copy changed (git status): {status.read_text().split() or 'nothing'}")
    print("  no API key used: ANTHROPIC_API_KEY unset by run.sh; no --disallowedTools passed")
    return 0 if ran and not failed else 1


def _allowed_tools() -> str:
    """Every chipgraph MCP tool a plugin command or agent names (pre-approval only)."""
    names: set[str] = set()
    for path in sorted(PLUGIN.glob("commands/*.md")) + sorted(PLUGIN.glob("agents/*.md")):
        names.update(re.findall(rf"{MCP}\w+", path.read_text(encoding="utf-8")))
    return " ".join(sorted(names))


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["--cases"]:
        for c in cases():
            print(f"{c.id}\t{c.project}\t{c.prompt}")
        sys.exit(0)
    if args == ["--allow"]:
        print(_allowed_tools())
        sys.exit(0)
    if args == ["--triage-log"]:
        print(TRIAGE_LOG)
        sys.exit(0)
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(Path(args[0])))
