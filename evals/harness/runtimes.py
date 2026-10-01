"""The runtimes a suite's samples are answered by: `fake` (CI) and `claude-code` (real)."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from . import streams
from .suites import Suite

RuntimeName = Literal["fake", "claude-code"]
RUNTIMES: tuple[RuntimeName, ...] = ("fake", "claude-code")

NotRun = Literal["budget", "time"]


@dataclass
class SampleRun:
    """What one sample's run produced: the answer for the grader (None: no answer)."""

    answer: dict[str, Any] | None
    cost_usd: float = 0.0
    not_run: NotRun | None = None
    note: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class Runtime(Protocol):
    name: RuntimeName

    async def run(self, item: Any) -> SampleRun: ...


# --- fake ---------------------------------------------------------------------------


class FakeAskRuntime:
    """Each question through the real `/ask` answerer, over a scripted fake LLM.

    Retrieval, `ask_check` and its retry run for real on the ingested tinysoc copy; the
    fake model replies with `overrides[id]` (an `{"answer", "citations", "unknown"}`
    object) or, by default, the question's reference answer citing its first expected
    citation (or "I don't know" for a question tinysoc cannot answer).
    """

    name: RuntimeName = "fake"

    def __init__(self, project_dir: Path, overrides: Mapping[str, dict[str, Any]]) -> None:
        from chipgraph.app.context import AppContext
        from chipgraph.packs.assist.ask._project import AskProject

        ctx = AppContext.load(project_dir)
        self._data = ctx.require_profile().profile.data
        self._project = AskProject.load(ctx)
        self._overrides = overrides

    @staticmethod
    def scripted(item: Any) -> dict[str, Any]:
        if item.unknown:
            return {"answer": "I don't know", "citations": [], "unknown": True}
        return {"answer": item.reference, "citations": [item.expected[0]], "unknown": False}

    async def run(self, item: Any) -> SampleRun:
        from chipgraph.adapters.llm import FakeProvider, GuardedProvider
        from chipgraph.packs.assist.ask import answer_question

        reply = json.dumps(self._overrides.get(item.id) or self.scripted(item))
        provider = GuardedProvider(FakeProvider(lambda _: reply, name="fake"), data=self._data)
        result = await answer_question(self._project, item.question, provider, model="fake")
        assert result.answer is not None
        answer = {
            "id": item.id,
            "answer": result.answer.answer,
            "citations": list(result.answer.citations),
            "unknown": result.answer.unknown,
            "checked": result.status in ("answered", "unknown"),
        }
        details = {"status": result.status, "attempts": result.attempts}
        return SampleRun(answer, note=f"ask {result.status}", details=details)


class FakeTriageRuntime:
    """Each log through the real triage engine: the rules, then `decide()`'s model tiers
    over a fake LLM that answers `overrides[id]` or, by default, the sample's label."""

    name: RuntimeName = "fake"
    confidence = 0.95

    def __init__(self, project_dir: Path, suite: Suite, overrides: Mapping[str, str]) -> None:
        from chipgraph.app.context import AppContext

        self._ctx = AppContext.load(project_dir)
        self._suite = suite
        self._overrides = overrides

    async def run(self, item: Any) -> SampleRun:
        from chipgraph.adapters.llm import FakeProvider
        from chipgraph.adapters.llm.decide_backend import LlmDecideBackend
        from chipgraph.core.config.models import ModelsCfg
        from chipgraph.packs.assist.triage import triage_log

        value = self._overrides.get(item.id, item.label)
        reply = json.dumps(
            {"value": value, "confidence": self.confidence, "reason": "fake: scripted label"}
        )
        profile = self._ctx.require_profile().profile
        backend = LlmDecideBackend(
            FakeProvider(lambda _: reply, name="fake"),
            ModelsCfg(tiers={"small": "fake-small", "large": "fake-large"}),
            data=profile.data,
        )
        text = self._suite.log_path(item.id).read_text(encoding="utf-8")
        report = await triage_log(
            self._ctx,
            text,
            source=f"logs/{item.id}.log",
            check_id=self._suite.check_id(item.id),
            backend=backend,
        )
        answer = {
            "id": item.id,
            "label": report.label,
            "backend": report.backend,
            "status": report.status,
            "rule": report.rule,
            "confidence": report.confidence,
            "low_confidence": report.low_confidence,
            "model": report.model,
        }
        return SampleRun(answer, note=f"triage {report.status} by {report.backend}")


# --- claude-code ----------------------------------------------------------------------

CLAUDE_ENV = "CHIPGRAPH_EVAL_CLAUDE"
"""Overrides the `claude` executable (the tests point it at a scripted stand-in)."""

TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
"""A Claude Code OAuth token (`claude setup-token`): used when set (CI)."""

API_KEY_OPT_IN = "CHIPGRAPH_EVAL_USE_API_KEY"
"""Set to 1 to let `claude` use `ANTHROPIC_API_KEY`; otherwise it is never passed on."""

DROPPED_ENV = (
    "VIRTUAL_ENV",
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_METRICS_EXPORTER",
    "OTEL_LOGS_EXPORTER",
)

SAMPLE_TIMEOUT_S = 900
MAX_TURNS = {"ask": 12, "triage": 30}

ASK_TOOLS = (
    "mcp__plugin_chipgraph_chipgraph__ask_context",
    "mcp__plugin_chipgraph_chipgraph__ask_check",
    "Agent",
)
TRIAGE_TOOLS = (
    "mcp__plugin_chipgraph_chipgraph__triage",
    "mcp__plugin_chipgraph_chipgraph__pending_decisions",
    "mcp__plugin_chipgraph_chipgraph__answer_decision",
    "Agent",
)
TRIAGE_DENIED = (
    "Bash",
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Glob",
    "Grep",
    "Skill",
    "WebFetch",
    "WebSearch",
)
"""Triage needs none of these; denying them keeps other installed plugins out of the run."""


def claude_auth(environ: Mapping[str, str]) -> tuple[dict[str, str], str]:
    """The environment for `claude`, and which auth it uses (never the secret itself).

    - `CLAUDE_CODE_OAUTH_TOKEN` set: that token (`ANTHROPIC_API_KEY` is removed, so it
      cannot take precedence);
    - else `CHIPGRAPH_EVAL_USE_API_KEY=1` and `ANTHROPIC_API_KEY` set: the API key;
    - else the logged-in Claude Code account (`ANTHROPIC_API_KEY` removed, as in the
      acceptance scripts: a stray key never pays for a run).
    """
    env = {k: v for k, v in environ.items() if k not in DROPPED_ENV}
    if environ.get(TOKEN_ENV):
        env.pop("ANTHROPIC_API_KEY", None)
        return env, "oauth token (CLAUDE_CODE_OAUTH_TOKEN)"
    if environ.get(API_KEY_OPT_IN) == "1" and environ.get("ANTHROPIC_API_KEY"):
        return env, "api key (ANTHROPIC_API_KEY)"
    env.pop("ANTHROPIC_API_KEY", None)
    return env, "logged-in Claude Code account"


def find_claude(environ: Mapping[str, str]) -> str | None:
    return environ.get(CLAUDE_ENV) or shutil.which("claude")


@dataclass
class Spend:
    """The suite's running cost and clock: a sample starts only under both limits."""

    budget_usd: float
    time_limit_s: float
    spent_usd: float = 0.0
    started: float = field(default_factory=time.monotonic)

    @property
    def remaining_usd(self) -> float:
        return self.budget_usd - self.spent_usd

    @property
    def remaining_s(self) -> float:
        return self.time_limit_s - (time.monotonic() - self.started)

    def stop_reason(self) -> NotRun | None:
        if self.remaining_usd <= 0:
            return "budget"
        if self.remaining_s <= 0:
            return "time"
        return None


class ClaudeCodeRuntime:
    """One headless `claude -p "/chipgraph:<cmd> ..."` per sample, in the tinysoc copy.

    The main session runs on `main_model`; the asker subagent on its own tier (haiku,
    its frontmatter), the decider subagents on the profile tiers of the copy. Each run's
    stream is kept in `streams/<id>.jsonl`. `--max-budget-usd` (when this `claude` has it)
    is the suite budget left; once the summed `total_cost_usd` reaches the budget, or
    the suite's time is up, the remaining samples are not run.
    """

    name: RuntimeName = "claude-code"

    def __init__(
        self,
        suite: Suite,
        *,
        project_dir: Path,
        plugin_dir: Path,
        streams_dir: Path,
        main_model: str,
        spend: Spend,
        claude: str,
        env: dict[str, str],
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.suite = suite
        self.project_dir = project_dir
        self.plugin_dir = plugin_dir
        self.streams_dir = streams_dir
        self.main_model = main_model
        self.spend = spend
        self.claude = claude
        self.env = env
        self.progress = progress or (lambda _: None)
        self.flags = self._flags()
        self.foreign: list[str] = []

    def _flags(self) -> set[str]:
        """The optional flags this `claude` supports, from `claude --help` (no model call)."""
        try:
            help_text = subprocess.run(
                [self.claude, "--help"],
                capture_output=True,
                text=True,
                timeout=60,
                env=self.env,
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            help_text = ""
        return {f for f in ("--max-budget-usd",) if f in help_text}

    def command(self, item: Any) -> list[str]:
        kind = self.suite.kind
        prompt = f"/chipgraph:{kind} {self.suite.prompt(item)}"
        cmd = [
            self.claude,
            "-p",
            prompt,
            "--model",
            self.main_model,
            "--max-turns",
            str(MAX_TURNS[kind]),
            "--plugin-dir",
            str(self.plugin_dir),
            "--allowedTools",
            " ".join(ASK_TOOLS if kind == "ask" else TRIAGE_TOOLS),
        ]
        if kind == "triage":
            cmd += ["--disallowedTools", " ".join(TRIAGE_DENIED)]
        cmd += ["--output-format", "stream-json", "--verbose"]
        if "--max-budget-usd" in self.flags:
            cmd += ["--max-budget-usd", f"{max(self.spend.remaining_usd, 0.01):.2f}"]
        return cmd

    async def run(self, item: Any) -> SampleRun:
        stop = self.spend.stop_reason()
        if stop is not None:
            return SampleRun(None, not_run=stop, note=f"not run ({stop})")
        stream_path = self.streams_dir / f"{item.id}.jsonl"
        stderr_path = self.streams_dir / f"{item.id}.stderr"
        timeout = min(SAMPLE_TIMEOUT_S, self.spend.remaining_s)
        note = ""
        with stream_path.open("wb") as out, stderr_path.open("wb") as err:
            proc = await asyncio.create_subprocess_exec(
                *self.command(item),
                cwd=self.project_dir,
                env=self.env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=out,
                stderr=err,
            )
            try:
                code = await asyncio.wait_for(proc.wait(), timeout=timeout)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                code, note = None, f"timed out after {timeout:.0f} s"
        if code:
            note = f"claude exited {code}"
        stream = streams.events(stream_path)
        calls = streams.tool_calls(stream)
        cost = streams.cost_usd(stream)
        self.spend.spent_usd += cost
        result = streams.result_event(stream)
        if self.suite.kind == "ask":
            answer = streams.ask_answer(item.id, calls)
        else:
            answer = streams.triage_answer(item.id, calls)
        foreign = streams.foreign_calls(self.suite.kind, item.id, calls)
        self.foreign += foreign
        details = {
            "main_tools": [c["name"].rsplit("__", 1)[-1] for c in calls if c["parent"] is None],
            "agents": streams.agents(calls),
            "foreign": foreign,
            "subtype": result.get("subtype"),
            "turns": result.get("num_turns"),
            "output_tokens": streams.output_tokens(stream),
        }
        self.progress(
            f"{item.id}: cost ${cost:.4f}, spent ${self.spend.spent_usd:.4f} of "
            f"${self.spend.budget_usd:.2f}{f' ({note})' if note else ''}"
        )
        return SampleRun(answer, cost_usd=cost, note=note, details=details)


__all__ = [
    "RUNTIMES",
    "ClaudeCodeRuntime",
    "FakeAskRuntime",
    "FakeTriageRuntime",
    "Runtime",
    "RuntimeName",
    "SampleRun",
    "Spend",
    "claude_auth",
    "find_claude",
]
