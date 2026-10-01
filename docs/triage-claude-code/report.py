"""Summarise one M1-14 acceptance run (`run.sh`) and grade its answers.

    uv run python docs/triage-claude-code/report.py [--set holdout] OUT_DIR
    uv run python docs/triage-claude-code/report.py [--set holdout] --list   # id<TAB>check

Reads OUT_DIR/streams/<id>.jsonl (one `claude -p "/chipgraph:triage ..."` stream per
sample). The answer of a sample is the last `triage` tool result of the main session (the
report the command prints): its label, its backend (rule, small, large) and confidence.
Writes OUT_DIR/answers.jsonl and grades it with evals/triage/grade.py. Subagents may only
call `pending_decisions`; any other subagent tool call is listed and fails the run.
`--set holdout` uses the holdout set (`evals/triage/holdout.yml`, logs in
`evals/triage/logs-holdout/`) instead of the 22 samples of `faults.yml`.
Exit code: grade.py's (0 pass, 1 fail).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any

REPO = Path(__file__).resolve().parents[2]
LOGS = REPO / "evals" / "triage" / "logs"
LOGS_BY_SET = {"default": LOGS, "holdout": REPO / "evals" / "triage" / "logs-holdout"}
SUBAGENT_TOOLS = ("__pending_decisions",)


def _grade_module() -> ModuleType:
    path = REPO / "evals" / "triage" / "grade.py"
    spec = importlib.util.spec_from_file_location("triage_grade", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["triage_grade"] = module
    spec.loader.exec_module(module)
    return module


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
    for ev in events:
        msg = ev.get("message")
        if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
            continue
        for block in msg["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                results[str(block.get("tool_use_id"))] = block.get("content")
    calls = []
    for ev in events:
        msg = ev.get("message")
        if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
            continue
        for block in msg["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                calls.append(
                    {
                        "parent": ev.get("parent_tool_use_id"),
                        "name": str(block.get("name", "")),
                        "input": block.get("input") or {},
                        "result": _json_result(results.get(str(block.get("id")))),
                    }
                )
    return calls


def _answer(sid: str, calls: list[dict[str, Any]]) -> dict[str, Any]:
    triages = [
        c for c in calls if c["parent"] is None and c["name"].endswith("__triage") and c["result"]
    ]
    if not triages:
        return {"id": sid, "label": None, "backend": None, "status": "no triage result"}
    report = triages[-1]["result"] or {}
    return {
        "id": sid,
        "label": report.get("label"),
        "backend": report.get("backend"),
        "status": report.get("status"),
        "rule": report.get("rule"),
        "confidence": report.get("confidence"),
        "low_confidence": report.get("low_confidence"),
        "model": report.get("model"),
    }


def list_samples(sample_set: str = "default") -> int:
    grade = _grade_module()
    for sample in grade.load_samples(grade.SETS[sample_set]):
        meta = json.loads((LOGS_BY_SET[sample_set] / f"{sample.id}.json").read_text())
        print(f"{sample.id}\t{meta.get('check') or '-'}")
    return 0


def main(out: Path, sample_set: str = "default") -> int:
    grade = _grade_module()
    samples = grade.load_samples(grade.SETS[sample_set])
    answers = []
    cost = 0.0
    models: Counter[str] = Counter()
    deciders: Counter[str] = Counter()
    foreign: list[str] = []

    print("== per sample")
    for sample in samples:
        stream = out / "streams" / f"{sample.id}.jsonl"
        if not stream.is_file():
            continue
        events = _events(stream)
        calls = _tool_calls(events)
        result = next((e for e in reversed(events) if e.get("type") == "result"), {})
        agents = [
            (c["input"].get("subagent_type"), c["input"].get("model"))
            for c in calls
            if c["parent"] is None and c["name"] in ("Agent", "Task")
        ]
        deciders.update(str(model) for _, model in agents)
        main_tools = [c["name"].rsplit("__", 1)[-1] for c in calls if c["parent"] is None]
        for c in calls:
            if c["parent"] is not None and not c["name"].endswith(SUBAGENT_TOOLS):
                foreign.append(f"{sample.id}:{c['name']}")
        for kind, _ in agents:
            if kind != "chipgraph:decider":  # the main session may start no other agent
                foreign.append(f"{sample.id}:Agent({kind})")
        answer = _answer(sample.id, calls)
        answers.append(answer)
        cost += float(result.get("total_cost_usd") or 0.0)
        for model, usage in (result.get("modelUsage") or {}).items():
            if isinstance(usage, dict):
                models[model] += int(usage.get("outputTokens", 0) or 0)
        print(f"-- {sample.id}")
        print(f"   main tools: {main_tools}")
        print(f"   decider subagents (type, model): {agents}")
        print(
            f"   triage: status={answer['status']} label={answer['label']} "
            f"backend={answer['backend']} rule={answer.get('rule')} "
            f"confidence={answer.get('confidence')} low={answer.get('low_confidence')}"
        )
        print(
            f"   result: subtype={result.get('subtype')} turns={result.get('num_turns')} "
            f"cost_usd={result.get('total_cost_usd')}"
        )

    answers_path = out / "answers.jsonl"
    answers_path.write_text("".join(json.dumps(a) + "\n" for a in answers))
    timing = out / "timing.txt"
    print("== session totals")
    print(f"  samples run: {len(answers)} of {len(samples)}")
    print(f"  total_cost_usd: {cost:.4f}")
    print(f"  output tokens by model (modelUsage): {dict(models)}")
    print(f"  decider subagents by model: {dict(deciders)}")
    print(f"  other agents or subagent tools (must be none): {foreign}")
    print(f"  timing: {timing.read_text().strip() if timing.is_file() else '?'}")
    print("  no API key used: ANTHROPIC_API_KEY unset by run.sh")
    print(f"== grade ({answers_path})")
    report = grade.grade(samples, grade.load_answers(answers_path))
    print(grade.format_report(report))
    return 0 if report.passed and not foreign else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(usage=__doc__)
    parser.add_argument("--set", choices=sorted(LOGS_BY_SET), default="default")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("out", type=Path, nargs="?")
    args = parser.parse_args()
    if args.list:
        sys.exit(list_samples(args.set))
    if args.out is None:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(args.out, args.set))
