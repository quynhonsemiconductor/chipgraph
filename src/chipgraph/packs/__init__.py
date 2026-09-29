"""Built-in packs shipped with chipgraph (DECISIONS D36).

Each pack is one subdirectory with a `pack.yml` manifest and, when it has code, an
`__init__.py`. Directory names are snake_case (`spec_core/`) so the code is importable;
the pack name in `pack.yml` keeps its dashes (`spec-core`). Pack code registers through
the `chipgraph.adapters.<kind>` entry points, like any adapter. `chipgraph.core` must not
import this package (enforced by import-linter).
"""
