"""A safe FTS5 query from a free-text question (FTS5 syntax never reaches SQLite raw).

The question is tokenised into words (Unicode letters, digits, `_`, with inner `-` or
`.`, so `REQ-TIM-004`, `CTRL.EN` and `pin_out` stay one term). English stop words and
one-character words are dropped, a plural gets its singular as an extra term, every term
is quoted as an FTS5 string, and the terms are ORed. A quoted string is never an FTS5
operator (`NEAR`, `AND`, `*`, `^`, `col:`), and the tokeniser splits it into a phrase
exactly as it split the indexed text. A question with no usable term gives `None`: no
search at all.
"""

from __future__ import annotations

import re

MAX_TERMS = 32
"""At most this many terms go into one query (the first ones in the question)."""

_TERM = re.compile(r"\w+(?:[-.]\w+)*")

_STOP_WORDS_TEXT = """
    a about above after again all also am an and any are as at be been before being below
    between both but by can could did do does doing done down during each either else
    for from further get gets give given had has have having he her here hers how however
    i if in into is it its itself just let me more most much must my no nor not now of
    off on once only or other our out over own per please same she should show so some
    such tell than that the their them then there these they this those through to too
    under until up upon us very via was we were what whats when where whether which while
    who whom whose why will with within without would yes you your
"""
STOP_WORDS = frozenset(_STOP_WORDS_TEXT.split())
"""English function words and question words: never a search term on their own.

`out`, `in` and `no` are stop words too: as ports (`pin_out`) they are kept inside the
longer term, and a typed lookup still finds an entity named exactly that.
"""


def question_terms(question: str) -> list[str]:
    """The search terms of `question`, lowercased, in order, without duplicates."""
    terms: list[str] = []
    seen: set[str] = set()
    for match in _TERM.finditer(question):
        word = match.group(0).strip("-.").lower()
        if len(word) < 2 or word in STOP_WORDS:
            continue
        for term in (word, _singular(word)):
            if term and term not in seen and term not in STOP_WORDS:
                seen.add(term)
                terms.append(term)
    return terms[:MAX_TERMS]


def fts_query(question: str) -> str | None:
    """A safe FTS5 MATCH expression for `question`: quoted terms ORed, or `None`."""
    terms = question_terms(question)
    if not terms:
        return None
    return " OR ".join(_quote(term) for term in terms)


def _quote(term: str) -> str:
    # Terms only hold word characters, `-` and `.`; doubling `"` keeps this safe anyway.
    return '"' + term.replace('"', '""') + '"'


def _singular(word: str) -> str | None:
    """A naive singular for an English plural (`registers` -> `register`), else None."""
    if not word.isalpha() or len(word) < 5:
        return None
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("sses") or word.endswith("xes"):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return None


__all__ = ["MAX_TERMS", "STOP_WORDS", "fts_query", "question_terms"]
