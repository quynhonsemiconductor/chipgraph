"""Shared fixtures for scheduler tests: a small 'gen' executor plus fakes for checks and
gates. Not a test module itself (no `test_*` functions); imported by test_scheduler.py,
test_resume_kill.py and the resume-kill worker script.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path

from chipgraph.core.contracts.check import CheckResult
from chipgraph.core.contracts.rule import RuleInstance, RuleSpec
from chipgraph.core.engine.scheduler import ExecOutcome, GateStatus


class UppercaseExecutor:
    """A 'gen' executor: each output is the uppercase of its inputs' concatenated content.

    An instance with no inputs writes its own instance id as content, so distinct
    no-input instances still produce distinct, deterministic output. Tracks call counts
    per instance id and (optionally) the max number of concurrently active calls, and can
    inject an `asyncio.sleep` per call for timing-sensitive tests.
    """

    def __init__(self, root: Path, *, delay_s: float = 0.0) -> None:
        self.root = root
        self.delay_s = delay_s
        self.calls: dict[str, int] = defaultdict(int)
        self.active = 0
        self.max_active = 0

    async def execute(self, rule: RuleSpec, instance: RuleInstance) -> ExecOutcome:
        self.calls[instance.instance_id] += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            parts = [
                (self.root / ref.path).read_text(encoding="utf-8")
                for ref in instance.inputs
                if ref.path is not None
            ]
            text = "".join(parts) or instance.instance_id
            content = text.upper()
            for ref in instance.outputs:
                assert ref.path is not None
                out_path = self.root / ref.path
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(content, encoding="utf-8")
            return ExecOutcome(ok=True)
        finally:
            self.active -= 1


class FailingExecutor:
    """An executor that always fails, for testing failure propagation."""

    def __init__(self, *, failure_label: str | None = None, message: str = "boom") -> None:
        self.failure_label = failure_label
        self.message = message
        self.calls = 0

    async def execute(self, rule: RuleSpec, instance: RuleInstance) -> ExecOutcome:
        self.calls += 1
        return ExecOutcome(ok=False, failure_label=self.failure_label, message=self.message)  # type: ignore[arg-type]


class NoOutputExecutor:
    """An executor that reports success but never writes its declared outputs."""

    async def execute(self, rule: RuleSpec, instance: RuleInstance) -> ExecOutcome:
        return ExecOutcome(ok=True)


class FakeGateChecker:
    """A `GateChecker` backed by a fixed mapping of gate id -> decision."""

    def __init__(self, decisions: dict[str, GateStatus]) -> None:
        self.decisions = decisions

    def status(self, gate_id: str, instance: RuleInstance) -> GateStatus:
        return self.decisions.get(gate_id, "waiting")


class FakeCheckRunner:
    """A `CheckRunner` backed by a fixed mapping of check id -> `CheckResult`."""

    def __init__(self, results: dict[str, CheckResult]) -> None:
        self.results = results
        self.calls: list[str] = []

    async def run(self, check_id: str, instance: RuleInstance) -> CheckResult:
        self.calls.append(check_id)
        return self.results[check_id]


def make_check_result(check_id: str, *, ok: bool) -> CheckResult:
    """A minimal `CheckResult` for tests: pass if `ok`, fail otherwise."""
    return CheckResult(
        check_id=check_id,
        status="pass" if ok else "fail",
        duration_s=0.0,
        idempotency_key=f"key-{check_id}",
    )


__all__ = [
    "FailingExecutor",
    "FakeCheckRunner",
    "FakeGateChecker",
    "NoOutputExecutor",
    "UppercaseExecutor",
    "make_check_result",
]
