"""Fake implementations of every plugin_api protocol, for tests."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from chipgraph.core.contracts import (
    AgentResult,
    Approval,
    CheckResult,
    CheckSpec,
    Issue,
)
from chipgraph.core.plugin_api.types import (
    AgentTask,
    LlmRequest,
    LlmResponse,
    RunResult,
    ToolContext,
)


class FakeRunner:
    """Fake `Runner`: records the last command and returns a canned result."""

    name = "fake-runner"

    async def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> RunResult:
        self.last_cmd = tuple(cmd)
        return RunResult(returncode=0, stdout="ok", stderr="", duration_s=0.01)


class FakeLogParser:
    """Fake `LogParser`: always returns a fixed issue."""

    name = "fake-parser"

    def parse(self, log: str) -> tuple[Issue, ...]:
        return (Issue(msg=log, severity="info"),)


class FakeToolAdapter:
    """Fake `ToolAdapter`: always passes."""

    name = "fake-tool"
    capability = "lint"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        return CheckResult(check_id=spec.id, status="pass", duration_s=0.0, idempotency_key="k")


class FakeCheck:
    """Fake `Check`: always passes."""

    name = "fake-check"
    id = "fake/check"

    async def run(self, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
        return CheckResult(check_id=spec.id, status="pass", duration_s=0.0, idempotency_key="k")


class FakeVcsAdapter:
    """Fake `VcsAdapter`: an in-memory stand-in for git."""

    name = "fake-vcs"

    def head(self, root: Path) -> str:
        return "deadbeef"

    def changed_files(self, root: Path, since: str | None = None) -> tuple[str, ...]:
        return ("a.sv",)

    def create_workspace(self, root: Path, name: str) -> Path:
        return root / name

    def remove_workspace(self, path: Path) -> None:
        return None


class FakeReviewAdapter:
    """Fake `ReviewAdapter`: keeps approvals in memory."""

    name = "fake-review"

    def __init__(self) -> None:
        self._approvals: list[Approval] = []

    def record(self, approval: Approval) -> None:
        self._approvals.append(approval)

    def approvals(self, gate_id: str) -> tuple[Approval, ...]:
        return tuple(a for a in self._approvals if a.gate_id == gate_id)


class FakeLlmProvider:
    """Fake `LlmProvider`: local, echoes back a fixed response."""

    name = "fake-llm"
    local = True

    async def complete(self, request: LlmRequest) -> LlmResponse:
        return LlmResponse(text="ok", input_tokens=1, output_tokens=1, model=request.model)


class FakeAgentRuntime:
    """Fake `AgentRuntime`: always reports done, no files written."""

    name = "fake-runtime"

    async def run_task(self, task: AgentTask) -> AgentResult:
        return AgentResult(status="done")


class FakeFormatAdapter:
    """Fake `FormatAdapter`: yields one fixed fact."""

    name = "fake-format"

    def load(self, path: Path) -> Iterable[Mapping[str, object]]:
        return ({"path": str(path)},)


class FakeExtractor:
    """Fake `Extractor`: yields one fixed fact."""

    name = "fake-extractor"

    def extract(self, path: Path) -> Iterable[Mapping[str, object]]:
        return ({"path": str(path)},)


class FakeGenerator:
    """Fake `Generator`: writes nothing, returns no paths."""

    name = "fake-generator"

    def generate(self, out_dir: Path, facts: Iterable[Mapping[str, object]]) -> tuple[Path, ...]:
        return ()


class NotAProtocol:
    """An object missing any protocol methods, used to check isinstance failures."""

    name = "not-a-protocol"
