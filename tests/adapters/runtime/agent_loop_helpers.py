"""Shared by the M2-02b tests: the acceptance fixture (`docs/agent-loop-claude-code/
fixture.py`, loaded under a unique module name) and short calls of the runtime tools."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from chipgraph.adapters.runtime.claude_code import service
from chipgraph.app.context import AppContext
from chipgraph.core.runtime import AgentTaskRecord, TaskQueue
from chipgraph.core.state.layout import StateLayout

REPO = Path(__file__).resolve().parents[3]
LOOP_DOCS = REPO / "docs" / "agent-loop-claude-code"


def load_module(name: str, path: Path) -> ModuleType:
    """Import `path` as module `name` (once)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fx = load_module("agent_loop_fixture", LOOP_DOCS / "fixture.py")

TIERS = {"medium": "haiku", "large": "sonnet"}


def ctx(root: Path) -> AppContext:
    return AppContext.load(root)


def next_task(root: Path, target: str | None = None) -> dict[str, Any]:
    return asyncio.run(service.next_task(ctx(root), target or fx.TARGET))


def get_context(root: Path, task_id: str) -> dict[str, Any]:
    return asyncio.run(service.get_context(ctx(root), task_id))


def submit(root: Path, task_id: str, **report: Any) -> dict[str, Any]:
    return asyncio.run(service.submit(ctx(root), task_id, service.SubmitReport(**report)))


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def record(root: Path, task_id: str) -> AgentTaskRecord:
    return TaskQueue(StateLayout(root)).require(task_id)


def task_ids(answer: dict[str, Any]) -> list[str]:
    return [t["task_id"] for t in answer["tasks"]]
