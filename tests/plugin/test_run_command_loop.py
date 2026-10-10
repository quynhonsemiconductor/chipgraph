"""M2-02b: `/chipgraph:run` writes the agent loop and its stop conditions into the
prompt (`plugin/commands/run.md`); the frontmatter checks stay in
`test_plugin_commands.py`."""

from __future__ import annotations

from pathlib import Path

import yaml

RUN = Path(__file__).resolve().parents[2] / "plugin" / "commands" / "run.md"


def _body() -> tuple[dict[str, object], str]:
    head, _, body = RUN.read_text(encoding="utf-8")[4:].partition("\n---\n")
    return yaml.safe_load(head), " ".join(body.split())


def test_the_disallowed_tools_stay_as_they_were() -> None:
    meta, _ = _body()
    assert meta["disallowed-tools"] == "Bash, NotebookEdit, Skill, WebFetch, WebSearch"
    assert meta["allowed-tools"] == (
        "mcp__plugin_chipgraph_chipgraph__next_task, mcp__plugin_chipgraph_chipgraph__submit, Agent"
    )


def test_the_stop_conditions_are_in_the_prompt() -> None:
    _, body = _body()
    for phrase in (
        "Stop conditions",
        "`done: true`",
        "No `tasks` and no `in_progress`",
        "`stopped: true`",
        "Only `blocked` entries are left",
        "`budget_exhausted` or `needs_human`",
        "at most 12 rounds",
        "12 rounds are done: stop",
        "print every `blocked` entry",
        "`handoff` path",
    ):
        assert phrase in body, phrase


def test_a_rejected_task_is_restarted_by_the_engine_only() -> None:
    _, body = _body()
    for phrase in (
        "`rejected`",
        "`next_model`",
        "pass the `task_id` only",
        "never the `redo` text",
        "Never start more subagents than the engine hands out",
        'Never "try once more" by hand',
        "is never started again",
    ):
        assert phrase in body, phrase
