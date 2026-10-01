"""The MCP server itself (task M0-14): `build_server` wires up an `mcp` 2.x `MCPServer`
with the five M0 tools, and `run_stdio` runs it over stdio.

The `init` tool (M1-16) is meant for a project with no `.chipgraph.yml` yet: it previews,
and with `confirm`, writes the profile `chipgraph init` would write.

Each tool call loads a fresh `chipgraph.app.context.AppContext` from `start` (and
`profile_path`, if given): a stdio session is long-lived, so a project's
`.chipgraph.yml` may change between calls, and every tool must see the current one
(no caching across calls). All the actual work is delegated to the same
`chipgraph.app` functions `chipgraph.cli` uses, so behavior never drifts between the
CLI and the MCP surface.

`AppError`/`ConfigError` (and `chipgraph.core.engine.gate.GateError`) are caught and
turned into `mcp.server.mcpserver.exceptions.ToolError`, which the SDK reports back to
the caller as a tool result with `isError=True` and the message as text -- never an
unhandled exception that would crash the server loop.
"""

from __future__ import annotations

import asyncio
import functools
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from chipgraph import __version__
from chipgraph.adapters.runtime.claude_code import decisions as cc_decisions
from chipgraph.adapters.runtime.claude_code import service as cc_service
from chipgraph.app.build import make_scheduler
from chipgraph.app.checks import ProfileCheckRunner
from chipgraph.app.context import AppContext, default_identity, find_repo_root
from chipgraph.app.errors import AppError
from chipgraph.core.config.errors import ConfigError
from chipgraph.core.contracts import ArtifactRef, RuleInstance
from chipgraph.core.engine import gate as gate_mod
from chipgraph.core.state import journal as journal_mod
from chipgraph.core.state.layout import StateLayout
from chipgraph.mcp import model_tools

__all__ = ["build_server", "run_stdio"]


def _latest_run_id(layout: StateLayout) -> str | None:
    """The id of the most recently created run, or `None` if there has never been one.

    A minimal re-implementation of `chipgraph.cli._latest_run_id` (private to the CLI
    module): both list `.chipgraph/state/runs` and take the lexicographically last id
    (run ids are time-sortable, see `chipgraph.core.state.layout.new_run_id`).
    """
    if not layout.runs_dir.is_dir():
        return None
    ids = sorted(p.name for p in layout.runs_dir.iterdir() if p.is_dir())
    return ids[-1] if ids else None


def _fake_check_instance(check_id: str, block: str | None) -> RuleInstance:
    """A throwaway `RuleInstance` carrying just the params `ProfileCheckRunner` reads.

    A minimal re-implementation of `chipgraph.cli._fake_instance` (private to the CLI
    module): `chipgraph check`/the `check` tool run checks directly, outside the build
    graph, so there is no real rule instance to hand the runner.
    """
    params = {"block": block} if block is not None else {}
    rule_id = "chipgraph/check"
    safe = "".join(c if c.isalnum() else "_" for c in check_id)
    output_path = f".chipgraph/tmp/check-{safe}-{block or 'all'}.json"
    return RuleInstance(
        rule_id=rule_id,
        params=params,
        outputs=(ArtifactRef(kind="report", path=output_path),),
        instance_id=RuleInstance.make_id(rule_id, params),
    )


def _json_safe(value: object) -> object:
    """Make a `ResolvedProfile.explain()` leaf value JSON-serialisable (tuples -> lists)."""
    if isinstance(value, tuple | list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return value


def _guard[F: Callable[..., Awaitable[dict[str, Any]]]](func: F) -> F:
    """Turn `AppError`/`ConfigError`/`GateError` into an MCP `ToolError`.

    Anything else propagates and the SDK reports it as an unexpected tool error
    (still a tool result, never a crash of the server loop -- see `_handle_call_tool`
    in `mcp.server.mcpserver.server`).
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await func(*args, **kwargs)
        except (AppError, ConfigError, gate_mod.GateError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper  # type: ignore[return-value]


def build_server(start: Path, *, profile_path: Path | None = None) -> MCPServer:
    """Build the MCP server for the project rooted at (or above) `start`.

    Registers `status`, `build`, `check`, `approve` and `config_show`. `start` and
    `profile_path` are the server's fixed project start directory / profile override
    (mirroring the CLI's `-C/--dir` and `--profile`); everything else about the
    project (the profile, the graph, the state) is re-read from disk on every call.
    """
    server: MCPServer = MCPServer(name="chipgraph", version=__version__)

    def _load_ctx() -> AppContext:
        return AppContext.load(start, profile_path=profile_path)

    @server.tool(description="Show the status of the latest run, or a given run id.")
    @_guard
    async def status(run_id: str | None = None) -> dict[str, Any]:
        ctx = _load_ctx()
        layout = ctx.layout
        rid = run_id or _latest_run_id(layout)
        if rid is None:
            return {"runs": []}

        journal_path = layout.journal(rid)
        if not journal_path.is_file():
            raise ToolError(f"no such run {rid!r}")
        journal_read = journal_mod.read(journal_path)
        run_state = journal_mod.replay(journal_read.events)

        gate_of: dict[str, str] = {}
        for event in journal_read.events:
            if event.type == "gate_wait" and event.rule_instance is not None:
                gate_of[event.rule_instance] = str(event.payload.get("gate", ""))
        waiting = {
            iid: gate_of.get(iid, "") for iid, st in run_state.rules.items() if st == "waiting_gate"
        }
        counts = Counter(run_state.rules.values())

        return {
            "run_id": rid,
            "started": run_state.started,
            "stopped": run_state.stopped,
            "rules": dict(run_state.rules),
            "counts": dict(counts),
            "waiting_gates": waiting,
        }

    @server.tool(description="Build a target with the scheduler and return the run summary.")
    @_guard
    async def build(target: str, concurrency: int = 4) -> dict[str, Any]:
        ctx = _load_ctx()
        ctx.require_profile()
        scheduler = make_scheduler(ctx, target, concurrency=concurrency)
        summary = await scheduler.run(target)
        return summary.model_dump(mode="json")

    @server.tool(
        description="Run checks directly against the working tree, without the build graph."
    )
    @_guard
    async def check(
        blocks: list[str] | None = None, only: list[str] | None = None
    ) -> dict[str, Any]:
        ctx = _load_ctx()
        resolved = ctx.require_profile()

        check_ids = list(only) if only else sorted(resolved.profile.adapters)
        raw_blocks = list(blocks) if blocks else sorted(resolved.profile.blocks)
        block_list: list[str | None] = list(raw_blocks) if raw_blocks else [None]

        runner = ProfileCheckRunner(ctx)
        results = []
        for check_id in check_ids:
            for block in block_list:
                instance = _fake_check_instance(check_id, block)
                results.append(await runner.run(check_id, instance))

        return {
            "ok": all(result.ok for result in results),
            "results": [result.model_dump(mode="json") for result in results],
        }

    @server.tool(
        description="Run every deterministic check over the whole project, grouped by layer."
    )
    @_guard
    async def audit(blocks: list[str] | None = None, ingest: bool = True) -> dict[str, Any]:
        from chipgraph.packs.assist.audit import run_audit

        ctx = _load_ctx()
        ctx.require_profile()
        block_list = list(blocks) if blocks else None
        # `run_audit` is synchronous and drives its own event loop; run it in a worker
        # thread so it does not clash with the MCP server's running loop.
        report = await asyncio.to_thread(run_audit, ctx, ingest=ingest, blocks=block_list)
        return report.model_dump(mode="json")

    @server.tool(
        description="Record an approve or reject decision for a gate over a rule instance."
    )
    @_guard
    async def approve(
        gate_id: str,
        instance: str,
        reject: bool = False,
        by: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        ctx = _load_ctx()
        ctx.require_profile()
        scheduler = make_scheduler(ctx, "*")
        rule_instance = scheduler.graph.instances.get(instance)
        if rule_instance is None:
            raise ToolError(
                f"no such rule instance {instance!r}; known instances: "
                f"{sorted(scheduler.graph.instances)}"
            )
        decision: Literal["approve", "reject"] = "reject" if reject else "approve"
        who = by or default_identity()
        approval = gate_mod.approve(
            ctx.review,
            ctx.store,
            gate_id,
            rule_instance,
            by=who,
            decision=decision,
            note=note or "",
        )
        return approval.model_dump(mode="json")

    @server.tool(description="Show the merged project profile, optionally with each key's source.")
    @_guard
    async def config_show(explain: bool = False) -> dict[str, Any]:
        ctx = _load_ctx()
        resolved = ctx.require_profile()
        if explain:
            return {
                "explain": [
                    {
                        "key": key,
                        "value": _json_safe(value),
                        "source": {"kind": source.kind, "location": source.location},
                    }
                    for key, value, source in resolved.explain()
                ]
            }
        return {"profile": resolved.profile.model_dump(mode="json")}

    # --- runtime claude-code (M1-11): the task loop the plugin command drives ----------
    # One lock for the three tools: the stdio session may run calls concurrently, and
    # each reads and writes the same task queue.
    runtime_lock = asyncio.Lock()

    @server.tool(
        description=(
            "Run the build and hand out the agent tasks that are ready: tasks (start one "
            "role subagent per task, in parallel), in_progress, and done or waiting."
        )
    )
    @_guard
    async def next_task(target: str = "*") -> dict[str, Any]:
        async with runtime_lock:
            return await cc_service.next_task(_load_ctx(), target)

    @server.tool(
        description=(
            "The context of one dispatched task, for its role subagent: inputs as text, "
            "outputs (the only files it may write), role, skills, checks."
        )
    )
    @_guard
    async def get_context(task_id: str) -> dict[str, Any]:
        async with runtime_lock:
            return await cc_service.get_context(_load_ctx(), task_id)

    @server.tool(
        description=(
            "Hand a task back: the engine checks that only its outputs changed, that they "
            "exist, and runs the rule's checks; then accepts or rejects it."
        )
    )
    @_guard
    async def submit(task_id: str, result: cc_service.SubmitReport | None = None) -> dict[str, Any]:
        async with runtime_lock:
            return await cc_service.submit(_load_ctx(), task_id, result)

    # --- decide() in runtime claude-code (M1-12): questions for the decider subagent ---

    @server.tool(
        description=(
            "Questions decide() queued for a model: for each, start one decider subagent "
            "(agent, model, prompt as given) and pass its JSON answer to answer_decision."
        )
    )
    @_guard
    async def pending_decisions() -> dict[str, Any]:
        async with runtime_lock:
            return cc_decisions.pending_decisions(_load_ctx().layout)

    @server.tool(
        description=(
            "Record the decider's answer to a pending question: value (one of its "
            "choices), confidence from 0 to 1, and a short reason."
        )
    )
    @_guard
    async def answer_decision(
        question_id: str, value: str, confidence: float, reason: str = ""
    ) -> dict[str, Any]:
        async with runtime_lock:
            try:
                return cc_decisions.answer_decision(
                    _load_ctx().layout, question_id, value, confidence, reason
                )
            except cc_decisions.DecisionStateError as exc:
                raise ToolError(str(exc)) from exc

    @server.tool(
        description="Retrieve a block and what it contains: modules, ports, registers, interrupts."
    )
    @_guard
    async def model_block(name: str) -> dict[str, Any]:
        ctx = _load_ctx()
        return await model_tools.model_block(ctx, name)

    @server.tool(description="Retrieve a module: ports, parameters, instances, instantiated_by.")
    @_guard
    async def model_module(name: str) -> dict[str, Any]:
        ctx = _load_ctx()
        return await model_tools.model_module(ctx, name)

    @server.tool(description="Find entities by kind, name (glob pattern), or attributes.")
    @_guard
    async def model_find(
        kind: str | None = None,
        name: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        ctx = _load_ctx()
        return await model_tools.model_find(ctx, kind=kind, name=name, limit=limit)

    @server.tool(description="Trace a requirement or entity: implements, verifies, derives_from.")
    @_guard
    async def model_trace(key: str) -> dict[str, Any]:
        ctx = _load_ctx()
        return await model_tools.model_trace(ctx, key)

    @server.tool(description="Compute downstream impact from a change: reachable entities.")
    @_guard
    async def model_impact(key: str, max_depth: int = 5) -> dict[str, Any]:
        ctx = _load_ctx()
        return await model_tools.model_impact(ctx, key, max_depth=max_depth)

    @server.tool(description="Find adjacent entities (one-hop neighbors).")
    @_guard
    async def model_neighbors(
        key: str,
        kinds: list[str] | None = None,
        relation: str | None = None,
        direction: str = "out",
    ) -> dict[str, Any]:
        ctx = _load_ctx()
        return await model_tools.model_neighbors(
            ctx, key, kinds=kinds, relation=relation, direction=direction
        )

    @server.tool(description="Full-text search over entities and documents.")
    @_guard
    async def model_search(
        text: str,
        kinds: list[str] | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        ctx = _load_ctx()
        return await model_tools.model_search(ctx, text, kinds=kinds, limit=limit)

    # --- /ask (M1-13): retrieval and the citation check the asker subagent uses ----------

    @server.tool(
        description=(
            "The sources for one /ask question: typed Design Model lookups and full-text "
            "hits in the project's documents, each with the exact citation to use "
            "('model:<key>' or 'path:line'). Answer only from these; none means unknown."
        )
    )
    @_guard
    async def ask_context(question: str, limit: int = 12) -> dict[str, Any]:
        from chipgraph.packs.assist.ask import ask_context as run_ask_context

        result = run_ask_context(_load_ctx(), question, limit)
        return result.model_dump(mode="json")

    @server.tool(
        description=(
            "Check an /ask answer before it is shown: every citation must be an indexed "
            "'path:line' or a 'model:<key>', and an answer that is not unknown needs at "
            "least one. Returns ok and the verified answer, or the reasons to fix."
        )
    )
    @_guard
    async def ask_check(
        answer: str, citations: list[str] | None = None, unknown: bool = False
    ) -> dict[str, Any]:
        from chipgraph.packs.assist.ask import AskAnswer
        from chipgraph.packs.assist.ask import ask_check as run_ask_check

        submitted = AskAnswer(answer=answer, citations=tuple(citations or ()), unknown=unknown)
        return run_ask_check(_load_ctx(), submitted).model_dump(mode="json")

    # --- /triage (M1-14): classify a failing log; models answer through the decider ----

    @server.tool(
        description=(
            "Classify a failing lint/sim/check log as infra, rtl, tb or spec, with a "
            "summary, a suggestion and evidence. Give `path` (a log file inside the "
            "project) or `log` (its text), and `check_id` if known. Rules answer first; "
            "otherwise status is 'deferred': answer pending_decisions with the decider "
            "subagent (answer_decision), then call triage again with the same arguments."
        )
    )
    @_guard
    async def triage(
        path: str | None = None, log: str | None = None, check_id: str | None = None
    ) -> dict[str, Any]:
        from chipgraph.packs.assist.triage import triage_log

        ctx = _load_ctx()
        profile = ctx.require_profile().profile
        if (path is None) == (log is None):
            raise ToolError("give exactly one of `path` (a log file) or `log` (its text)")
        source: str | None = None
        if path is not None:
            root = ctx.root.resolve()
            target = (root / path).resolve()
            if not target.is_relative_to(root):
                raise ToolError(f"{path!r} is outside the project; give a path inside it")
            if not target.is_file():
                raise ToolError(f"no log file {path!r} in the project")
            log = target.read_text(encoding="utf-8", errors="replace")
            source = target.relative_to(root).as_posix()
        assert log is not None
        # In Claude Code the model runs in the user's session (D35): queue the question
        # for the decider subagent, whatever API runtime the profile names.
        backend = cc_decisions.ClaudeCodeDecideBackend(ctx.layout, profile.models)
        async with runtime_lock:
            report = await triage_log(ctx, log, source=source, check_id=check_id, backend=backend)
        return report.model_dump(mode="json")

    # --- /init-chipgraph (M1-16): `chipgraph init`, previewed before it writes -------

    @server.tool(
        description=(
            "Set chipgraph up in this project (works with no .chipgraph.yml): returns the "
            ".chipgraph.yml `chipgraph init` would write (a stub, or inferred from the repo "
            "with from_learn). Writes it only with confirm=true, and never overwrites an "
            "existing one without force=true. Show the preview to the user first."
        )
    )
    @_guard
    async def init(
        project: str | None = None,
        preset: str | None = None,
        from_learn: bool = False,
        confirm: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            _init_project,
            find_repo_root(start),
            project=project,
            preset=preset,
            from_learn=from_learn,
            confirm=confirm,
            force=force,
        )

    return server


_INIT_NEXT_STEPS = (
    "chipgraph config check     # validate the profile",
    "chipgraph doctor           # check tools are on PATH",
    "chipgraph ingest           # build the Design Model (for /chipgraph:ask and /chipgraph:trace)",
)


def _init_preview(
    root: Path, *, project: str | None, preset: str | None, from_learn: bool
) -> tuple[str, list[dict[str, str]]]:
    """The `.chipgraph.yml` text `chipgraph init` would write into `root`, and any other
    file it would write (`--from-learn`'s naming rules), without writing anything.

    The stub is `chipgraph.cli._init_content`, the CLI's own text. The `--from-learn`
    text is composed from the same `chipgraph.learn` functions `init_from_learn` uses
    (the write itself goes through `init_from_learn`; a test checks the two agree).
    """
    if not from_learn:
        from chipgraph.cli import _init_content

        return _init_content(project or root.name, preset), []

    from chipgraph.learn import (
        _INIT_NAMING_RULES_REL,
        build_naming_rules,
        build_profile_dict,
        dump_naming_rules_yaml,
        learn,
    )
    from chipgraph.learn.draft import dump_yaml

    result = learn(root)
    rules = build_naming_rules(result)
    others: list[dict[str, str]] = []
    naming_ref: str | None = None
    if rules is not None:
        naming_ref = _INIT_NAMING_RULES_REL
        others.append({"path": naming_ref, "content": dump_naming_rules_yaml(rules)})
    header = (
        "# chipgraph project profile written by `chipgraph init --from-learn` (DESIGN.md 8.6).\n"
        "# Review it: each rule was inferred from the repo. Edit freely.\n"
    )
    data = build_profile_dict(result, naming_rules_ref=naming_ref)
    return header + dump_yaml(data), others


def _init_project(
    root: Path,
    *,
    project: str | None,
    preset: str | None,
    from_learn: bool,
    confirm: bool,
    force: bool,
) -> dict[str, Any]:
    """The `init` tool: preview `chipgraph init`, and with `confirm`, do it.

    Mirrors the CLI command: refuses to overwrite an existing `.chipgraph.yml` unless
    `force`; `from_learn` infers the profile (and naming rules) from the repo; otherwise
    it writes the stub (`project` defaults to the root directory's name, `preset` adds
    `extends`). `preset` and `project` are ignored with `from_learn`, as in the CLI.
    """
    profile = root / ".chipgraph.yml"
    exists = profile.is_file()
    content, others = _init_preview(root, project=project, preset=preset, from_learn=from_learn)
    reply: dict[str, Any] = {
        "path": ".chipgraph.yml",
        "exists": exists,
        "written": False,
        "content": content,
        "other_files": others,
    }
    if exists and not force:
        if confirm:
            raise ToolError(".chipgraph.yml already exists; pass force=true to overwrite it")
        reply["message"] = (
            ".chipgraph.yml already exists: nothing will be written unless force=true "
            "(which overwrites it)."
        )
        return reply
    if not confirm:
        verb = "overwrite" if exists else "write"
        reply["message"] = (
            f"Preview only: nothing was written. Call init again with confirm=true to {verb} "
            "these files."
        )
        return reply

    if from_learn:
        from chipgraph.learn import InitFromLearnError, init_from_learn

        old = profile.read_bytes() if exists else None
        if exists:
            profile.unlink()
        try:
            _, naming_path = init_from_learn(root)
        except Exception as exc:
            if old is not None and not profile.exists():
                profile.write_bytes(old)  # keep the user's file if the write failed
            if isinstance(exc, InitFromLearnError):
                raise ToolError(str(exc)) from exc
            raise
        written = [".chipgraph.yml"]
        if naming_path is not None:
            written.append(naming_path.relative_to(root).as_posix())
        reply["content"] = profile.read_text(encoding="utf-8")
    else:
        profile.write_text(content, encoding="utf-8")
        written = [".chipgraph.yml"]
    reply["written"] = True
    reply["files_written"] = written
    reply["message"] = f"wrote {', '.join(written)}; review it, then run the next steps."
    reply["next_steps"] = list(_INIT_NEXT_STEPS)
    return reply


async def run_stdio(start: Path, profile_path: Path | None = None) -> None:
    """Run the MCP server over stdio for the project rooted at (or above) `start`."""
    server = build_server(start, profile_path=profile_path)
    await server.run_stdio_async()
