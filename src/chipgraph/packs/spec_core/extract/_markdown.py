"""A small, line-exact Markdown scanner for MAS documents.

The scanner keeps every source line with its 1-based number and classifies it so the
extractor never has to rescan raw text. It skips YAML front matter, HTML comments
(multi-line; the QNSC template is full of them) and fenced code blocks, but keeps line
numbers exact so a diagnostic can always cite the real source line.

``<!-- gen:... -->`` and ``<!-- /gen -->`` markers are ordinary comments and are skipped,
but the table between them is content and is scanned normally.

Nothing here knows about ports, registers or requirements: it produces headings, tables,
list items and paragraphs, and the MAS extractor gives them meaning.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_HEADING_NUMBER = re.compile(r"^\s*\d+(?:\.\d+)*\.?\s+")
_LIST_ITEM = re.compile(r"^(\s*)(?:[-*+]|(\d+)[.)])\s+(.*)$")
_TABLE_DIVIDER = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(?:\|\s*:?-{1,}:?\s*)*\|?\s*$")
_FRONT_MATTER = "---"
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


@dataclass(frozen=True, slots=True)
class Heading:
    """A section heading: its raw title, the number-stripped title, and its level."""

    line: int
    level: int
    raw_title: str
    title: str
    """The title with any leading section number (``5.``, ``7.1``) stripped."""


@dataclass(frozen=True, slots=True)
class ListItem:
    """A top-level or nested list item, with its continuation lines already joined."""

    line: int
    indent: int
    ordered: bool
    number: int | None
    text: str
    """The item body (marker removed), continuation lines joined with single spaces."""


@dataclass(frozen=True, slots=True)
class Paragraph:
    """A run of non-list, non-table prose lines."""

    line: int
    text: str


@dataclass(frozen=True, slots=True)
class TableRow:
    """One row of a pipe table: its cells (outer pipes stripped) and its source line."""

    line: int
    cells: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Table:
    """A GitHub-style pipe table: a header row, its column names, and body rows."""

    header_line: int
    headers: tuple[str, ...]
    header_cells_raw: tuple[str, ...]
    rows: tuple[TableRow, ...]


@dataclass(slots=True)
class Section:
    """A heading and every block that belongs to it, until the next same-or-higher heading."""

    heading: Heading | None
    tables: list[Table] = field(default_factory=list)
    list_items: list[ListItem] = field(default_factory=list)
    paragraphs: list[Paragraph] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Document:
    """A scanned MAS document: its sections and every declared block, in source order."""

    sections: tuple[Section, ...]
    all_tables: tuple[Table, ...]
    all_list_items: tuple[ListItem, ...]


def strip_heading_number(title: str) -> str:
    """Remove a leading section number (``5.``, ``7.1``, ``11.``) from a heading title."""
    return _HEADING_NUMBER.sub("", title).strip()


def split_row(line: str) -> tuple[str, ...]:
    """Split a pipe-table row into trimmed cells, honouring escaped ``\\|``.

    Outer pipes are stripped. ``\\|`` inside a cell is kept as a literal ``|``.
    """
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for ch in body:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
            current.append(ch)
        elif ch == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    cells.append("".join(current).strip())
    return tuple(cells)


class _LineKind:
    """Internal per-line classification produced by the pre-pass."""

    __slots__ = ("kind", "line", "text")

    def __init__(self, line: int, kind: str, text: str) -> None:
        self.line = line
        self.kind = kind  # "content" or "skip"
        self.text = text


def _prepass(text: str) -> list[_LineKind]:
    """Classify every physical line as content or skipped, keeping numbers exact."""
    lines = text.splitlines()
    result: list[_LineKind] = []
    in_front_matter = False
    front_matter_possible = True
    in_comment = False
    fence: str | None = None

    for index, raw in enumerate(lines):
        number = index + 1
        stripped = raw.strip()

        # YAML front matter: only a leading '---' on line 1 opens it.
        if front_matter_possible and number == 1 and stripped == _FRONT_MATTER:
            in_front_matter = True
            front_matter_possible = False
            result.append(_LineKind(number, "skip", raw))
            continue
        front_matter_possible = False
        if in_front_matter:
            result.append(_LineKind(number, "skip", raw))
            if stripped == _FRONT_MATTER:
                in_front_matter = False
            continue

        # Fenced code blocks.
        if fence is not None:
            result.append(_LineKind(number, "skip", raw))
            if stripped.startswith(fence):
                fence = None
            continue

        # HTML comments (may open and close on the same line, or span many lines).
        if in_comment:
            result.append(_LineKind(number, "skip", raw))
            if "-->" in raw:
                in_comment = False
            continue

        working = raw
        if "<!--" in working:
            # A comment may sit inline; skip the whole physical line for simplicity, and
            # track whether it stays open past this line.
            if "-->" in working[working.index("<!--") :]:
                # Opens and closes on this line: treat the line as skipped content.
                result.append(_LineKind(number, "skip", raw))
                continue
            in_comment = True
            result.append(_LineKind(number, "skip", raw))
            continue

        fence_match = _FENCE.match(raw)
        if fence_match:
            fence = fence_match.group(1)[0] * 3
            result.append(_LineKind(number, "skip", raw))
            continue

        result.append(_LineKind(number, "content", raw))

    return result


def _looks_like_table_header(lines: Sequence[_LineKind], i: int) -> bool:
    """True when line ``i`` is a header row followed by a divider row."""
    if "|" not in lines[i].text:
        return False
    if i + 1 >= len(lines):
        return False
    nxt = lines[i + 1]
    return nxt.kind == "content" and bool(_TABLE_DIVIDER.match(nxt.text))


def scan(text: str) -> Document:
    """Scan MAS Markdown into a :class:`Document`."""
    lines = _prepass(text)
    sections: list[Section] = []
    current = Section(heading=None)
    sections.append(current)
    all_tables: list[Table] = []
    all_items: list[ListItem] = []

    i = 0
    n = len(lines)
    while i < n:
        entry = lines[i]
        if entry.kind == "skip":
            i += 1
            continue
        raw = entry.text
        stripped = raw.strip()

        if not stripped:
            i += 1
            continue

        heading_match = _HEADING.match(raw)
        if heading_match:
            level = len(heading_match.group(1))
            raw_title = heading_match.group(2).strip()
            heading = Heading(
                line=entry.line,
                level=level,
                raw_title=raw_title,
                title=strip_heading_number(raw_title),
            )
            current = Section(heading=heading)
            sections.append(current)
            i += 1
            continue

        if _looks_like_table_header(lines, i):
            table, consumed = _consume_table(lines, i)
            current.tables.append(table)
            all_tables.append(table)
            i += consumed
            continue

        item_match = _LIST_ITEM.match(raw)
        if item_match:
            item, consumed = _consume_list_item(lines, i)
            current.list_items.append(item)
            all_items.append(item)
            i += consumed
            continue

        paragraph, consumed = _consume_paragraph(lines, i)
        current.paragraphs.append(paragraph)
        i += consumed

    return Document(
        sections=tuple(sections),
        all_tables=tuple(all_tables),
        all_list_items=tuple(all_items),
    )


def _consume_table(lines: Sequence[_LineKind], start: int) -> tuple[Table, int]:
    """Consume a pipe table beginning at ``start`` (header + divider + body rows)."""
    header = lines[start]
    header_cells = split_row(header.text)
    headers = tuple(c.strip() for c in header_cells)
    rows: list[TableRow] = []
    i = start + 2  # skip header and divider
    n = len(lines)
    while i < n:
        entry = lines[i]
        if entry.kind == "skip":
            # A comment or fence inside a table ends it (tables are contiguous).
            break
        if "|" not in entry.text or not entry.text.strip():
            break
        rows.append(TableRow(line=entry.line, cells=split_row(entry.text)))
        i += 1
    table = Table(
        header_line=header.line,
        headers=headers,
        header_cells_raw=header_cells,
        rows=tuple(rows),
    )
    return table, i - start


def _consume_list_item(lines: Sequence[_LineKind], start: int) -> tuple[ListItem, int]:
    """Consume a list item and its continuation lines beginning at ``start``."""
    match = _LIST_ITEM.match(lines[start].text)
    assert match is not None
    indent = len(match.group(1))
    number = int(match.group(2)) if match.group(2) else None
    ordered = number is not None
    parts = [match.group(3).strip()]
    i = start + 1
    n = len(lines)
    while i < n:
        entry = lines[i]
        if entry.kind == "skip":
            break
        raw = entry.text
        if not raw.strip():
            break
        if _LIST_ITEM.match(raw):
            break
        if _HEADING.match(raw):
            break
        if "|" in raw and _looks_like_table_header(lines, i):
            break
        # A continuation line is indented under the item.
        leading = len(raw) - len(raw.lstrip())
        if leading <= indent and not raw.startswith(" "):
            break
        parts.append(raw.strip())
        i += 1
    text = " ".join(p for p in parts if p)
    item = ListItem(
        line=lines[start].line,
        indent=indent,
        ordered=ordered,
        number=number,
        text=text,
    )
    return item, i - start


def _consume_paragraph(lines: Sequence[_LineKind], start: int) -> tuple[Paragraph, int]:
    """Consume a run of prose lines beginning at ``start``."""
    parts = [lines[start].text.strip()]
    i = start + 1
    n = len(lines)
    while i < n:
        entry = lines[i]
        if entry.kind == "skip":
            break
        raw = entry.text
        if not raw.strip():
            break
        if _HEADING.match(raw) or _LIST_ITEM.match(raw):
            break
        if "|" in raw and _looks_like_table_header(lines, i):
            break
        parts.append(raw.strip())
        i += 1
    return Paragraph(line=lines[start].line, text=" ".join(parts)), i - start


def iter_sections_by_title(doc: Document, title: str) -> Iterator[Section]:
    """Yield every section whose number-stripped title equals ``title`` (case-insensitive)."""
    wanted = title.strip().casefold()
    for section in doc.sections:
        if section.heading is not None and section.heading.title.casefold() == wanted:
            yield section
