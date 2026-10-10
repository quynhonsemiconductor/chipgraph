"""Loads the M2-02b acceptance report (`docs/agent-loop-claude-code/report.py`) under a
unique module name, for `test_agent_loop_acceptance_report.py`."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPORT = Path(__file__).resolve().parents[2] / "docs" / "agent-loop-claude-code" / "report.py"


def load_report() -> ModuleType:
    name = "agent_loop_acceptance_report"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPORT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
