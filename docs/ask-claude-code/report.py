"""Summarise one M1-13 acceptance run (`run.sh`) and grade its answers.

    uv run python docs/ask-claude-code/report.py OUT_DIR
    uv run python docs/ask-claude-code/report.py --list      # id<TAB>question, for run.sh

Reads OUT_DIR/streams/<id>.jsonl (one `claude -p "/chipgraph:ask ..."` stream per
question). The answer of a question is the one the main session's `ask_check` call
accepted (the checked answer the command prints); if the main session made no accepted
check, the last accepted `ask_check` of the asker subagent is used but marked unchecked, so
it never passes. Writes OUT_DIR/answers.jsonl and grades it with evals/ask/grade.py.
Exit code: grade.py's (0 pass, 1 fail).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any

REPO = Path(__file__).resolve().parents[2]
ASK_TOOLS = ("__ask_context", "__ask_check")


def _grade_module() -> ModuleType:
    path = REPO / "evals" / "ask" / "grade.py"
    spec = importlib.util.spec_from_file_location("ask_grade", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ask_grade"] = module
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


def _answer(qid: str, calls: list[dict[str, Any]]) -> dict[str, Any]:
    checks = [c for c in calls if c["name"].endswith("__ask_check")]
    main_ok = [c for c in checks if c["parent"] is None and (c["result"] or {}).get("ok")]
    sub_ok = [c for c in checks if c["parent"] is not None and (c["result"] or {}).get("ok")]
    chosen, checked = (main_ok[-1], True) if main_ok else (sub_ok[-1] if sub_ok else None, False)
    if chosen is None:
        return {"id": qid, "answer": "", "citations": [], "unknown": False, "checked": False}
    verified = (chosen["result"] or {}).get("answer") or chosen["input"]
    return {
        "id": qid,
        "answer": str(verified.get("answer", "")),
        "citations": list(verified.get("citations") or []),
        "unknown": bool(verified.get("unknown", False)),
        "checked": checked,
    }


def list_questions() -> int:
    grade = _grade_module()
    for question in grade.load_questions(grade.DEFAULT_QUESTIONS):
        print(f"{question.id}\t{question.question}")
    return 0


def main(out: Path) -> int:
    grade = _grade_module()
    questions = grade.load_questions(grade.DEFAULT_QUESTIONS)
    answers = []
    cost = 0.0
    models: Counter[str] = Counter()
    asker_tools: Counter[str] = Counter()
    foreign: list[str] = []

    print("== per question")
    for question in questions:
        stream = out / "streams" / f"{question.id}.jsonl"
        if not stream.is_file():
            continue
        events = _events(stream)
        calls = _tool_calls(events)
        result = next((e for e in reversed(events) if e.get("type") == "result"), {})
        agents = [
            (c["input"].get("subagent_type"), c["input"].get("model"))
            for c in calls
            if c["parent"] is None and c["name"] == "Agent"
        ]
        sub = Counter(c["name"].rsplit("__", 1)[-1] for c in calls if c["parent"] is not None)
        asker_tools.update(sub)
        main_tools = [c["name"].rsplit("__", 1)[-1] for c in calls if c["parent"] is None]
        for c in calls:
            if c["parent"] is not None and not c["name"].endswith(ASK_TOOLS):
                foreign.append(f"{question.id}:{c['name']}")
        answer = _answer(question.id, calls)
        answers.append(answer)
        cost += float(result.get("total_cost_usd") or 0.0)
        for model, usage in (result.get("modelUsage") or {}).items():
            if isinstance(usage, dict):
                models[model] += int(usage.get("outputTokens", 0) or 0)
        print(f"-- {question.id}: {question.question}")
        print(f"   main tools: {main_tools}   Agent calls: {agents}   asker tools: {dict(sub)}")
        print(
            f"   answer (checked={answer['checked']}, unknown={answer['unknown']}): "
            f"{answer['answer'][:200]}"
        )
        print(f"   citations: {answer['citations']}")
        print(
            f"   result: subtype={result.get('subtype')} turns={result.get('num_turns')} "
            f"cost_usd={result.get('total_cost_usd')}"
        )

    answers_path = out / "answers.jsonl"
    answers_path.write_text("".join(json.dumps(a) + "\n" for a in answers))
    timing = out / "timing.txt"
    print("== session totals")
    print(f"  questions run: {len(answers)} of {len(questions)}")
    print(f"  total_cost_usd: {cost:.4f}")
    print(f"  output tokens by model (modelUsage): {dict(models)}")
    print(f"  asker tool calls: {dict(asker_tools)}")
    print(f"  asker calls to other tools (must be none): {foreign}")
    print(f"  timing: {timing.read_text().strip() if timing.is_file() else '?'}")
    print("  no API key used: ANTHROPIC_API_KEY unset by run.sh")
    print(f"== grade ({answers_path})")
    report = grade.grade(questions, grade.load_answers(answers_path))
    print(grade.format_report(report))
    return 0 if report.passed and not foreign else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--list"]:
        sys.exit(list_questions())
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(Path(sys.argv[1])))
