"""The `rtl` and `tb` evals (task M2-13): the RTL Author and the testbench Author on leaf
modules of finished QSoC blocks, graded against the RTL people wrote.

- `problems.yml` / `problems`: the problems, their fixtures (`fixtures/`, `fixtures`),
  approved-plan fixtures (`plans/`) and reference tests (`reference/`);
- `project`: a fresh eval project per problem and phase (the RTL Author's, the
  testbench Author's), as a chipgraph project of its own;
- `stimulus`: the random, seeded stimulus, from the interface only;
- `diffsim`: the differential simulation of a module against the oracle (Verilator);
- `mutants`: the mutants of the oracle, by textual operators;
- `tbsim`: a cocotb test run against the oracle and its mutants (Verilator);
- `static`: lint, naming and style measures;
- `metrics`: pass@1, pass@k, repair turns, cost;
- `grade`: the deterministic grader of both suites.

`evals/` is not part of the installed package: the harness loads this directory as the
package `chipgraph_evals_rtl` (`load_package`).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

PACKAGE = "chipgraph_evals_rtl"
HERE = Path(__file__).resolve().parent


def load_package() -> ModuleType:
    """This directory as the package `chipgraph_evals_rtl` (once)."""
    loaded = sys.modules.get(PACKAGE)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(
        PACKAGE, HERE / "__init__.py", submodule_search_locations=[str(HERE)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module
