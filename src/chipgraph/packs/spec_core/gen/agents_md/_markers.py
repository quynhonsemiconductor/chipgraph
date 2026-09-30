"""The managed block of an `AGENTS.md`: find the markers, replace only what is between.

DESIGN.md 4.7 / DECISIONS D31: chipgraph owns only the text between
``<!-- chipgraph:begin -->`` and ``<!-- chipgraph:end -->``; everything else in the file
belongs to the team and is kept byte for byte.

A marker is recognised only as a whole line (surrounding spaces, tabs and a trailing
``\\r`` allowed), so a team's own prose that mentions the marker inline is not mistaken
for one. Malformed markers are an error, never a guess.
"""

from __future__ import annotations

BEGIN_MARKER = "<!-- chipgraph:begin -->"
END_MARKER = "<!-- chipgraph:end -->"


class AgentsMdError(Exception):
    """The file's markers are malformed, or the managed block could not be rendered."""


def _marker_lines(text: str, marker: str) -> list[tuple[int, int, int]]:
    """Every whole-line occurrence of `marker`: (line number, start offset, end offset).

    The offsets span the marker line without its line ending.
    """
    found: list[tuple[int, int, int]] = []
    offset = 0
    for number, line in enumerate(text.splitlines(keepends=True), start=1):
        body = line.rstrip("\r\n")
        if body.strip(" \t") == marker:
            found.append((number, offset, offset + len(body)))
        offset += len(line)
    return found


def split_managed(existing: str) -> tuple[str, str, str] | None:
    """Split `existing` into (text up to and including the begin marker line's marker,
    the managed text between the markers, text from the end marker on).

    Returns `None` if the file has no markers at all. Raises `AgentsMdError` for a begin
    marker without an end, an end without a begin, more than one of either, or an end
    marker before the begin marker.
    """
    begins = _marker_lines(existing, BEGIN_MARKER)
    ends = _marker_lines(existing, END_MARKER)
    if not begins and not ends:
        return None
    if len(begins) > 1:
        lines = ", ".join(str(n) for n, _, _ in begins)
        raise AgentsMdError(
            f"{BEGIN_MARKER} appears {len(begins)} times (lines {lines}); keep exactly one "
            "managed block"
        )
    if len(ends) > 1:
        lines = ", ".join(str(n) for n, _, _ in ends)
        raise AgentsMdError(
            f"{END_MARKER} appears {len(ends)} times (lines {lines}); keep exactly one "
            "managed block"
        )
    if not ends:
        raise AgentsMdError(
            f"{BEGIN_MARKER} on line {begins[0][0]} has no matching {END_MARKER}; add the "
            "end marker or remove the begin marker"
        )
    if not begins:
        raise AgentsMdError(
            f"{END_MARKER} on line {ends[0][0]} has no {BEGIN_MARKER} before it; add the "
            "begin marker or remove the end marker"
        )
    begin_line, _, begin_end = begins[0]
    end_line, end_start, _ = ends[0]
    if end_line < begin_line:
        raise AgentsMdError(
            f"{END_MARKER} (line {end_line}) comes before {BEGIN_MARKER} (line "
            f"{begin_line}); the begin marker must come first"
        )
    # The begin marker's own line ending stays with the managed text's replacement.
    return existing[:begin_end], existing[begin_end:end_start], existing[end_start:]


def _check_block(block: str) -> None:
    if _marker_lines(block, BEGIN_MARKER) or _marker_lines(block, END_MARKER):
        raise AgentsMdError("the managed block must not contain the chipgraph markers itself")


def wrap_block(block: str) -> str:
    """`block` between the two markers, newline-terminated."""
    _check_block(block)
    body = block if block.endswith("\n") or not block else block + "\n"
    return f"{BEGIN_MARKER}\n{body}{END_MARKER}\n"


def update_agents_md(existing: str | None, block: str) -> str:
    """The file content after putting `block` in the managed part of `existing`.

    - `existing` is `None` (no file) or empty: the markers with `block` between them.
    - `existing` has no markers: the team's content unchanged, one blank line, then the
      marked block appended.
    - `existing` has one well-formed pair: only the text between the markers is replaced;
      every byte outside them (and the marker lines themselves) is kept.
    - Malformed markers raise `AgentsMdError`.

    `block` is the managed content without the markers (as `render_agents_md` returns).
    Applying the same `block` twice gives the same result as applying it once.
    """
    _check_block(block)
    if existing is None or existing == "":
        return wrap_block(block)
    parts = split_managed(existing)
    if parts is None:
        if existing.endswith("\n\n"):
            sep = ""
        elif existing.endswith("\n"):
            sep = "\n"
        else:
            sep = "\n\n"
        return existing + sep + wrap_block(block)
    head, _, tail = parts
    body = block if block.endswith("\n") or not block else block + "\n"
    return f"{head}\n{body}{tail}"


__all__ = [
    "BEGIN_MARKER",
    "END_MARKER",
    "AgentsMdError",
    "split_managed",
    "update_agents_md",
    "wrap_block",
]
