"""Tests for the managed-block markers of the ``agents-md`` generator (task M1-15)."""

from __future__ import annotations

import pytest

from chipgraph.packs.spec_core.gen.agents_md import (
    BEGIN_MARKER,
    END_MARKER,
    AgentsMdError,
    split_managed,
    update_agents_md,
)

_BLOCK = "<!-- generated note -->\n\n## Rules\n\n- one\n"
_NEW_BLOCK = "<!-- generated note -->\n\n## Rules\n\n- one\n- two\n"


def _managed(block: str) -> str:
    return f"{BEGIN_MARKER}\n{block}{END_MARKER}\n"


def test_no_file_gives_the_block_alone() -> None:
    assert update_agents_md(None, _BLOCK) == _managed(_BLOCK)
    assert update_agents_md("", _BLOCK) == _managed(_BLOCK)


def test_block_without_final_newline_is_terminated() -> None:
    assert update_agents_md(None, "x") == f"{BEGIN_MARKER}\nx\n{END_MARKER}\n"


@pytest.mark.parametrize(
    ("team", "sep"),
    [
        ("# Team rules\n\nUse tabs.\n", "\n"),
        ("# Team rules\n\nUse tabs.", "\n\n"),
        ("# Team rules\n\nUse tabs.\n\n", ""),
    ],
)
def test_no_markers_appends_after_one_blank_line(team: str, sep: str) -> None:
    result = update_agents_md(team, _BLOCK)
    assert result == team + sep + _managed(_BLOCK)
    assert result.startswith(team)
    # Appending once is enough: the next run finds the markers and replaces in place.
    assert update_agents_md(result, _BLOCK) == result


def test_team_text_before_and_after_is_kept_byte_for_byte() -> None:
    before = "# Our AGENTS.md\r\n\r\nWe review every PR.  \r\n\t- trailing tab line\t\r\n\n"
    after = "\n## After the block\r\n\r\nKeep this, even `<!-- chipgraph:begin -->` inline.\n\n\n"
    existing = f"{before}{BEGIN_MARKER}\r\nold generated text\r\n{END_MARKER}{after}"
    result = update_agents_md(existing, _NEW_BLOCK)
    assert result == f"{before}{BEGIN_MARKER}\n{_NEW_BLOCK}{END_MARKER}{after}"
    assert result.startswith(before + BEGIN_MARKER)
    assert result.endswith(END_MARKER + after)
    assert update_agents_md(result, _NEW_BLOCK) == result


def test_only_the_managed_text_changes() -> None:
    existing = f"top\n{BEGIN_MARKER}\n{_BLOCK}{END_MARKER}\nbottom\n"
    result = update_agents_md(existing, _NEW_BLOCK)
    assert result == f"top\n{BEGIN_MARKER}\n{_NEW_BLOCK}{END_MARKER}\nbottom\n"


def test_indented_markers_are_recognised_and_kept() -> None:
    existing = f"a\n  {BEGIN_MARKER}  \nold\n\t{END_MARKER}\nb"
    result = update_agents_md(existing, _BLOCK)
    assert result == f"a\n  {BEGIN_MARKER}  \n{_BLOCK}\t{END_MARKER}\nb"


def test_end_marker_at_end_of_file_without_newline() -> None:
    existing = f"x\n{BEGIN_MARKER}\nold\n{END_MARKER}"
    assert update_agents_md(existing, _BLOCK) == f"x\n{BEGIN_MARKER}\n{_BLOCK}{END_MARKER}"


def test_inline_marker_text_is_not_a_marker() -> None:
    team = f"Text mentioning `{BEGIN_MARKER}` and `{END_MARKER}` inline.\n"
    assert split_managed(team) is None
    assert update_agents_md(team, _BLOCK) == team + "\n" + _managed(_BLOCK)


@pytest.mark.parametrize(
    ("existing", "message"),
    [
        (f"a\n{BEGIN_MARKER}\nb\n", r"line 2 has no matching"),
        (f"a\n{END_MARKER}\nb\n", r"line 2 has no .* before it"),
        (f"{BEGIN_MARKER}\n{BEGIN_MARKER}\n{END_MARKER}\n", r"appears 2 times \(lines 1, 2\)"),
        (f"{BEGIN_MARKER}\n{END_MARKER}\n{END_MARKER}\n", r"appears 2 times \(lines 2, 3\)"),
        (
            f"{BEGIN_MARKER}\nx\n{END_MARKER}\n{BEGIN_MARKER}\ny\n{END_MARKER}\n",
            r"appears 2 times",
        ),
        (f"a\n{END_MARKER}\nb\n{BEGIN_MARKER}\n", r"line 2\) comes before .* \(line 4\)"),
    ],
    ids=[
        "begin-without-end",
        "end-without-begin",
        "two-begins",
        "two-ends",
        "two-blocks",
        "end-before-begin",
    ],
)
def test_malformed_markers_are_an_error(existing: str, message: str) -> None:
    with pytest.raises(AgentsMdError, match=message):
        update_agents_md(existing, _BLOCK)


def test_block_containing_markers_is_rejected() -> None:
    with pytest.raises(AgentsMdError, match="must not contain"):
        update_agents_md(None, f"x\n{END_MARKER}\n")
