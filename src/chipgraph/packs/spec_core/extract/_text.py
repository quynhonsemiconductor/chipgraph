"""Text helpers shared by the MAS extractor: normalization, hashing, cell parsing.

Kept separate from the scanner and the extractor so the rules for turning a cell or an
item into a value are all in one place and easy to test.
"""

from __future__ import annotations

import hashlib
import re

_WHITESPACE = re.compile(r"\s+")
_LEADING_NUMBER = re.compile(r"^\s*\d+(?:\.\d+)*[.)]?\s+")
_INT_LITERAL = re.compile(r"^[+-]?(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|[0-9][0-9_]*)$")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def collapse_whitespace(text: str) -> str:
    """Collapse every run of whitespace to a single space and strip the ends."""
    return _WHITESPACE.sub(" ", text).strip()


def strip_leading_number(text: str) -> str:
    """Remove a leading ordered-list number (``1.``, ``2)``) from item text."""
    return _LEADING_NUMBER.sub("", text, count=1)


def normalize_item_text(text: str) -> str:
    """Normalize a list item for hashing: drop its number, collapse whitespace.

    Inline markup (backticks, ``**``) is intentionally kept: two items that differ only
    in emphasis are different requirements.
    """
    return collapse_whitespace(strip_leading_number(text))


def content_hash8(text: str) -> str:
    """The first 8 hex chars of the SHA-256 of ``text``, for a stable inferred key."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def strip_markup_token(token: str) -> str:
    """Strip surrounding backticks and ``**`` bold markers from a single token."""
    value = token.strip()
    changed = True
    while changed:
        changed = False
        if value.startswith("**") and value.endswith("**") and len(value) >= 4:
            value = value[2:-2]
            changed = True
        if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
            value = value[1:-1]
            changed = True
    return value.strip()


def strip_cell_markup(cell: str) -> str:
    """Remove backticks and bold markers throughout a table cell, then collapse spaces."""
    without = cell.replace("`", "").replace("**", "")
    return collapse_whitespace(without)


def first_token(text: str) -> str:
    """Return the first whitespace-separated token of ``text`` (empty if none)."""
    stripped = text.strip()
    if not stripped:
        return ""
    return stripped.split()[0]


def parse_int_maybe(text: str) -> int | str | None:
    """Parse ``text`` as an int (hex/bin/decimal, ``_`` separators allowed).

    Returns the int on success, the collapsed string if it is non-empty but not an int,
    and ``None`` if empty.
    """
    value = strip_cell_markup(text)
    if not value:
        return None
    compact = value.replace(" ", "")
    if not _INT_LITERAL.match(compact):
        return value
    cleaned = compact.replace("_", "")
    lowered = cleaned.lstrip("+-").lower()
    try:
        if lowered.startswith("0x"):
            return int(cleaned, 16)
        if lowered.startswith("0b"):
            return int(cleaned, 2)
        return int(cleaned, 10)
    except ValueError:  # pragma: no cover - guarded by the regex
        return value


def is_identifier(text: str) -> bool:
    """True when ``text`` (after markup removal) is a single simple identifier."""
    return bool(_IDENTIFIER.match(strip_cell_markup(text)))


def parse_bits(text: str) -> tuple[int, int] | None:
    """Parse a Bits cell (``15:8`` or ``0``) into ``(msb, lsb)``.

    Returns ``None`` when the cell is not a plain bit or bit range (e.g. ``bits 0-15``).
    """
    value = strip_cell_markup(text)
    if not value:
        return None
    if ":" in value:
        hi, _, lo = value.partition(":")
        if hi.strip().isdigit() and lo.strip().isdigit():
            return int(hi), int(lo)
        return None
    if value.isdigit():
        n = int(value)
        return n, n
    return None
