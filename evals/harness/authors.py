"""The `rtl` and `tb` suites' runtimes (task M2-13): an author rule on one QSoC problem.

Each sample is one problem of `evals/rtl/problems.yml` in a fresh eval project of its
own (`evals/rtl/project.py`): for `rtl` the RTL Author's (`digital-rtl/rtl_module` for
the module, after its approved plan), for `tb` the testbench Author's
(`dv/tb_module` for the block, with no RTL in the project). The two never meet: each
author works without the other's work; only the grader runs the test on the oracle.

- `FakeAuthorRuntime`: the runtime claude-code MCP service in process, no model: the
  real `next_task`, `get_context` and `submit` (the rule's checks, the loop's labels,
  retries and budget), with a scripted author that writes, try by try, the problem's
  `fake.<phase>` script (`oracle`, `broken`, `syntax_error`; `reference`, `broken`,
  `unknown_port`), or `overrides[id]`. It counts no cost, and estimates the tokens an
  author would read and write from the text exchanged (`estimate`).
- `ClaudeCodeAuthorRuntime`: one headless `claude -p "/chipgraph:run <target>"` per
  problem: the main session on `--main-model` (default sonnet), the author subagent on
  the profile tiers (medium = the main model, large = `--large-model`); the loop is the
  plugin's (`next_task`, `get_context`, `submit`); cost and tokens per model come from
  the stream's `result` event.

Either way the answer is `evals/rtl/grade.py`'s: the written file, every counted try
(accepted, label, failed checks), the last checks, cost, usage and wall time.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import streams
from .review import tool_search_loads_only
from .runtimes import RuntimeName, SampleRun, Spend
from .suites import rtl_module

TOOL = "mcp__plugin_chipgraph_chipgraph__"
MAX_TURNS = 40
SAMPLE_TIMEOUT_S = 1500
MAX_ROUNDS = 8
AGENTS = {"rtl": "chipgraph:rtl-author", "tb": "chipgraph:tb-author"}
AUTHOR_TOOLS = (
    f"{TOOL}next_task",
    f"{TOOL}get_context",
    f"{TOOL}submit",
    "Agent",
    "Write",
    "Edit",
    "MultiEdit",
)
"""Pre-approved: the loop's tools and the author's (its agent file grants it these)."""
AUTHOR_DENIED = (
    "Bash",
    "Read",
    "Glob",
    "Grep",
    "NotebookEdit",
    "Skill",
    "WebFetch",
    "WebSearch",
)
"""No one reads a file or runs a shell: the engine gives the author everything it may see."""
MAIN_ALLOWED = (f"{TOOL}next_task", f"{TOOL}submit", "Agent", "Task")
AUTHOR_ALLOWED = (f"{TOOL}get_context", "Write", "Edit", "MultiEdit")
WRITES = ("Write", "Edit", "MultiEdit")

CHARS_PER_TOKEN = 4
"""The fake runtime's token estimate: characters exchanged / 4."""

UNKNOWN_PORT_TEST = '''

@cocotb.test()
async def test_cg_unknown_port(dut):
    """Drives a port the interface does not have (the fake author's mistake)."""
    dut.cg_no_such_port.value = 0
'''


def author_tiers(main_model: str, large_model: str) -> dict[str, str]:
    """The eval project's `models.tiers`: the author starts on `medium` (the main
    session's model, sonnet by default; Haiku only ever as a worker) and escalates to
    `large` after a rejection."""
    return {"medium": main_model, "large": large_model}


def missing_tools(phase: str) -> str:
    """What a run of the phase's suite needs and this machine lacks ('' when nothing):
    Verilator and make (lint, the differential simulation), and for `tb` the `sim` extra."""
    import importlib.util
    import shutil

    missing = [t for t in ("verilator", "make") if shutil.which(t) is None]
    if phase == "tb" and (
        importlib.util.find_spec("cocotb") is None or importlib.util.find_spec("edalize") is None
    ):
        missing.append("the `sim` extra (uv sync --extra sim)")
    return ", ".join(missing)


def scripted_text(problem: Any, phase: str, reply: str) -> str:
    """What a fake author writes for one scripted reply."""
    strip_header = rtl_module("fixtures").strip_header
    if phase == "rtl":
        oracle = strip_header(problem.fixture(problem.files.oracle))
        if reply == "oracle":
            return str(oracle)
        if reply == "broken":
            return str(problem.fake.broken_rtl.apply(oracle))
        if reply == "syntax_error":
            head, _, _ = oracle.rpartition("endmodule")
            return str(head)
    else:
        reference = problem.reference_path.read_text(encoding="utf-8")
        if reply == "reference":
            return str(reference)
        if reply == "broken":
            return str(problem.fake.broken_tb.apply(reference))
        if reply == "unknown_port":
            return str(reference) + UNKNOWN_PORT_TEST
    raise ValueError(f"no scripted {phase} reply {reply!r}")


def _attempt(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "accepted": bool(result.get("accepted")),
        "status": result.get("status"),
        "label": result.get("label"),
        "failed_checks": [
            c if isinstance(c, str) else str(c.get("check_id", c))
            for c in result.get("failed_checks") or []
        ],
    }


def _counted(result: Mapping[str, Any]) -> bool:
    """A submit that used a try (an accepted one, or a counted rejection)."""
    return bool(result.get("accepted")) or bool(result.get("counted", True))


class FakeAuthorRuntime:
    """Each problem in its own eval project, through the real MCP service functions."""

    name: RuntimeName = "fake"

    def __init__(self, phase: str, work: Path, overrides: Mapping[str, Sequence[str]]) -> None:
        self.phase = phase
        self.work = work
        self.overrides = overrides

    async def run(self, item: Any) -> SampleRun:
        from chipgraph.adapters.runtime.claude_code import service
        from chipgraph.app.context import AppContext

        project = rtl_module("project")
        problem = item.problem
        started = time.monotonic()
        root = await asyncio.to_thread(
            project.make_project, problem, self.phase, self.work / item.id
        )
        script = list(self.overrides.get(item.id) or getattr(problem.fake, self.phase))
        out = root / item.output
        attempts: list[dict[str, Any]] = []
        last: dict[str, Any] = {}
        estimate = {"rounds": 0, "author_input_tokens": 0, "author_output_tokens": 0}
        for _ in range(MAX_ROUNDS):
            handed = await service.next_task(AppContext.load(root), item.target)
            tasks = [*handed["tasks"], *handed["in_progress"]]
            if not tasks:
                break
            task = tasks[0]
            context = await service.get_context(AppContext.load(root), task["task_id"])
            reply = script[min(len(attempts), len(script) - 1)]
            text = scripted_text(problem, self.phase, reply)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            report = service.SubmitReport(files_written=(item.output,))
            last = await service.submit(AppContext.load(root), task["task_id"], report)
            estimate["rounds"] += 1
            estimate["author_input_tokens"] += len(json.dumps(context)) // CHARS_PER_TOKEN
            estimate["author_output_tokens"] += len(text) // CHARS_PER_TOKEN
            if _counted(last):
                attempts.append({**_attempt(last), "reply": reply})
            if last.get("status") != "rejected":
                break
        answer = {
            "id": item.id,
            "phase": self.phase,
            "status": last.get("status"),
            "accepted": bool(last.get("accepted")),
            "text": out.read_text(encoding="utf-8") if out.is_file() else None,
            "attempts": attempts,
            "checks": last.get("checks") or [],
            "tries": int(last.get("tries") or 0),
            "cost_usd": 0.0,
            "usage": {},
            "estimate": estimate,
            "wall_s": round(time.monotonic() - started, 1),
        }
        note = f"{self.phase} {answer['status']} after {len(attempts)} counted tries"
        return SampleRun(answer, note=note, details={"script": script})


def foreign_calls(
    phase: str, sid: str, calls: list[dict[str, Any]], project: Path, output: str
) -> list[str]:
    """Calls an author run must not make: main-session tools other than the loop's,
    author tools other than its own, a write to any file but the task's output, and any
    subagent other than the phase's author. A `ToolSearch` is ignored only when it loads
    allowed tools."""
    out: list[str] = []
    allowed_file = {output, str((project / output).resolve()), str(project / output)}
    for call in calls:
        allowed = MAIN_ALLOWED if call["parent"] is None else AUTHOR_ALLOWED
        name = call["name"]
        if name == "ToolSearch":
            if not tool_search_loads_only(call["input"], allowed):
                out.append(f"{sid}:ToolSearch({call['input'].get('query', '')})")
            continue
        if name not in allowed:
            out.append(f"{sid}:{name}")
            continue
        if name in WRITES:
            path = str(call["input"].get("file_path", ""))
            resolved = str(Path(path).resolve()) if Path(path).is_absolute() else path
            if path not in allowed_file and resolved not in allowed_file:
                out.append(f"{sid}:{name}({path})")
    out += [f"{sid}:Agent({t})" for t, _ in streams.agents(calls) if t != AGENTS[phase]]
    return out


class ClaudeCodeAuthorRuntime:
    """One headless `claude -p "/chipgraph:run <target>"` per problem, in its own project."""

    name: RuntimeName = "claude-code"

    def __init__(
        self,
        phase: str,
        *,
        work: Path,
        plugin_dir: Path,
        streams_dir: Path,
        main_model: str,
        tiers: dict[str, str],
        spend: Spend,
        claude: str,
        env: dict[str, str],
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.phase = phase
        self.work = work
        self.plugin_dir = plugin_dir
        self.streams_dir = streams_dir
        self.main_model = main_model
        self.tiers = tiers
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
        cmd = [
            self.claude,
            "-p",
            f"/chipgraph:run {item.target}",
            "--model",
            self.main_model,
            "--max-turns",
            str(MAX_TURNS),
            "--plugin-dir",
            str(self.plugin_dir),
            "--allowedTools",
            " ".join(AUTHOR_TOOLS),
            "--disallowedTools",
            " ".join(AUTHOR_DENIED),
            "--permission-mode",
            "acceptEdits",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if "--max-budget-usd" in self.flags:
            cmd += ["--max-budget-usd", f"{max(self.spend.remaining_usd, 0.01):.2f}"]
        return cmd

    async def run(self, item: Any) -> SampleRun:
        stop = self.spend.stop_reason()
        if stop is not None:
            return SampleRun(None, not_run=stop, note=f"not run ({stop})")
        project = rtl_module("project")
        started = time.monotonic()
        root = await asyncio.to_thread(
            project.make_project,
            item.problem,
            self.phase,
            self.work / item.id,
            runtime="claude-code",
            tiers=self.tiers,
        )
        stream_path = self.streams_dir / f"{item.id}.jsonl"
        stderr_path = self.streams_dir / f"{item.id}.stderr"
        timeout = min(SAMPLE_TIMEOUT_S, self.spend.remaining_s)
        note = ""
        with stream_path.open("wb") as out, stderr_path.open("wb") as err:
            proc = await asyncio.create_subprocess_exec(
                *self.command(item),
                cwd=root,
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
        submits = [
            c["result"] or {} for c in calls if c["parent"] is None and c["name"] == f"{TOOL}submit"
        ]
        last = submits[-1] if submits else {}
        out_file = root / item.output
        usage = _usage(result.get("modelUsage"))
        answer = {
            "id": item.id,
            "phase": self.phase,
            "status": last.get("status") or "no submit",
            "accepted": bool(last.get("accepted")),
            "text": out_file.read_text(encoding="utf-8") if out_file.is_file() else None,
            "attempts": [_attempt(s) for s in submits if _counted(s)],
            "checks": last.get("checks") or [],
            "tries": int(last.get("tries") or 0),
            "cost_usd": cost,
            "usage": usage,
            "wall_s": round(time.monotonic() - started, 1),
            "note": note,
        }
        foreign = foreign_calls(self.phase, item.id, calls, root, item.output)
        self.foreign += foreign
        details = {
            "main_tools": [c["name"].rsplit("__", 1)[-1] for c in calls if c["parent"] is None],
            "agents": streams.agents(calls),
            "author_tools": [c["name"] for c in calls if c["parent"] is not None],
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


def _usage(model_usage: Any) -> dict[str, dict[str, float]]:
    usage: dict[str, dict[str, float]] = rtl_module("metrics").usage_from_model_usage(model_usage)
    return usage


__all__ = [
    "AGENTS",
    "AUTHOR_DENIED",
    "AUTHOR_TOOLS",
    "ClaudeCodeAuthorRuntime",
    "FakeAuthorRuntime",
    "foreign_calls",
    "scripted_text",
]
