"""A whitespace-only formatter for the M2-04 acceptance fixture and its tests: a formatter
runs after generation, but it never changes the style being measured.

    python fmt_trim.py FILE

Strips trailing whitespace from every line and leaves exactly one newline at the end.
Exits 2 when FILE does not exist.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not Path(argv[0]).is_file():
        print(f"fmt_trim: no such file: {argv}", file=sys.stderr)
        return 2
    path = Path(argv[0])
    text = path.read_text(encoding="utf-8")
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
