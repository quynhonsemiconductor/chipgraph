#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""chipgraph example: tinysoc
Writes a small, deterministic manifest for one block: its filelist and the sha256 of
each source file it lists. Used by the `tinysoc/lint_manifest` rule as a tiny `gen`
step whose output the built-in `lint` check then verifies (DESIGN.md 3.4, 7.1).

Usage: gen_manifest.py <block>
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: gen_manifest.py <block>", file=sys.stderr)
        return 2

    block = argv[1]
    root = Path(__file__).resolve().parent.parent
    filelist_path = root / "filelists" / f"{block}.f"
    if not filelist_path.is_file():
        print(f"no such filelist: {filelist_path}", file=sys.stderr)
        return 1

    sources = [line.strip() for line in filelist_path.read_text().splitlines() if line.strip()]
    entries = []
    for rel in sources:
        digest = hashlib.sha256((root / rel).read_bytes()).hexdigest()
        entries.append({"path": rel, "sha256": digest})

    manifest = {"block": block, "sources": entries}

    out_dir = root / "build"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"{block}.manifest.json"
    out_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
