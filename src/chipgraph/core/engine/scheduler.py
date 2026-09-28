"""The scheduler: runs a `BuildGraph` with bounded concurrency, stops at gates, resumes
from a killed run, and rewinds a rule instance so it (and everything downstream) rebuilds
(DESIGN.md 3.3, 6.2).

The scheduler itself never talks to a tool, an LLM, or a human: it depends only on the
small protocols below (`Executor`, `CheckRunner`, `GateChecker`), so M0-09..M0-11 and M2
plug in real implementations without this module changing.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from chipgraph import __version__
from chipgraph.core.contracts.artifact import ArtifactRef
from chipgraph.core.contracts.check import CheckResult
from chipgraph.core.contracts.event import Event, RunManifest
from chipgraph.core.contracts.rule import RuleInstance, RuleSpec
from chipgraph.core.contracts.types import FailureLabel, RuleKind
from chipgraph.core.engine.graph import BuildGraph, ProductionRecord, Staleness, compute_staleness
from chipgraph.core.engine.records import RecordStore
from chipgraph.core.state import trace as tracing
from chipgraph.core.state.artifacts import ArtifactStore, hash_inputs
from chipgraph.core.state.journal import Journal, read, read_manifest, replay, write_manifest
from chipgraph.core.state.layout import StateLayout, new_run_id
from chipgraph.core.state.lock import BlockLock
from chipgraph.core.state.trace import NoopTracer, Tracer

_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")

InstanceStatus = Literal["fresh", "done", "failed", "waiting_gate"]
GateStatus = Literal["approved", "rejected", "waiting"]


class SchedulerError(Exception):
    """Raised when the scheduler cannot make progress on a selected graph."""


# --- Protocols the scheduler depends on ---------------------------------------------


class ExecOutcome(BaseModel):
    """What an `Executor` returns for a single rule instance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    message: str = ""
    failure_label: FailureLabel | None = None


@runtime_checkable
class Executor(Protocol):
    """Runs one rule instance and reports whether it succeeded."""

    async def execute(self, rule: RuleSpec, instance: RuleInstance) -> ExecOutcome: ...


@runtime_checkable
class CheckRunner(Protocol):
    """Runs a deterministic check against a rule instance's outputs."""

    async def run(self, check_id: str, instance: RuleInstance) -> CheckResult: ...


@runtime_checkable
class GateChecker(Protocol):
    """Reports whether a gate has been approved, rejected, or is still waiting."""

    def status(self, gate_id: str, instance: RuleInstance) -> GateStatus: ...


class AgentStub:
    """An `Executor` for `kind='agent'` rules, which arrive in M2 (see agent_rule.py)."""

    async def execute(self, rule: RuleSpec, instance: RuleInstance) -> ExecOutcome:
        return ExecOutcome(ok=False, failure_label="infra", message="agent rules arrive in M2")


class RunSummary(BaseModel):
    """The outcome of one `Scheduler.run`/`resume` call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    done: tuple[str, ...] = ()
    skipped_fresh: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    waiting_gate: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True only if nothing failed and nothing is waiting on a gate."""
        return not self.failed and not self.waiting_gate


def _ref_key(ref: ArtifactRef) -> str:
    """The same `"<repo>:<path-or-model_key>"` key `graph.py` uses for staleness lookups."""
    locator = ref.path if ref.path is not None else ref.model_key
    return f"{ref.repo}:{locator}"


def _format_gate(template: str, params: Mapping[str, str]) -> str:
    """Substitute `{param}` placeholders in a gate id template; leave unknown ones as-is."""

    def _replace(match: re.Match[str]) -> str:
        return params.get(match.group(1), match.group(0))

    return _PLACEHOLDER_RE.sub(_replace, template)


class Scheduler:
    """Runs a `BuildGraph`, journaling every step so a killed run can be resumed."""

    def __init__(
        self,
        graph: BuildGraph,
        *,
        layout: StateLayout,
        store: ArtifactStore,
        executors: Mapping[RuleKind, Executor],
        checks: CheckRunner | None = None,
        gates: GateChecker | None = None,
        concurrency: int = 4,
        owner: str = "chipgraph",
        tracer: Tracer | None = None,
    ) -> None:
        self.graph = graph
        self.layout = layout
        self.store = store
        self.executors = executors
        self.checks = checks
        self.gates = gates
        self.concurrency = concurrency
        self.owner = owner
        self.records = RecordStore(layout)
        self.tracer: Tracer = tracer if tracer is not None else NoopTracer()

    # --- entry points ----------------------------------------------------------------

    async def run(self, target: str = "*", *, run_id: str | None = None) -> RunSummary:
        """Run `target` from scratch, in a new run, unless `run_id` names one to continue."""
        resuming = run_id is not None
        rid = run_id if run_id is not None else new_run_id()
        journal = Journal(self.layout.journal(rid))

        if resuming:
            manifest = read_manifest(self.layout, rid)
            target = manifest.target
        else:
            manifest = RunManifest(
                run_id=rid,
                started_at=datetime.now(UTC),
                target=target,
                chipgraph_version=__version__,
                profile_hash="0" * 64,
            )
            write_manifest(self.layout, manifest)

        return await self._execute(rid, journal, target, emit_run_start=not resuming)

    async def resume(self, run_id: str) -> RunSummary:
        """Continue a run that was interrupted, from the same journal and manifest."""
        journal = Journal(self.layout.journal(run_id))  # repairs a truncated last line
        replay(read(journal.path).events)  # validate the journal is well-formed
        manifest = read_manifest(self.layout, run_id)
        return await self._execute(run_id, journal, manifest.target, emit_run_start=False)

    def rewind(self, instance_id: str) -> set[str]:
        """Delete the records of `instance_id` and everything downstream of it.

        Returns the set of instance ids whose records were deleted; the next run will
        find none of them have a record and rebuild them.
        """
        ids = {instance_id} | self.graph.downstream(instance_id)
        for iid in ids:
            self.records.delete(iid)
        return ids

    # --- run/resume shared machinery --------------------------------------------------

    async def _execute(
        self, rid: str, journal: Journal, target: str, *, emit_run_start: bool
    ) -> RunSummary:
        with self.tracer.span(
            tracing.SPAN_RUN, **{tracing.RUN_ID: rid, tracing.TARGET: target}
        ) as run_span:
            graph = self.graph
            selected = graph.select(target)

            if emit_run_start:
                self._emit(journal, rid, "run_start", payload={"target": target})

            blocks = sorted(
                {graph.instances[iid].params.get("block", "graph") for iid in selected}
            ) or ["graph"]
            locks = [BlockLock(self.layout, block=block, owner=self.owner) for block in blocks]
            for lock in locks:
                lock.acquire()
            try:
                summary = await self._schedule(rid, journal, graph, selected)
            finally:
                for lock in locks:
                    lock.release()

            self._emit(
                journal,
                rid,
                "run_stop",
                payload={
                    "done": len(summary.done),
                    "skipped_fresh": len(summary.skipped_fresh),
                    "failed": len(summary.failed),
                    "waiting_gate": len(summary.waiting_gate),
                    "blocked": len(summary.blocked),
                },
            )
            run_span.set("chipgraph.run.done", len(summary.done))
            run_span.set("chipgraph.run.skipped_fresh", len(summary.skipped_fresh))
            run_span.set("chipgraph.run.failed", len(summary.failed))
            run_span.set("chipgraph.run.waiting_gate", len(summary.waiting_gate))
            run_span.set("chipgraph.run.blocked", len(summary.blocked))
            return summary

    async def _schedule(
        self, rid: str, journal: Journal, graph: BuildGraph, selected: set[str]
    ) -> RunSummary:
        all_refs = [
            ref
            for iid in selected
            for ref in (*graph.instances[iid].inputs, *graph.instances[iid].outputs)
            if ref.path is not None
        ]
        current = self.store.current_hashes(all_refs)
        staleness: dict[str, Staleness] = compute_staleness(graph, self.records.all(), current)

        finished_ok: set[str] = set()
        done: set[str] = set()
        skipped_fresh: set[str] = set()
        failed: set[str] = set()
        waiting_gate: set[str] = set()
        blocked: set[str] = set()

        remaining = set(selected)
        running: dict[str, asyncio.Task[tuple[InstanceStatus, FailureLabel | None]]] = {}

        def deps_ready(iid: str) -> bool:
            return all(dep in finished_ok for dep in graph.deps(iid))

        def deps_bad(iid: str) -> bool:
            return any(
                dep in blocked or dep in failed or dep in waiting_gate for dep in graph.deps(iid)
            )

        while remaining or running:
            changed = True
            while changed:
                changed = False
                for iid in sorted(remaining):
                    if iid in running:
                        continue
                    if deps_bad(iid):
                        blocked.add(iid)
                        remaining.discard(iid)
                        changed = True

            for iid in sorted(remaining):
                if len(running) >= self.concurrency:
                    break
                if iid in running:
                    continue
                if deps_ready(iid):
                    running[iid] = asyncio.create_task(
                        self._process_instance(rid, journal, graph, iid, staleness)
                    )

            if not running:
                if remaining:
                    raise SchedulerError(
                        f"scheduler stuck: {sorted(remaining)!r} are never ready or blocked"
                    )
                break

            completed, _ = await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED)
            for task in completed:
                iid = next(key for key, value in running.items() if value is task)
                status, _label = task.result()
                del running[iid]
                remaining.discard(iid)
                if status == "fresh":
                    finished_ok.add(iid)
                    skipped_fresh.add(iid)
                elif status == "done":
                    finished_ok.add(iid)
                    done.add(iid)
                elif status == "failed":
                    failed.add(iid)
                elif status == "waiting_gate":
                    waiting_gate.add(iid)

        return RunSummary(
            run_id=rid,
            done=tuple(sorted(done)),
            skipped_fresh=tuple(sorted(skipped_fresh)),
            failed=tuple(sorted(failed)),
            waiting_gate=tuple(sorted(waiting_gate)),
            blocked=tuple(sorted(blocked)),
        )

    # --- one instance ------------------------------------------------------------------

    async def _process_instance(
        self,
        rid: str,
        journal: Journal,
        graph: BuildGraph,
        iid: str,
        staleness: Mapping[str, Staleness],
    ) -> tuple[InstanceStatus, FailureLabel | None]:
        instance = graph.instances[iid]
        rule = graph.rules[instance.rule_id]
        state = staleness[iid]

        with self.tracer.span(
            tracing.SPAN_RULE,
            **{
                tracing.RULE_ID: rule.id,
                tracing.RULE_INSTANCE: iid,
                tracing.RULE_KIND: rule.kind,
            },
        ) as rule_span:
            return await self._process_instance_body(
                rid, journal, iid, instance, rule, state, rule_span
            )

    async def _process_instance_body(
        self,
        rid: str,
        journal: Journal,
        iid: str,
        instance: RuleInstance,
        rule: RuleSpec,
        state: Staleness,
        rule_span: tracing.SpanHandle,
    ) -> tuple[InstanceStatus, FailureLabel | None]:
        def _fail(label: FailureLabel, message: str) -> None:
            rule_span.set(tracing.FAILURE_LABEL, label)
            rule_span.error(message)

        if state.state == "fresh":
            self._emit(journal, rid, "rule_done", iid, payload={"skipped": "fresh"})
            return "fresh", None

        if state.state == "diverged":
            message = "output edited by hand; approve or revert first"
            self._emit(
                journal,
                rid,
                "rule_fail",
                iid,
                failure_label="constraint",
                payload={"message": message},
            )
            _fail("constraint", message)
            return "failed", "constraint"

        if rule.gate:
            gate_id = _format_gate(rule.gate, instance.params)
            gate_status: GateStatus = (
                self.gates.status(gate_id, instance) if self.gates is not None else "waiting"
            )
            if gate_status == "waiting":
                self._emit(journal, rid, "gate_wait", iid, payload={"gate": gate_id})
                return "waiting_gate", None
            if gate_status == "rejected":
                message = "gate rejected"
                self._emit(
                    journal,
                    rid,
                    "rule_fail",
                    iid,
                    failure_label="planning",
                    payload={"gate": gate_id, "message": message},
                )
                _fail("planning", message)
                return "failed", "planning"

        self._emit(journal, rid, "rule_start", iid, payload={})

        executor = self.executors.get(rule.kind)
        if executor is None:
            message = f"no executor registered for rule kind {rule.kind!r}"
            self._emit(
                journal, rid, "rule_fail", iid, failure_label="infra", payload={"message": message}
            )
            _fail("infra", message)
            return "failed", "infra"

        exec_span_name = tracing.SPAN_INVOKE_AGENT if rule.kind == "agent" else tracing.SPAN_EXECUTE
        with self.tracer.span(exec_span_name) as exec_span:
            outcome = await executor.execute(rule, instance)
            if not outcome.ok:
                exec_span.error(outcome.message or "execution failed")
        if not outcome.ok:
            label: FailureLabel = outcome.failure_label or "infra"
            self._emit(
                journal,
                rid,
                "rule_fail",
                iid,
                failure_label=label,
                payload={"message": outcome.message},
            )
            _fail(label, outcome.message)
            return "failed", label

        if self.checks is not None:
            for check_id in rule.checks:
                check_attrs = {tracing.CHECK_ID: check_id}
                with self.tracer.span(tracing.SPAN_CHECK, **check_attrs) as check_span:
                    result = await self.checks.run(check_id, instance)
                    check_span.set(tracing.CHECK_STATUS, result.status)
                    if not result.ok:
                        check_span.error(f"check {check_id} failed")
                self._emit(
                    journal, rid, "check_result", iid, payload=result.model_dump(mode="json")
                )
                if not result.ok:
                    message = f"check {check_id} failed"
                    self._emit(
                        journal,
                        rid,
                        "rule_fail",
                        iid,
                        failure_label="verification",
                        payload={"message": message},
                    )
                    _fail("verification", message)
                    return "failed", "verification"

        for ref in instance.outputs:
            if not self.store.exists(ref):
                message = f"output {ref.path} missing"
                self._emit(
                    journal,
                    rid,
                    "rule_fail",
                    iid,
                    failure_label="verification",
                    payload={"message": message},
                )
                _fail("verification", message)
                return "failed", "verification"

        record = self._build_record(instance)
        self.records.put(record)
        self._emit(journal, rid, "rule_done", iid, payload={})
        return "done", None

    def _build_record(self, instance: RuleInstance) -> ProductionRecord:
        path_inputs = [ref for ref in instance.inputs if ref.path is not None]
        current_inputs = self.store.current_hashes(path_inputs)
        inputs_hash = hash_inputs(
            (_ref_key(ref), current_inputs.get(_ref_key(ref), "")) for ref in instance.inputs
        )
        output_hashes = self.store.current_hashes(instance.outputs)
        return ProductionRecord(
            instance_id=instance.instance_id,
            inputs_hash=inputs_hash,
            output_hashes=output_hashes,
        )

    def _emit(
        self,
        journal: Journal,
        rid: str,
        type_: str,
        rule_instance: str | None = None,
        *,
        payload: dict[str, object] | None = None,
        failure_label: FailureLabel | None = None,
    ) -> None:
        journal.append(
            Event(
                run_id=rid,
                seq=journal.next_seq(),
                ts=datetime.now(UTC),
                type=type_,  # type: ignore[arg-type]
                rule_instance=rule_instance,
                payload=payload or {},
                failure_label=failure_label,
            )
        )


__all__ = [
    "AgentStub",
    "CheckRunner",
    "ExecOutcome",
    "Executor",
    "GateChecker",
    "GateStatus",
    "RunSummary",
    "Scheduler",
    "SchedulerError",
]
