"""chipgraph CLI (task M0-12): commands that run a project's build graph.

A command is small on purpose: it parses options, builds an `AppContext` (and, when
needed, a `Scheduler` via `chipgraph.app.build.make_scheduler`), and prints the result.
All the wiring lives in `chipgraph.app` (DESIGN.md 3.3, 10.1, 10.4).

Exit codes: 0 ok; 1 a check/run failed or is waiting; 2 usage/config error.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import shutil
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal

import typer
import yaml

from chipgraph import __version__
from chipgraph.app import findings as findings_app
from chipgraph.app.baseline import (
    current_branch,
    main_branch,
    plan_project_baseline,
    run_baseline,
)
from chipgraph.app.build import make_scheduler
from chipgraph.app.checks import ProfileCheckRunner
from chipgraph.app.context import AppContext, default_identity, find_repo_root
from chipgraph.app.errors import AppError
from chipgraph.core.config.errors import ConfigError
from chipgraph.core.config.loader import ConfigIssue
from chipgraph.core.contracts import ArtifactRef, CheckResult, Finding, RuleInstance
from chipgraph.core.engine import gate as gate_mod
from chipgraph.core.engine.scheduler import RunSummary
from chipgraph.core.plugin_api.local import LocalPluginRecord
from chipgraph.core.state import journal as journal_mod
from chipgraph.core.state import trace as trace_mod
from chipgraph.core.state.findings import FindingStore, effective_status, waiver_gate_id
from chipgraph.core.state.layout import StateLayout
from chipgraph.core.state.trace import Tracer

TraceExporter = Literal["none", "console", "otlp"]

app = typer.Typer(no_args_is_help=True)
config_app = typer.Typer(no_args_is_help=True, help="Inspect the resolved profile.")
app.add_typer(config_app, name="config")


class CliState:
    """The global options every command reads: `--profile`, `--json`, `-C/--dir`, `--trace`."""

    def __init__(
        self,
        *,
        profile_path: Path | None,
        json_output: bool,
        start: Path,
        trace: TraceExporter,
    ) -> None:
        self.profile_path = profile_path
        self.json_output = json_output
        self.start = start
        self.trace = trace


def _load_ctx(state: CliState) -> AppContext:
    return AppContext.load(state.start, profile_path=state.profile_path)


def _handle_errors[F: Callable[..., None]](func: F) -> F:
    """Catch `AppError`/`ConfigError`, print the message, and exit 2."""

    @functools.wraps(func)
    def wrapper(*args: object, **kwargs: object) -> None:
        try:
            func(*args, **kwargs)
        except (AppError, ConfigError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc

    return wrapper  # type: ignore[return-value]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"chipgraph {__version__}")
        raise typer.Exit(code=0)


def _default_trace() -> TraceExporter:
    """`$CHIPGRAPH_TRACE`, if it names a known exporter, else `"none"`."""
    value = os.environ.get("CHIPGRAPH_TRACE", "none").strip().lower()
    if value in ("none", "console", "otlp"):
        return value  # type: ignore[return-value]
    return "none"


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the chipgraph version and exit.",
        ),
    ] = False,
    profile: Annotated[
        Path | None,
        typer.Option("--profile", help="Use this profile file instead of discovering one."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print machine-readable JSON instead of text.")
    ] = False,
    directory: Annotated[
        Path | None,
        typer.Option("-C", "--dir", help="Start looking for a profile from this directory."),
    ] = None,
    trace: Annotated[
        TraceExporter | None,
        typer.Option(
            "--trace",
            help="Export OpenTelemetry spans for build/resume ('none'|'console'|'otlp'); "
            "default from $CHIPGRAPH_TRACE, else 'none'.",
        ),
    ] = None,
) -> None:
    """chipgraph: an AI agent system for chip (IC) design, built on a build graph."""
    start = directory if directory is not None else Path.cwd()
    ctx.obj = CliState(
        profile_path=profile,
        json_output=json_output,
        start=start,
        trace=trace if trace is not None else _default_trace(),
    )


# --- init --------------------------------------------------------------------------


def _init_content(project: str, preset: str | None) -> str:
    lines = [
        "# chipgraph project profile (see DESIGN.md 8.3-8.6)",
        "# generated by `chipgraph init`; edit freely.",
        f"project: {project}",
    ]
    if preset:
        lines.append(f'extends: ["preset:{preset}"]')
    lines += [
        "packs: []",
        "adapters: {}",
        "# adapters:",
        "#   lint:",
        "#     use: cmd",
        '#     cmd: "make lint BLOCK={block}"',
        "#     parser: verilator",
        "blocks: {}",
        "",
    ]
    return "\n".join(lines) + "\n"


@app.command()
@_handle_errors
def init(
    ctx: typer.Context,
    project: Annotated[str | None, typer.Option("--project", help="Project name.")] = None,
    preset: Annotated[
        str | None, typer.Option("--preset", help="Extend an org/preset profile.")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing .chipgraph.yml.")
    ] = False,
    from_learn: Annotated[
        bool,
        typer.Option(
            "--from-learn",
            help="Infer the profile from this repo (like `chipgraph learn`) instead of a stub.",
        ),
    ] = False,
) -> None:
    """Write a `.chipgraph.yml` at the repo root (a stub, or inferred with `--from-learn`)."""
    state: CliState = ctx.obj
    root = find_repo_root(state.start)
    path = root / ".chipgraph.yml"
    if path.is_file() and not force:
        typer.echo(f"{path} already exists; pass --force to overwrite", err=True)
        raise typer.Exit(code=2)

    if from_learn:
        from chipgraph.learn import InitFromLearnError, init_from_learn

        if path.is_file() and force:
            path.unlink()
        try:
            profile_path, naming_path = init_from_learn(root)
        except InitFromLearnError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        typer.echo(f"wrote {profile_path} (inferred from {root})")
        if naming_path is not None:
            typer.echo(f"wrote {naming_path}")
        typer.echo("review it, then:")
        typer.echo("  chipgraph config check     # validate the profile")
        return

    name = project or root.name
    path.write_text(_init_content(name, preset), encoding="utf-8")
    typer.echo(f"wrote {path}")
    typer.echo("next steps:")
    typer.echo("  chipgraph config check     # validate the profile")
    typer.echo("  chipgraph doctor           # check tools are on PATH")
    typer.echo("  chipgraph learn            # fill this in from an existing repo (task M1-21)")


# --- learn / try --------------------------------------------------------------------


def _learn_json(result: object) -> str:
    from chipgraph.learn import LearnResult

    assert isinstance(result, LearnResult)
    return json.dumps(result.model_dump(mode="json"), indent=2)


def _print_learn_text(result: object) -> None:
    from chipgraph.learn import LearnResult

    assert isinstance(result, LearnResult)
    typer.echo(f"# learned {result.project} from {result.root}")
    typer.echo(f"# blocks: {', '.join(result.blocks) or '(none, single block)'}")
    typer.echo("")
    typer.echo("layout (kind: template  coverage):")
    for lrule in result.layout:
        typer.echo(f"  {lrule.kind:9} {lrule.template:34} {lrule.coverage.percent:3}%")
    typer.echo("naming (kind: pattern  coverage  emitted):")
    for nrule in result.naming:
        mark = "rule" if nrule.emitted else "obs "
        typer.echo(
            f"  {mark} {nrule.kind:11} {nrule.pattern:30} {nrule.coverage.percent:3}% "
            f"({nrule.coverage.matched}/{nrule.coverage.total})"
        )
    if result.observations:
        typer.echo("observations:")
        for obs in result.observations:
            typer.echo(f"  {obs.topic:9} {obs.summary}")
    if result.vendor_paths:
        typer.echo("vendor paths (checks off, learned):")
        for glob in result.vendor_paths:
            typer.echo(f"  {glob}")


@app.command()
@_handle_errors
def learn(
    ctx: typer.Context,
    path: Annotated[
        Path | None, typer.Argument(help="Repo to learn (default: the current directory).")
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Write chipgraph.draft.yml (+ naming rules) into this dir."),
    ] = None,
    threshold: Annotated[
        float,
        typer.Option("--threshold", min=0.0, max=1.0, help="Min coverage to emit a naming rule."),
    ] = 0.9,
) -> None:
    """Infer a draft profile from an existing repo, with per-rule coverage (DESIGN 8.6 V1)."""
    from chipgraph.learn import dump_profile_yaml, write_draft
    from chipgraph.learn import learn as run_learn
    from chipgraph.learn.draft import DRAFT_NAMING_RULES_NAME, build_naming_rules

    state: CliState = ctx.obj
    root = (path if path is not None else state.start).resolve()
    if not root.is_dir():
        typer.echo(f"not a directory: {root}", err=True)
        raise typer.Exit(code=2)

    result = run_learn(root, threshold=threshold)

    if state.json_output:
        typer.echo(_learn_json(result))
    else:
        _print_learn_text(result)
        naming_ref = DRAFT_NAMING_RULES_NAME if build_naming_rules(result) is not None else None
        typer.echo("")
        typer.echo("draft profile:")
        typer.echo(dump_profile_yaml(result, naming_rules_ref=naming_ref))

    if out is not None:
        profile_path, naming_path = write_draft(result, out)
        typer.echo(f"wrote {profile_path}")
        if naming_path is not None:
            typer.echo(f"wrote {naming_path}")


@app.command("try")
@_handle_errors
def try_cmd(
    ctx: typer.Context,
    path: Annotated[
        Path | None, typer.Argument(help="Repo to try (default: the current directory).")
    ] = None,
    profile: Annotated[
        Path | None,
        typer.Option("--profile", help="Use this draft profile (from `learn --out`) instead."),
    ] = None,
    threshold: Annotated[
        float,
        typer.Option("--threshold", min=0.0, max=1.0, help="Min coverage to emit a naming rule."),
    ] = 0.9,
) -> None:
    """Run chipgraph read-only against a repo (ingest/check/audit), writing nothing into it."""
    from chipgraph.learn.try_run import try_run

    state: CliState = ctx.obj
    root = (path if path is not None else state.start).resolve()
    if not root.is_dir():
        typer.echo(f"not a directory: {root}", err=True)
        raise typer.Exit(code=2)

    report = try_run(root, profile_path=profile, threshold=threshold)

    if state.json_output:
        payload = {
            "project": report.result.project,
            "ingest_ok": report.ingest_ok,
            "ingest": report.ingest_summary,
            "audit_available": report.audit_available,
            "audit": report.audit_summary,
            "files_with_issues": report.check_files_with_issues,
            "checks": [
                {"check": cid, "block": blk, "status": res.status, "issues": len(res.issues)}
                for cid, blk, res in report.checks
            ],
        }
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(f"try {report.result.project} (read-only; nothing written to the repo)")
        typer.echo(f"  ingest: {report.ingest_summary}")
        for cid, blk, res in report.checks:
            typer.echo(f"  {res.status.upper():6} {cid} {blk or '-'} {len(res.issues)} issues")
        typer.echo(f"  {report.audit_summary}")
        typer.echo(f"files with issues: {report.check_files_with_issues}")


# --- config show / config check -----------------------------------------------------


@config_app.command("show")
@_handle_errors
def config_show(
    ctx: typer.Context,
    explain: Annotated[
        bool, typer.Option("--explain", help="One line per leaf key, with its source.")
    ] = False,
) -> None:
    """Print the merged profile."""
    state: CliState = ctx.obj
    app_ctx = _load_ctx(state)
    resolved = app_ctx.require_profile()
    if explain:
        for key, value, source in resolved.explain():
            typer.echo(f"{key} = {value!r}  ({source.kind}:{source.location})")
        return
    typer.echo(yaml.safe_dump(resolved.profile.model_dump(mode="json"), sort_keys=True))


@config_app.command("check")
@_handle_errors
def config_check(ctx: typer.Context) -> None:
    """Print consistency issues in the resolved profile."""
    state: CliState = ctx.obj
    app_ctx = _load_ctx(state)
    resolved = app_ctx.require_profile()
    # Drop the generic per-plugin info line from `check()`: the richer lines built from
    # what actually loaded (below) replace it, keeping the "runs code" warning wording.
    issues = [i for i in resolved.check() if not i.key.startswith("plugins[")]
    plugins = [_plugin_issue(index, rec) for index, rec in enumerate(app_ctx.local_plugins)]

    if state.json_output:
        payload = [
            {"severity": i.severity, "key": i.key, "message": i.message}
            for i in (*issues, *plugins)
        ]
        typer.echo(json.dumps(payload, indent=2))
    else:
        if not issues and not plugins:
            typer.echo("no issues")
        for issue in (*issues, *plugins):
            typer.echo(f"{issue.severity:7} {issue.key}: {issue.message}")
    if any(issue.severity == "error" for issue in (*issues, *plugins)):
        raise typer.Exit(code=1)


def _plugin_issue(index: int, record: LocalPluginRecord) -> ConfigIssue:
    """One `config check` line per local plugin: loaded (with hash + registrations) or not."""
    key = f"plugins[{index}]"
    if not record.loaded:
        return ConfigIssue(
            severity="warning",
            key=key,
            message=f"{record.entry}: not loaded: {record.disabled_reason}",
        )
    registered = ", ".join(f"{kind}/{name}" for kind, name in record.registered) or "nothing"
    return ConfigIssue(
        severity="info",
        key=key,
        message=(
            f"{record.path} (sha256 {(record.sha256 or '')[:8]}) registered {registered} "
            "(runs code: review it like code)"
        ),
    )


# --- check -------------------------------------------------------------------------


def _fake_instance(check_id: str, block: str | None) -> RuleInstance:
    """A throwaway `RuleInstance` carrying just the params `ProfileCheckRunner` reads."""
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


async def _run_checks(
    runner: ProfileCheckRunner, combos: list[tuple[str, str | None]]
) -> list[tuple[str, str | None, CheckResult]]:
    results: list[tuple[str, str | None, CheckResult]] = []
    for check_id, block in combos:
        instance = _fake_instance(check_id, block)
        result = await runner.run(check_id, instance)
        results.append((check_id, block, result))
    return results


@app.command()
@_handle_errors
def check(
    ctx: typer.Context,
    block: Annotated[
        list[str], typer.Option("--block", help="Run only for this block (repeatable).")
    ] = [],  # noqa: B006
    only: Annotated[
        list[str], typer.Option("--only", help="Run only this check id (repeatable).")
    ] = [],  # noqa: B006
) -> None:
    """Run checks directly against the current working tree, without the build graph."""
    state: CliState = ctx.obj
    app_ctx = _load_ctx(state)
    resolved = app_ctx.require_profile()

    check_ids = list(only) if only else sorted(resolved.profile.adapters)
    raw_blocks = list(block) if block else sorted(resolved.profile.blocks)
    blocks: list[str | None] = list(raw_blocks) if raw_blocks else [None]

    runner = ProfileCheckRunner(app_ctx)
    # A chip-wide check (per_block = False) runs once for the whole project, unless the
    # user scoped the run with --block; running it per block would repeat its issues and
    # drop those that belong to no block.
    combos = [
        (cid, b)
        for cid in check_ids
        for b in (blocks if block or runner.runs_per_block(cid) else [None])
    ]
    results = asyncio.run(_run_checks(runner, combos))

    if state.json_output:
        typer.echo(json.dumps([r.model_dump(mode="json") for _, _, r in results], indent=2))
    else:
        for check_id, blk, result in results:
            status = result.status.upper()
            typer.echo(f"{status:6} {check_id} {blk or '-'} {len(result.issues)} issues")
            for issue in result.issues:
                loc = f"{issue.file or '-'}:{issue.line or '-'}"
                typer.echo(f"  {loc}  [{issue.rule}] {issue.msg}")

    if any(not r.ok for _, _, r in results):
        raise typer.Exit(code=1)


# --- ingest -------------------------------------------------------------------------


@app.command()
@_handle_errors
def ingest(
    ctx: typer.Context,
    strict: Annotated[
        bool, typer.Option("--strict", help="Exit 1 if any error-severity issue was found.")
    ] = False,
) -> None:
    """Run every extractor, build the Design Model, and print stats."""
    from chipgraph.app.ingest import run_ingest

    state: CliState = ctx.obj
    app_ctx = _load_ctx(state)
    app_ctx.require_profile()
    report = run_ingest(app_ctx)
    stats = report.result.stats

    if state.json_output:
        payload = {
            "db": str(report.db_path),
            "build_inputs_hash": report.result.build_inputs_hash,
            "stats": stats.model_dump(mode="json"),
            "issues": [i.model_dump(mode="json") for i in report.issues],
        }
        typer.echo(json.dumps(payload, indent=2))
    else:
        _print_ingest_text(stats, report)

    if strict and report.has_error:
        raise typer.Exit(code=1)


def _print_ingest_text(stats: object, report: object) -> None:
    from chipgraph.app.ingest import IngestReport
    from chipgraph.core.model.ingest import IngestStats

    assert isinstance(stats, IngestStats)
    assert isinstance(report, IngestReport)
    typer.echo(
        f"inputs: {stats.inputs}   entities: {stats.entities}   relations: {stats.relations}"
    )
    if stats.entities_by_kind:
        typer.echo("entities by kind:")
        for kind, count in sorted(stats.entities_by_kind.items()):
            typer.echo(f"  {count:5} {kind}")
    if stats.entities_by_block:
        typer.echo("entities by block:")
        for block, count in sorted(stats.entities_by_block.items()):
            typer.echo(f"  {count:5} {block or '(none)'}")
    by_sev = stats.diagnostics_by_severity
    if by_sev:
        summary = ", ".join(f"{sev}={by_sev[sev]}" for sev in sorted(by_sev))
        typer.echo(f"issues: {summary}   conflicts: {stats.conflicts}")
    else:
        typer.echo(f"issues: none   conflicts: {stats.conflicts}")
    shown = [i for i in report.issues if i.severity in ("warning", "error")][:20]
    for issue in shown:
        loc = f"{issue.file or '-'}:{issue.line or '-'}"
        typer.echo(f"  {issue.severity:7} [{issue.code}] {loc}  {issue.message}")
    typer.echo(f"db: {report.db_path}")


# --- build / resume / rewind --------------------------------------------------------


def _handoff_path(app_ctx: AppContext, run_id: str) -> Path:
    return app_ctx.layout.run_dir(run_id) / "HANDOFF.md"


def _print_summary(summary: RunSummary, json_output: bool, handoff_path: Path) -> None:
    if json_output:
        payload = summary.model_dump(mode="json")
        payload["handoff"] = str(handoff_path)
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(f"run {summary.run_id}")
    for label, ids in (
        ("done", summary.done),
        ("skipped (fresh)", summary.skipped_fresh),
        ("failed", summary.failed),
        ("waiting on a gate", summary.waiting_gate),
        ("blocked", summary.blocked),
    ):
        if ids:
            typer.echo(f"  {label}: {', '.join(ids)}")
    typer.echo(str(handoff_path))


def _build_tracer(state: CliState, *, offline: bool) -> Tracer:
    """Build the tracer `build`/`resume` should use for this invocation."""
    return trace_mod.configure(state.trace, offline=offline)


@app.command()
@_handle_errors
def build(
    ctx: typer.Context,
    target: Annotated[str, typer.Argument(help="Rule id, 'id[k=v]', or '*'.")],
    concurrency: Annotated[int, typer.Option("--concurrency", min=1)] = 4,
) -> None:
    """Build TARGET with the scheduler."""
    state: CliState = ctx.obj
    app_ctx = _load_ctx(state)
    resolved = app_ctx.require_profile()
    tracer = _build_tracer(state, offline=resolved.profile.offline)
    scheduler = make_scheduler(app_ctx, target, concurrency=concurrency, tracer=tracer)
    try:
        summary = asyncio.run(scheduler.run(target))
    finally:
        tracer.flush()
    _print_summary(summary, state.json_output, _handoff_path(app_ctx, summary.run_id))
    if not summary.ok:
        raise typer.Exit(code=1)


@app.command()
@_handle_errors
def resume(ctx: typer.Context, run_id: Annotated[str, typer.Argument()]) -> None:
    """Resume a run that was interrupted."""
    state: CliState = ctx.obj
    app_ctx = _load_ctx(state)
    resolved = app_ctx.require_profile()
    tracer = _build_tracer(state, offline=resolved.profile.offline)
    scheduler = make_scheduler(app_ctx, "*", tracer=tracer)
    if isinstance(scheduler.checks, ProfileCheckRunner):
        scheduler.checks.run_id = run_id
    try:
        summary = asyncio.run(scheduler.resume(run_id))
    finally:
        tracer.flush()
    _print_summary(summary, state.json_output, _handoff_path(app_ctx, summary.run_id))
    if not summary.ok:
        raise typer.Exit(code=1)


@app.command()
@_handle_errors
def rewind(ctx: typer.Context, instance_id: Annotated[str, typer.Argument()]) -> None:
    """Delete a rule instance's record (and everything downstream), so it rebuilds."""
    state: CliState = ctx.obj
    app_ctx = _load_ctx(state)
    app_ctx.require_profile()
    scheduler = make_scheduler(app_ctx, "*")
    ids = sorted(scheduler.rewind(instance_id))
    if state.json_output:
        typer.echo(json.dumps(ids))
    else:
        for iid in ids:
            typer.echo(f"rewound {iid}")


# --- status --------------------------------------------------------------------------


def _latest_run_id(layout: StateLayout) -> str | None:
    if not layout.runs_dir.is_dir():
        return None
    ids = sorted(p.name for p in layout.runs_dir.iterdir() if p.is_dir())
    return ids[-1] if ids else None


@app.command()
@_handle_errors
def status(ctx: typer.Context, run_id: Annotated[str | None, typer.Argument()] = None) -> None:
    """Show the status of the latest run, or a given one."""
    state: CliState = ctx.obj
    app_ctx = _load_ctx(state)
    layout = app_ctx.layout
    rid = run_id or _latest_run_id(layout)
    if rid is None:
        typer.echo("no runs found")
        return

    journal_read = journal_mod.read(layout.journal(rid))
    run_state = journal_mod.replay(journal_read.events)
    gate_of: dict[str, str] = {}
    for event in journal_read.events:
        if event.type == "gate_wait" and event.rule_instance is not None:
            gate_of[event.rule_instance] = str(event.payload.get("gate", ""))
    waiting = {
        iid: gate_of.get(iid, "") for iid, st in run_state.rules.items() if st == "waiting_gate"
    }
    counts = Counter(run_state.rules.values())

    if state.json_output:
        typer.echo(
            json.dumps(
                {
                    "run_id": rid,
                    "started": run_state.started,
                    "stopped": run_state.stopped,
                    "rules": run_state.rules,
                    "counts": dict(counts),
                    "waiting_gates": waiting,
                },
                indent=2,
            )
        )
        return

    typer.echo(f"run {rid}  started={run_state.started}  stopped={run_state.stopped}")
    for iid, st in sorted(run_state.rules.items()):
        typer.echo(f"  {st:13} {iid}")
    typer.echo("counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if waiting:
        typer.echo("waiting on gates:")
        for iid, gate_id in sorted(waiting.items()):
            typer.echo(f"  {iid} -> {gate_id}")


# --- approve -------------------------------------------------------------------------


@app.command()
@_handle_errors
def approve(
    ctx: typer.Context,
    gate_id: Annotated[str, typer.Argument()],
    instance: Annotated[str, typer.Option("--instance", help="The rule instance id.")],
    reject: Annotated[bool, typer.Option("--reject", help="Reject instead of approve.")] = False,
    by: Annotated[str | None, typer.Option("--by")] = None,
    note: Annotated[str, typer.Option("--note")] = "",
) -> None:
    """Record an approve/reject decision for a gate over a rule instance's inputs."""
    state: CliState = ctx.obj
    app_ctx = _load_ctx(state)
    app_ctx.require_profile()
    scheduler = make_scheduler(app_ctx, "*")
    rule_instance = scheduler.graph.instances.get(instance)
    if rule_instance is None:
        raise AppError(
            f"no such rule instance {instance!r}; known instances: "
            f"{sorted(scheduler.graph.instances)}"
        )
    decision: Literal["approve", "reject"] = "reject" if reject else "approve"
    who = by or default_identity()
    try:
        approval = gate_mod.approve(
            app_ctx.review,
            app_ctx.store,
            gate_id,
            rule_instance,
            by=who,
            decision=decision,
            note=note,
        )
    except gate_mod.GateError as exc:
        raise AppError(str(exc)) from exc
    if state.json_output:
        typer.echo(approval.model_dump_json())
    else:
        typer.echo(f"{decision} recorded for gate {gate_id!r} by {who!r}")


# --- baseline ------------------------------------------------------------------------


def _baseline_plan_json(plan: object) -> dict[str, object]:
    from chipgraph.core.engine.baseline import BaselineArtifact, BaselineGate, BaselinePlan

    assert isinstance(plan, BaselinePlan)

    def _artifact(a: BaselineArtifact) -> dict[str, object]:
        commit = (
            {
                "short_sha": a.last_commit.short_sha,
                "date": a.last_commit.date,
                "subject": a.last_commit.subject,
                "is_merge": a.last_commit.is_merge,
            }
            if a.last_commit is not None
            else None
        )
        return {
            "path": a.path,
            "sha256": a.sha256,
            "short_sha256": a.short_sha256,
            "status": a.status,
            "baselineable": a.baselineable,
            "last_commit": commit,
        }

    def _gate(g: BaselineGate) -> dict[str, object]:
        return {
            "gate_id": g.gate_id,
            "instance_id": g.instance_id,
            "already_decided": g.already_decided,
            "will_baseline": g.will_baseline,
            "artifacts": [_artifact(a) for a in g.artifacts],
        }

    return {
        "gates": [_gate(g) for g in plan.gates],
        "gates_to_baseline": [g.gate_id for g in plan.gates_to_baseline],
        "skipped_gates": [g.gate_id for g in plan.skipped_gates],
        "dirty_artifacts": [_artifact(a) for a in plan.dirty_artifacts],
        "open_findings": plan.open_findings,
    }


def _print_baseline_text(plan: object) -> None:
    from chipgraph.core.engine.baseline import BaselinePlan

    assert isinstance(plan, BaselinePlan)
    if not plan.gates:
        typer.echo("no gates found: nothing to baseline")
    for gate in plan.gates:
        state = "decided (skipped)" if gate.already_decided else "waiting"
        typer.echo(f"gate {gate.gate_id}  [{gate.instance_id}]  {state}")
        for artifact in gate.artifacts:
            commit = artifact.last_commit
            where = (
                f"{commit.short_sha} {commit.date} {commit.subject}"
                if commit is not None
                else "(no commit)"
            )
            typer.echo(f"    {artifact.status:9} {artifact.short_sha256}  {artifact.path}  {where}")
    if plan.dirty_artifacts:
        typer.echo("not baselined (modified or untracked in the working tree):")
        for artifact in plan.dirty_artifacts:
            typer.echo(f"    {artifact.status:9} {artifact.path}")
    typer.echo(f"open findings: {plan.open_findings}")
    to_do = plan.gates_to_baseline
    if to_do:
        typer.echo(f"would baseline {len(to_do)} gate(s): {', '.join(g.gate_id for g in to_do)}")
    else:
        typer.echo("would baseline 0 gates")


@app.command("baseline")
@_handle_errors
def baseline_cmd(
    ctx: typer.Context,
    confirm: Annotated[
        bool, typer.Option("--confirm", help="Record the baseline decisions (default: dry run).")
    ] = False,
    by: Annotated[str | None, typer.Option("--by")] = None,
    note: Annotated[str, typer.Option("--note")] = "",
    allow_branch: Annotated[
        bool,
        typer.Option("--allow-branch", help="Allow running on a branch other than main."),
    ] = False,
) -> None:
    """List the artifacts on the main branch and, with --confirm, baseline them (DESIGN 6.4)."""
    state: CliState = ctx.obj
    app_ctx = _load_ctx(state)
    app_ctx.require_profile()

    branch = current_branch(app_ctx.root)
    main = main_branch(app_ctx.root)
    if not allow_branch and branch is not None and branch != main:
        typer.echo(
            f"refusing to baseline on branch {branch!r}: not the main branch {main!r} "
            "(pass --allow-branch to override)",
            err=True,
        )
        raise typer.Exit(code=1)

    plan, refs_by_path = plan_project_baseline(app_ctx)

    if not confirm:
        if state.json_output:
            typer.echo(json.dumps(_baseline_plan_json(plan), indent=2))
        else:
            _print_baseline_text(plan)
        return

    who = by or default_identity()
    outcome = run_baseline(app_ctx, plan, refs_by_path, by=who, note=note)
    if state.json_output:
        typer.echo(
            json.dumps(
                {
                    "recorded_gates": [a.gate_id for a in outcome.approvals],
                    "findings_baselined": list(outcome.findings_baselined),
                },
                indent=2,
            )
        )
    else:
        if outcome.approvals:
            typer.echo(f"recorded baseline for {len(outcome.approvals)} gate(s):")
            for approval in outcome.approvals:
                typer.echo(f"    {approval.gate_id}")
        else:
            typer.echo("recorded baseline for 0 gates (nothing new)")
        if outcome.findings_baselined:
            typer.echo(f"baselined {len(outcome.findings_baselined)} open finding(s)")


# --- doctor ----------------------------------------------------------------------


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".chipgraph-doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        return False
    return True


def _first_word(cmd: object) -> str | None:
    if isinstance(cmd, str):
        parts = cmd.split()
        return parts[0] if parts else None
    if isinstance(cmd, list) and cmd:
        return str(cmd[0])
    return None


@app.command()
@_handle_errors
def doctor(ctx: typer.Context) -> None:
    """Report whether the environment is set up to run chipgraph."""
    state: CliState = ctx.obj
    lines: list[tuple[str, bool, str]] = []

    py_ok = sys.version_info >= (3, 13)
    lines.append(("python", py_ok, sys.version.split()[0]))

    git_path = shutil.which("git")
    lines.append(("git", git_path is not None, git_path or "not found on PATH"))

    app_ctx: AppContext | None
    try:
        app_ctx = _load_ctx(state)
    except (AppError, ConfigError) as exc:
        app_ctx = None
        lines.append(("profile", False, str(exc)))
    else:
        if app_ctx.profile is not None:
            lines.append(("profile", True, str(app_ctx.root / ".chipgraph.yml")))
        else:
            lines.append(("profile", False, "no .chipgraph.yml found"))

    if app_ctx is not None and app_ctx.profile is not None:
        state_dir = app_ctx.layout.state_dir
        lines.append(("state dir writable", _writable(state_dir), str(state_dir)))
        for check_id, cfg in sorted(app_ctx.profile.adapters.items()):
            if cfg.use != "cmd":
                continue
            first = _first_word(cfg.model_dump().get("cmd"))
            found = shutil.which(first) if first else None
            detail = f"{first!r} -> {found}" if first else "no 'cmd' configured"
            lines.append((f"tool for {check_id}", found is not None, detail))

    ok = all(passed for _, passed, _ in lines)
    if state.json_output:
        typer.echo(json.dumps([{"check": n, "ok": p, "detail": d} for n, p, d in lines], indent=2))
    else:
        for name, passed, detail in lines:
            mark = "ok" if passed else "MISSING"
            typer.echo(f"{mark:7} {name}: {detail}")
    if not ok:
        raise typer.Exit(code=1)


# --- findings / waive ---------------------------------------------------------------

_FINDING_STATUS_CHOICES = ("open", "waived", "fixed", "all")


def _finding_first_evidence(finding: Finding) -> str:
    ev = finding.evidence[0]
    if ev.file is not None:
        return f"{ev.file}:{ev.line}" if ev.line is not None else ev.file
    return f"model:{ev.model_key}"


def _findings_with_effective_status(
    app_ctx: AppContext, *, status: str, layer: int | None
) -> list[Finding]:
    """Every stored finding matching `layer`, with `.status` set to its *effective*
    status (resolving waiver currency against the finding's current artifact hashes),
    filtered by `status` unless it is `"all"`.
    """
    store = FindingStore(app_ctx.layout)
    rows: list[Finding] = []
    for finding in store.list(layer=layer):
        waivers = app_ctx.review.approvals(waiver_gate_id(finding.id))
        current_hashes = app_ctx.store.current_hashes(finding.artifacts)
        eff = effective_status(finding, waivers, current_hashes)
        if status != "all" and eff != status:
            continue
        display = finding if eff == finding.status else finding.model_copy(update={"status": eff})
        rows.append(display)
    return rows


@app.command("findings")
@_handle_errors
def findings_cmd(
    ctx: typer.Context,
    status: Annotated[
        str, typer.Option("--status", help="open|waived|fixed|all (default: open).")
    ] = "open",
    layer: Annotated[
        int | None, typer.Option("--layer", min=1, max=5, help="Only this DESIGN.md layer.")
    ] = None,
) -> None:
    """List recorded findings, with their effective status (DESIGN.md 4.8)."""
    state: CliState = ctx.obj
    if status not in _FINDING_STATUS_CHOICES:
        raise AppError(
            f"--status must be one of {'|'.join(_FINDING_STATUS_CHOICES)}, got {status!r}"
        )
    app_ctx = _load_ctx(state)
    rows = _findings_with_effective_status(app_ctx, status=status, layer=layer)

    if state.json_output:
        typer.echo(json.dumps([f.model_dump(mode="json") for f in rows], indent=2))
        return
    if not rows:
        typer.echo("no findings")
        return
    for finding in rows:
        typer.echo(
            f"{finding.id}  L{finding.layer}  {finding.severity:9}{finding.source:16}"
            f"{_finding_first_evidence(finding):40}{finding.status:7}{finding.claim}"
        )


@app.command("waive")
@_handle_errors
def waive_cmd(
    ctx: typer.Context,
    finding_id: Annotated[str, typer.Argument(help="A finding id, e.g. 'F-1a2b3c4d'.")],
    reason: Annotated[str, typer.Option("--reason", help="Why this finding is waived.")],
    by: Annotated[str | None, typer.Option("--by")] = None,
) -> None:
    """Waive a finding, binding the waiver to its artifacts' current hashes."""
    state: CliState = ctx.obj
    app_ctx = _load_ctx(state)
    store = FindingStore(app_ctx.layout)
    finding = store.get_by_id(finding_id)
    if finding is None:
        typer.echo(f"no such finding: {finding_id!r}", err=True)
        raise typer.Exit(code=2)

    who = by or default_identity()
    current_hashes = app_ctx.store.current_hashes(finding.artifacts)
    try:
        waived = findings_app.waive(
            finding,
            by=who,
            reason=reason,
            store=store,
            review=app_ctx.review,
            current_hashes=current_hashes,
        )
    except ValueError as exc:
        raise AppError(str(exc)) from exc

    if state.json_output:
        typer.echo(waived.model_dump_json())
    else:
        typer.echo(f"waived {waived.id} by {who!r}: {reason}")


# --- mcp ---------------------------------------------------------------------------


@app.command("mcp")
@_handle_errors
def mcp_cmd(ctx: typer.Context) -> None:
    """Start the MCP server over stdio, for Claude Code or another MCP client."""
    from chipgraph.mcp import run_stdio

    state: CliState = ctx.obj
    asyncio.run(run_stdio(state.start, profile_path=state.profile_path))


__all__ = ["app"]
