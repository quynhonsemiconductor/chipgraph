"""M2-11b: `/chipgraph:plan` and `/chipgraph:rtl` (`plugin/commands/{plan,rtl}.md`).

The frontmatter rules every command follows are in `test_plugin_commands.py`; these check
what the two commands promise: the exact tools, the human gate (only a person's
`AskUserQuestion` answer approves; headless prints the command and stops), the revise
path, and the stop conditions of `/chipgraph:run`. The MCP side is in
`tests/mcp/test_plan_rtl_session.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

COMMANDS = Path(__file__).resolve().parents[2] / "plugin" / "commands"
MCP = "mcp__plugin_chipgraph_chipgraph__"
DENIED = {"Bash", "NotebookEdit", "Skill", "WebFetch", "WebSearch"}


def _read(name: str) -> tuple[dict[str, object], str]:
    head, _, body = (COMMANDS / f"{name}.md").read_text(encoding="utf-8")[4:].partition("\n---\n")
    return yaml.safe_load(head), " ".join(body.split())


def _tools(value: object) -> set[str]:
    return {t for t in re.split(r"[,\s]+", str(value)) if t}


def test_plan_tools_are_exactly_the_loop_the_gate_and_the_question() -> None:
    meta, _ = _read("plan")
    assert _tools(meta["allowed-tools"]) == {
        *(f"{MCP}{t}" for t in ("next_task", "submit", "plan_show", "approve", "status")),
        "Agent",
        "AskUserQuestion",
    }
    assert _tools(meta["disallowed-tools"]) == DENIED
    assert meta["argument-hint"] == "<block>"


def test_rtl_has_no_approve_and_no_question() -> None:
    meta, body = _read("rtl")
    assert _tools(meta["allowed-tools"]) == {
        *(f"{MCP}{t}" for t in ("next_task", "submit", "plan_show", "status")),
        "Agent",
    }
    assert _tools(meta["disallowed-tools"]) == DENIED
    assert "approve" not in " ".join(re.findall(rf"{MCP}\w+", body))
    assert "AskUserQuestion" not in body


def test_plan_only_a_persons_answer_approves() -> None:
    _, body = _read("plan")
    for phrase in (
        "no argument approves anything",
        "Ask the person with `AskUserQuestion`",
        '"Approve", "Revise (the planner redoes it with your note)", "Stop"',
        "Act on the answer, and only on the answer",
        "Do not pass `by`: the server records the local user",
        "**Never approve without a person's answer.**",
        "You never approve on your own",
        "never approve a plan that fails `plan_check`",
    ):
        assert phrase in body, phrase
    assert "--approve" not in body


def test_plan_headless_prints_the_command_and_stops() -> None:
    _, body = _read("plan")
    for phrase in (
        "Headless (`claude -p`) or when `AskUserQuestion` is unavailable",
        'print "Not approved: no person answered."',
        "`chipgraph plan show <block>` and `gate.approve`, then end",
    ):
        assert phrase in body, phrase


def test_plan_revise_rejects_with_the_note_and_runs_the_planner_again() -> None:
    _, body = _read("plan")
    for phrase in (
        "`reject` = true, `note` = the note",
        "go back to part A, step 1",
        "the note in its context",
        "At most 3 revisions",
    ):
        assert phrase in body, phrase


def test_plan_prints_the_summary_the_approver_needs() -> None:
    _, body = _read("plan")
    for field in (
        "`modules`",
        "`reqs`",
        "`depends_on`",
        "`writes`",
        "`coverage`",
        "`warnings`",
        "`open_questions`",
    ):
        assert field in body, field


def test_both_keep_the_stop_conditions_of_run() -> None:
    for name, rounds in (("plan", 12), ("rtl", 20)):
        _, body = _read(name)
        for phrase in (
            "Stop conditions",
            "`done: true`",
            "No `tasks` and no `in_progress` (`stopped: true`)",
            "`budget_exhausted` or `needs_human`",
            f"at most {rounds} rounds",
            f"{rounds} rounds are done",
            "never the `redo` text",
            'Never "try once more" by hand',
            "Do not write or edit any file",
            "Do not call `get_context` yourself",
        ):
            assert phrase in body, (name, phrase)


def test_rtl_needs_an_approved_plan_and_never_approves_or_merges() -> None:
    _, body = _read("rtl")
    for phrase in (
        "If `gate.status` is not `approved`",
        "run /chipgraph:plan <block> first",
        "Never approve a gate, never merge",
        "`open_pr`",
        "never as a subagent",
        '`"reply": "review"`',
        '`{"status": "done", "review": <that JSON object>}`',
        "in parallel",
        "`review.verdict`",
        "`pr.url`, or else the local branch `pr.branch`",
        "gate pr:<block>",
        "`handoff` path",
    ):
        assert phrase in body, phrase
    for agent in ("chipgraph:rtl-author", "chipgraph:tb-author", "chipgraph:critic"):
        assert agent in body
