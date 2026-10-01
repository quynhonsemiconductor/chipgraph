"""`run_suite`: one suite, one runtime, graded, with its Inspect log and summary."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import report as report_mod
from .project import copy_tinysoc, dev_plugin
from .runtimes import (
    RUNTIMES,
    ClaudeCodeRuntime,
    FakeAskRuntime,
    FakeTriageRuntime,
    Runtime,
    RuntimeName,
    Spend,
    claude_auth,
    find_claude,
)
from .suites import SUITES, Suite
from .tasks import RUN_KEY, suite_task

DEFAULT_BUDGET_USD = 3.0
DEFAULT_TIME_LIMIT_S = 3600
DEFAULT_MAIN_MODEL = "haiku"
DEFAULT_SMALL_MODEL = "haiku"
DEFAULT_LARGE_MODEL = "opus"


class EvalError(ValueError):
    """A run that cannot start: an unknown suite, runtime or id, or no `claude`."""


@dataclass(frozen=True)
class EvalOptions:
    """How to run a suite (the `chipgraph eval` options)."""

    runtime: RuntimeName = "fake"
    main_model: str = DEFAULT_MAIN_MODEL
    small_model: str = DEFAULT_SMALL_MODEL
    large_model: str = DEFAULT_LARGE_MODEL
    budget_usd: float = DEFAULT_BUDGET_USD
    time_limit_s: float = DEFAULT_TIME_LIMIT_S
    only: tuple[str, ...] = ()
    fake_overrides: Mapping[str, Any] = field(default_factory=dict)
    """`fake` runtime only: scripted replies by id (an ask answer object, a triage label)
    instead of the correct ones, so a test can make the run fail on purpose."""


@dataclass(frozen=True)
class SuiteResult:
    summary: dict[str, Any]
    out: Path

    @property
    def status(self) -> str:
        return str(self.summary["status"])

    @property
    def exit_code(self) -> int:
        """0 pass (or no data), 1 fail."""
        return 1 if self.status == "fail" else 0


def _versions() -> dict[str, str]:
    import inspect_ai

    from chipgraph import __version__

    return {"inspect_ai": str(inspect_ai.__version__), "chipgraph": __version__}


def _select(suite: Suite, only: tuple[str, ...]) -> list[Any]:
    items = suite.items()
    if not only:
        return items
    known = {item.id for item in items}
    unknown = [i for i in only if i not in known]
    if unknown:
        raise EvalError(f"no such id in {suite.name}: {', '.join(unknown)}")
    return [item for item in items if item.id in only]


def _write(out: Path, summary: dict[str, Any]) -> None:
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (out / "summary.md").write_text(report_mod.markdown(summary), encoding="utf-8")


def _claude_version(claude: str, env: dict[str, str]) -> str:
    try:
        proc = subprocess.run(
            [claude, "--version"], capture_output=True, text=True, timeout=60, env=env
        )
    except (OSError, subprocess.SubprocessError):
        return "?"
    return proc.stdout.strip() or "?"


def run_suite(
    suite: str | Suite,
    out: Path,
    options: EvalOptions | None = None,
    *,
    progress: Callable[[str], None] | None = None,
) -> SuiteResult:
    """Run `suite` with `options.runtime`, grade it, and write its report to `out`.

    A suite whose data file does not exist yet is reported as `no data` (exit 0).
    """
    from inspect_ai import eval as inspect_eval

    options = options or EvalOptions()
    if isinstance(suite, str):
        if suite not in SUITES:
            raise EvalError(f"unknown suite {suite!r} (known: {', '.join(SUITES)})")
        suite = SUITES[suite]
    if options.runtime not in RUNTIMES:
        raise EvalError(f"unknown runtime {options.runtime!r} (known: {', '.join(RUNTIMES)})")
    out.mkdir(parents=True, exist_ok=True)
    info: dict[str, Any] = {"runtime": options.runtime, **_versions()}
    if not suite.has_data():
        summary = report_mod.no_data_summary(suite, info)
        _write(out, summary)
        return SuiteResult(summary, out)
    items = _select(suite, options.only)
    environ = dict(os.environ)

    claude: str | None = None
    env: dict[str, str] = {}
    if options.runtime == "claude-code":
        claude = find_claude(environ)
        if claude is None:
            raise EvalError("runtime claude-code needs the `claude` CLI on PATH")
        env, auth = claude_auth(environ)
        models = {"main": options.main_model}
        models.update(
            {"asker": "haiku"}
            if suite.kind == "ask"
            else {"small": options.small_model, "large": options.large_model}
        )
        info.update(
            models=models,
            auth=auth,
            claude_code=_claude_version(claude, env),
            cost_usd={"budget": options.budget_usd},
            time_limit_s=options.time_limit_s,
        )
    else:
        info["models"] = {"llm": "fake (scripted)"}

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"cg-eval-{suite.name}-") as tmp:
        work = Path(tmp)
        tiers = None
        if options.runtime == "claude-code" and suite.kind == "triage":
            tiers = {"small": options.small_model, "large": options.large_model}
        project = copy_tinysoc(work / "tinysoc", suite, tiers=tiers)
        runtime: Runtime
        if options.runtime == "fake":
            if suite.kind == "ask":
                runtime = FakeAskRuntime(project, options.fake_overrides)
            else:
                runtime = FakeTriageRuntime(project, suite, options.fake_overrides)
        else:
            assert claude is not None
            streams_dir = out / "streams"
            streams_dir.mkdir(exist_ok=True)
            runtime = ClaudeCodeRuntime(
                suite,
                project_dir=project,
                plugin_dir=dev_plugin(work / "plugin"),
                streams_dir=streams_dir,
                main_model=options.main_model,
                spend=Spend(options.budget_usd, options.time_limit_s),
                claude=claude,
                env=env,
                progress=progress,
            )
        task = suite_task(suite, items, runtime, info)
        [log] = inspect_eval(
            task,
            model="none/none",
            display="none",
            log_dir=str(out / "logs"),
            log_format="json",
            max_samples=1,
            fail_on_error=False,
        )
    info["wall_s"] = round(time.monotonic() - started, 1)

    runs: dict[str, dict[str, Any]] = {}
    for sample in log.samples or []:
        run = dict((sample.metadata or {}).get(RUN_KEY) or {})
        if sample.error is not None:
            run.setdefault("answer", None)
            run["note"] = f"error: {sample.error.message}"
        runs[str(sample.id)] = run
    answers = {sid: r["answer"] for sid, r in runs.items() if r.get("answer") is not None}
    (out / "answers.jsonl").write_text(
        "".join(json.dumps(a) + "\n" for a in answers.values()), encoding="utf-8"
    )
    grade_report = suite.grader.grade(items, answers)
    metrics = log.results.scores[0].metrics if log.results and log.results.scores else {}
    info["inspect"] = {
        "status": log.status,
        "log": os.path.relpath(log.location, out) if log.location else None,
        "accuracy": metrics["accuracy"].value if "accuracy" in metrics else None,
    }
    summary = report_mod.build_summary(suite, report=grade_report, runs=runs, info=info)
    _write(out, summary)
    return SuiteResult(summary, out)


__all__ = [
    "DEFAULT_BUDGET_USD",
    "DEFAULT_MAIN_MODEL",
    "DEFAULT_TIME_LIMIT_S",
    "EvalError",
    "EvalOptions",
    "SuiteResult",
    "run_suite",
]
