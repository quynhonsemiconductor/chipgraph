"""Shared by the tests of outputs moved aside for authors without a read tool
(`chipgraph.adapters.runtime.claude_code.previous`): a small tinysoc copy with one agent
rule of a chosen role, a check that passes once the output holds `GOOD`, and short calls
of the runtime tools. Not a test module itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from chipgraph.adapters.runtime.claude_code import service
from chipgraph.app.build import make_scheduler
from chipgraph.app.context import AppContext
from chipgraph.core.runtime import AgentTaskRecord, TaskQueue
from chipgraph.core.state.layout import StateLayout

REPO = Path(__file__).resolve().parents[3]
EXAMPLE = REPO / "examples" / "tinysoc"
RULE = "prev/task"
TASK = f"{RULE}[]"
SPEC = "doc/tasks/prev.md"
GOOD = "GOOD"
"""The check passes once every output contains this word."""

OUTPUTS = {
    "rtl-author": "rtl/tiny_prev.sv",
    "tb-author": "dv/test_prev.py",
    "author": "doc/prev.md",
}
"""A natural output per role."""

_CHECK = (
    "import pathlib, sys; outs = sys.argv[1:]; "
    "bad = [o for o in outs if 'GOOD' not in "
    "(pathlib.Path(o).read_text() if pathlib.Path(o).is_file() else '')]; "
    "[print(o + ':1: the file must say the word GOOD') for o in bad]; "
    "sys.exit(1 if bad else 0)"
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def make_project(
    dest: Path,
    *,
    role: str = "rtl-author",
    outputs: tuple[str, ...] | None = None,
    tries: int = 3,
    existing: dict[str, tuple[bytes, int]] | None = None,
    inputs: tuple[str, ...] = (),
) -> Path:
    """tinysoc at `dest` with one agent rule `prev/task` of `role` (a new git repo).

    `existing` maps an output path to (bytes, mode): files committed before the task.
    `inputs` are extra input files (each is created with one line of text).
    """
    outs = outputs or (OUTPUTS[role],)
    shutil.copytree(EXAMPLE, dest)
    pack = dest / ".chipgraph" / "packs" / "prev"
    (pack / "rules").mkdir(parents=True)
    (pack / "pack.yml").write_text("name: prev\nversion: 0.1.0\nprovides:\n  rules: [rules]\n")
    rule = {
        "rule": "task",
        "kind": "agent",
        "role": role,
        "description": "Write the outputs; each must say GOOD.",
        "inputs": [{"path": SPEC}, *({"path": p} for p in inputs)],
        "outputs": list(outs),
        "checks": ["prev_check"],
        "budget": {"tries": tries},
    }
    (pack / "rules" / "task.yml").write_text(yaml.safe_dump(rule, sort_keys=False))
    (dest / SPEC).parent.mkdir(parents=True, exist_ok=True)
    (dest / SPEC).write_text("# prev\n\nWrite each output; it must say GOOD.\n")
    for rel in inputs:
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_text("an extra input\n")
    profile_path = dest / ".chipgraph.yml"
    profile = yaml.safe_load(profile_path.read_text())
    profile["packs"] = ["prev"]
    profile["adapters"]["prev_check"] = {
        "use": "cmd",
        "cmd": ["python3", "-c", _CHECK, *outs],
        "regex": r"^(?P<file>[^\s:]+):(?P<line>\d+): (?P<msg>.+)$",
    }
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))
    for rel, (data, mode) in (existing or {}).items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.chmod(path, mode)
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "config", "user.email", "prev@example.invalid")
    _git(dest, "config", "user.name", "prev")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "tinysoc with one agent task")
    return dest


def ctx(root: Path) -> AppContext:
    return AppContext.load(root)


def next_task(root: Path, target: str = RULE) -> dict[str, Any]:
    return asyncio.run(service.next_task(ctx(root), target))


def get_context(root: Path, task_id: str = TASK) -> dict[str, Any]:
    return asyncio.run(service.get_context(ctx(root), task_id))


def submit(root: Path, task_id: str = TASK, **report: Any) -> dict[str, Any]:
    return asyncio.run(service.submit(ctx(root), task_id, service.SubmitReport(**report)))


def rewind(root: Path, task_id: str = TASK) -> None:
    make_scheduler(ctx(root), "*").rewind(task_id)


def record(root: Path, task_id: str = TASK) -> AgentTaskRecord:
    return TaskQueue(StateLayout(root)).require(task_id)


def create(root: Path, rel: str, text: str) -> None:
    """Write a file the way Claude Code lets an agent without Read: only a new one."""
    path = root / rel
    if path.exists():
        raise PermissionError("File has not been read yet. Read it first before writing to it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def tree_hashes(root: Path, skip: tuple[str, ...] = ()) -> dict[str, tuple[str, int]]:
    """Hash and mode of every file in the project, outside `.git` and the state backend."""
    found: dict[str, tuple[str, int]] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith((".git/", ".chipgraph/state/")) or rel in skip or not path.is_file():
            continue
        found[rel] = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode & 0o7777)
    return found


def previous_dir(root: Path, task_id: str = TASK) -> Path:
    return (
        root
        / ".chipgraph"
        / "state"
        / "runtime"
        / "previous"
        / hashlib.sha256(task_id.encode()).hexdigest()
    )


__all__ = [
    "GOOD",
    "OUTPUTS",
    "REPO",
    "RULE",
    "SPEC",
    "TASK",
    "create",
    "ctx",
    "get_context",
    "make_project",
    "next_task",
    "previous_dir",
    "record",
    "rewind",
    "submit",
    "tree_hashes",
]
