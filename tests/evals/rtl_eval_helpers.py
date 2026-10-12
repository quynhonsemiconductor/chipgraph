"""Shared by the M2-13 tests (`test_rtl_*.py`): the evals harness and `evals/rtl/` loaded
as packages, and the skip conditions of the simulator-dependent tests.

Not a test module itself.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
HARNESS = REPO / "evals" / "harness"


def harness() -> ModuleType:
    if "chipgraph_evals" in sys.modules:
        return sys.modules["chipgraph_evals"]
    spec = importlib.util.spec_from_file_location(
        "chipgraph_evals", HARNESS / "__init__.py", submodule_search_locations=[str(HARNESS)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["chipgraph_evals"] = module
    spec.loader.exec_module(module)
    return module


def rtl(name: str) -> ModuleType:
    """A module of evals/rtl/, as `chipgraph_evals_rtl.<name>`."""
    return harness().suites.rtl_module(name)


def _missing_verilator() -> str | None:
    missing = [t for t in ("verilator", "make") if shutil.which(t) is None]
    if all(shutil.which(c) is None for c in ("g++", "c++", "clang++")):
        missing.append("a C++ compiler")
    return f"{', '.join(missing)} not found on PATH" if missing else None


def _missing_sim() -> str | None:
    if importlib.util.find_spec("cocotb") is None or importlib.util.find_spec("edalize") is None:
        return "the `sim` extra is not installed (uv sync --extra sim)"
    return _missing_verilator()


NO_VERILATOR = _missing_verilator()
NO_SIM = _missing_sim()
needs_verilator = pytest.mark.skipif(NO_VERILATOR is not None, reason=str(NO_VERILATOR))
needs_sim = pytest.mark.skipif(NO_SIM is not None, reason=str(NO_SIM))


def qsoc_clone() -> Path | None:
    """A QSoC clone to check the fixtures against: $CHIPGRAPH_QSOC_REPO, else
    /tmp/qsoc-clone (None when neither is a git checkout)."""
    for raw in (os.environ.get("CHIPGRAPH_QSOC_REPO"), "/tmp/qsoc-clone"):
        if raw and (Path(raw) / ".git").exists():
            return Path(raw)
    return None
