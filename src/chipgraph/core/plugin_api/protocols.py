"""Plugin protocols: the structural interfaces adapters, checks and packs implement.

Every protocol here is `@runtime_checkable`, so `isinstance(obj, Protocol)` works for
registry validation (see `registry.py`). Each protocol carries a `name: str` attribute
identifying the concrete implementation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from chipgraph.core.contracts import AgentResult, Approval, CheckResult, CheckSpec, Issue
from chipgraph.core.plugin_api.types import (
    AgentTask,
    LlmRequest,
    LlmResponse,
    RunResult,
    ToolContext,
)


@runtime_checkable
class Runner(Protocol):
    """Runs subprocess commands. Core never calls `subprocess` directly; it goes through this."""

    name: str

    async def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> RunResult:
        """Run `cmd` in `cwd` and return its result."""
        ...


@runtime_checkable
class LogParser(Protocol):
    """Parses a tool's raw log text into structured issues."""

    name: str

    def parse(self, log: str) -> tuple[Issue, ...]:
        """Extract issues from `log`."""
        ...


@runtime_checkable
class ToolAdapter(Protocol):
    """Wraps an external EDA tool (or other command-line tool) behind a common interface."""

    name: str
    capability: str

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        """Run the check described by `spec` under `ctx` and return its result."""
        ...


@runtime_checkable
class Check(Protocol):
    """A built-in, deterministic check that needs no external tool."""

    name: str
    id: str

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        """Run this check for `spec` under `ctx` and return its result."""
        ...


@runtime_checkable
class VcsAdapter(Protocol):
    """Version control operations the engine needs: head, diffs, and disposable workspaces."""

    name: str

    def head(self, root: Path) -> str:
        """Return the current head revision of the repo at `root`."""
        ...

    def changed_files(self, root: Path, since: str | None = None) -> tuple[str, ...]:
        """Return paths changed since `since` (or since the last commit, if None)."""
        ...

    def create_workspace(self, root: Path, name: str) -> Path:
        """Create a disposable workspace (e.g. a worktree) named `name` and return its path."""
        ...

    def remove_workspace(self, path: Path) -> None:
        """Remove a workspace previously created by `create_workspace`."""
        ...


@runtime_checkable
class ReviewAdapter(Protocol):
    """Records and retrieves human approvals against gates."""

    name: str

    def record(self, approval: Approval) -> None:
        """Persist `approval`."""
        ...

    def approvals(self, gate_id: str) -> tuple[Approval, ...]:
        """Return every approval recorded against `gate_id`."""
        ...


@runtime_checkable
class LlmProvider(Protocol):
    """Completes LLM requests. `local` gates whether it may see data labeled 'nda'."""

    name: str
    local: bool
    """True when this provider is self-hosted; only then may it receive 'nda'-labeled data."""

    async def complete(self, request: LlmRequest) -> LlmResponse:
        """Complete `request` and return the response."""
        ...


@runtime_checkable
class AgentRuntime(Protocol):
    """Runs an `AgentTask` to completion (or to a stopping point) via some agent harness."""

    name: str

    async def run_task(self, task: AgentTask) -> AgentResult:
        """Run `task` and return its result."""
        ...


@runtime_checkable
class FormatAdapter(Protocol):
    """Loads a domain file format into raw facts.

    Returns raw facts (plain mappings), not the typed Design Model: the typed model
    arrives in M1-01. Callers that need the Design Model translate these facts themselves.
    """

    name: str

    def load(self, path: Path) -> Iterable[Mapping[str, object]]:
        """Load `path` and yield its facts."""
        ...


@runtime_checkable
class Extractor(Protocol):
    """Extracts facts from a source file (e.g. RTL) by parsing it.

    Returns raw facts (plain mappings), not the typed Design Model: the typed model
    arrives in M1-01. Callers that need the Design Model translate these facts themselves.
    """

    name: str

    def extract(self, path: Path) -> Iterable[Mapping[str, object]]:
        """Extract facts from `path`."""
        ...


@runtime_checkable
class Generator(Protocol):
    """Generates output files from facts, e.g. rendering a template."""

    name: str

    def generate(self, out_dir: Path, facts: Iterable[Mapping[str, object]]) -> tuple[Path, ...]:
        """Write generated files under `out_dir` from `facts` and return their paths."""
        ...
