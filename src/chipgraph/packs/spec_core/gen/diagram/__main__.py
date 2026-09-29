"""``python -m chipgraph.packs.spec_core.gen.diagram --root <repo> --out <dir>``.

Reads the Design Model from the project's model store (``default_model_db_path(root)``)
and writes the four figures into ``--out``. Prints a clear message and exits 1 when the
model store does not exist yet (run ``chipgraph ingest`` first).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from chipgraph.core.model import default_model_db_path
from chipgraph.core.model.store import ModelStore
from chipgraph.packs.spec_core.gen.diagram import render


def main(argv: list[str] | None = None) -> int:
    """Render the model's figures; return a process exit code."""
    parser = argparse.ArgumentParser(prog="python -m chipgraph.packs.spec_core.gen.diagram")
    parser.add_argument("--root", type=Path, default=Path("."), help="project repo root")
    parser.add_argument("--out", type=Path, required=True, help="output directory for figures")
    args = parser.parse_args(argv)

    db_path = default_model_db_path(args.root)
    if not db_path.is_file():
        print(
            f"no Design Model at {db_path}; run 'chipgraph ingest' to build it first",
            file=sys.stderr,
        )
        return 1

    model = ModelStore(db_path).read()
    files = render(model)
    args.out.mkdir(parents=True, exist_ok=True)
    for name in sorted(files):
        (args.out / name).write_bytes(files[name])
        print(f"wrote {args.out / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
