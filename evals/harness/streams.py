"""Reading one headless `claude -p --output-format stream-json` run: tool calls, answer, cost.

Ported from the M1-13/M1-14 acceptance scripts (`docs/ask-claude-code/report.py`,
`docs/triage-claude-code/report.py`), which stay as they are.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

ASK_SUBAGENT_TOOLS = ("__ask_context", "__ask_check")
"""The only tools the `chipgraph:asker` subagent may call."""

TRIAGE_SUBAGENT_TOOLS = ("__pending_decisions",)
"""The only tool a `chipgraph:decider` subagent may call."""

TRIAGE_AGENT = "chipgraph:decider"
"""The only subagent the triage main session may start."""


def events(path: Path) -> list[dict[str, Any]]:
    """The JSON events of a stream file (non-JSON lines skipped)."""
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().startswith("{"):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                out.append(event)
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


def _blocks(stream: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    out = []
    for event in stream:
        message = event.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        out.extend((event, block) for block in message["content"] if isinstance(block, dict))
    return out


def tool_calls(stream: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every tool_use, in order, with its parent (None = main session) and its JSON result."""
    blocks = _blocks(stream)
    results = {
        str(block.get("tool_use_id")): block.get("content")
        for _, block in blocks
        if block.get("type") == "tool_result"
    }
    return [
        {
            "parent": event.get("parent_tool_use_id"),
            "name": str(block.get("name", "")),
            "input": block.get("input") or {},
            "result": _json_result(results.get(str(block.get("id")))),
        }
        for event, block in blocks
        if block.get("type") == "tool_use"
    ]


def result_event(stream: list[dict[str, Any]]) -> dict[str, Any]:
    """The last `result` event (cost, turns, subtype), or {}."""
    return next((e for e in reversed(stream) if e.get("type") == "result"), {})


def cost_usd(stream: list[dict[str, Any]]) -> float:
    try:
        return float(result_event(stream).get("total_cost_usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def output_tokens(stream: list[dict[str, Any]]) -> dict[str, int]:
    """Output tokens by model, from the result's `modelUsage`."""
    tokens: Counter[str] = Counter()
    for model, usage in (result_event(stream).get("modelUsage") or {}).items():
        if isinstance(usage, dict):
            tokens[str(model)] += int(usage.get("outputTokens", 0) or 0)
    return dict(tokens)


def agents(calls: list[dict[str, Any]]) -> list[tuple[str | None, str | None]]:
    """The subagents the main session started: (subagent_type, model)."""
    return [
        (c["input"].get("subagent_type"), c["input"].get("model"))
        for c in calls
        if c["parent"] is None and c["name"] in ("Agent", "Task")
    ]


def ask_answer(qid: str, calls: list[dict[str, Any]]) -> dict[str, Any]:
    """The answer of one `/chipgraph:ask` run, in `evals/ask/grade.py`'s format.

    It is the one the main session's `ask_check` accepted (the checked answer the command
    prints); without one, the asker's last accepted check is used but marked unchecked,
    so it never passes.
    """
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


def triage_answer(sid: str, calls: list[dict[str, Any]]) -> dict[str, Any]:
    """The answer of one `/chipgraph:triage` run: the main session's last `triage` result."""
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


def foreign_calls(kind: str, sid: str, calls: list[dict[str, Any]]) -> list[str]:
    """Tool calls a run must not make: subagent tools outside the allowed set, and (triage)
    any subagent other than the decider. A run with one fails the suite."""
    allowed = ASK_SUBAGENT_TOOLS if kind == "ask" else TRIAGE_SUBAGENT_TOOLS
    out = [
        f"{sid}:{c['name']}"
        for c in calls
        if c["parent"] is not None and not c["name"].endswith(allowed)
    ]
    if kind == "triage":
        out += [f"{sid}:Agent({t})" for t, _ in agents(calls) if t != TRIAGE_AGENT]
    return out


__all__ = [
    "agents",
    "ask_answer",
    "cost_usd",
    "events",
    "foreign_calls",
    "output_tokens",
    "result_event",
    "tool_calls",
    "triage_answer",
]
