"""Read-only lookups in the documents table of the model store (`ModelStore.add_document`).

`ModelStore` offers full-text `search` only; `/ask` also needs exact line lookups (the
context around a hit, and checking that a cited `file:line` is indexed). These read the
same `documents(path, line, text)` table, read-only, and treat a missing database or
table as "no documents".
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class DocumentLines:
    """Line lookups over the indexed documents of the store at `db_path`."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._counts: dict[str, int] | None = None

    def line_counts(self) -> dict[str, int]:
        """Indexed path -> its number of lines (the highest line number)."""
        if self._counts is None:
            rows = self._query("SELECT path, MAX(line) FROM documents GROUP BY path", ())
            self._counts = {str(path): int(count) for path, count in rows}
        return self._counts

    def is_indexed(self, path: str) -> bool:
        return path in self.line_counts()

    def lines(self, path: str, start: int, end: int) -> list[tuple[int, str]]:
        """The `(line, text)` rows of `path` from `start` to `end`, inclusive, in order."""
        rows = self._query(
            "SELECT line, text FROM documents WHERE path = ? AND line BETWEEN ? AND ? "
            "ORDER BY line",
            (path, start, end),
        )
        return [(int(line), str(text)) for line, text in rows]

    def _query(self, sql: str, params: tuple[object, ...]) -> list[tuple[Any, ...]]:
        if not self.db_path.is_file():
            return []
        try:
            conn = sqlite3.connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True)
        except sqlite3.Error:
            return []
        try:
            return list(conn.execute(sql, params))
        except sqlite3.Error:
            return []
        finally:
            conn.close()


__all__ = ["DocumentLines"]
