"""The `review` suite's runtimes (task M2-09): one tinysoc copy per sample, its change
applied in the working tree, reviewed by the rule `digital-rtl/review` for its block.

- `FakeReviewRuntime`: the runtime claude-code MCP service in process, no model: the
  real `next_task` (rule loading, role checks, dispatch), the real `get_context` (diff,
  spec and model slices), a scripted Critic reply, the real `submit` (validation, the
  engine writing the report). The scripted reply points at the planted defect, with its
  class and REQ and the spec line as evidence (no findings for a clean sample);
  `overrides[id]` replaces fields of it, so a test can make the run fail on purpose.
- `ClaudeCodeReviewRuntime`: one headless `claude -p "/chipgraph:run <target>"` per
  sample: the main session on `--main-model`, the `chipgraph:critic` subagent on its
  tier (`large`, the profile's `models.tiers.large`).

Either way the answer is the review report the engine wrote, `reports/review/<block>.json`
(only an accepted review is written), in `evals/review/grade.py`'s format.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

from . import streams
from .runtimes import SAMPLE_TIMEOUT_S, RuntimeName, SampleRun, Spend
from .suites import Suite

PACK = "digital-rtl"
MAX_TURNS = 30
TOOL = "mcp__plugin_chipgraph_chipgraph__"
REVIEW_TOOLS = (
    f"{TOOL}next_task",
    f"{TOOL}get_context",
    f"{TOOL}submit",
    "Agent",
    "Read",
    "Glob",
    "Grep",
)
"""Pre-approved: the loop's tools, and the critic's own (its agent file grants them)."""

REVIEW_DENIED = (
    "Bash",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Skill",
    "WebFetch",
    "WebSearch",
)
"""Nobody in a review run writes a file or runs a shell: the engine writes the report."""

CRITIC_AGENT = "chipgraph:critic"
MAIN_ALLOWED = (f"{TOOL}next_task", f"{TOOL}submit", "Agent", "Task")
CRITIC_ALLOWED = (f"{TOOL}get_context", "Read", "Glob", "Grep")


def _git(project: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=eval@example.invalid", "-c", "user.name=eval", *args],
        cwd=project,
        check=True,
        capture_output=True,
    )


def enable_review(project: Path) -> Path:
    """Turn the `digital-rtl` pack on in a tinysoc copy and commit it (the review base)."""
    profile_path = project / ".chipgraph.yml"
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    packs = list(profile.get("packs") or [])
    if PACK not in packs:
        profile["packs"] = [*packs, PACK]
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", f"enable {PACK}")
    return project


def sample_project(base: Path, dest: Path, sample: Any, grader: Any) -> Path:
    """A copy of the base project (its git history too) with the sample's change applied."""
    shutil.copytree(base, dest, symlinks=True)
    grader.apply_edits(sample, dest)
    return dest


def report_path(project: Path, sample: Any) -> Path:
    return project / "reports" / "review" / f"{sample.block}.json"


def read_review(project: Path, sample: Any) -> dict[str, Any] | None:
    path = report_path(project, sample)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _spec_line(project: Path, ref: str) -> str:
    path, _, line = ref.rpartition(":")
    lines = (project / path).read_text(encoding="utf-8").splitlines()
    return lines[int(line) - 1].strip()


def scripted_review(sample: Any, review: Mapping[str, Any], project: Path) -> dict[str, Any]:
    """The reply a correct Critic gives: the planted defect, or no findings."""
    reply: dict[str, Any] = {
        "target": review["target"],
        "base": review["base"],
        "head": review["head"],
        "reviewed": sorted({*review["changed"], *([sample.at_file] if sample.at_file else [])}),
        "comments": [],
        "summary": "fake: scripted review",
        "verdict": "approve",
    }
    if not sample.clean:
        reply["comments"] = [
            {
                "id": "R1",
                "severity": "major",
                "category": sample.cls,
                "file": sample.at_file,
                "line": sample.at_line,
                "claim": sample.description,
                "evidence": f"{sample.spec} {_spec_line(project, sample.spec)}",
                "suggestion": "follow the spec",
                "req_id": sample.req,
            }
        ]
        reply["verdict"] = "changes_requested"
    return reply


class FakeReviewRuntime:
    """Each sample through the real MCP service functions, with a scripted Critic."""

    name: RuntimeName = "fake"

    def __init__(self, base: Path, work: Path, suite: Suite, overrides: Mapping[str, Any]) -> None:
        self.base = base
        self.work = work
        self.suite = suite
        self.overrides = overrides

    async def run(self, item: Any) -> SampleRun:
        from chipgraph.adapters.runtime.claude_code import service
        from chipgraph.app.context import AppContext

        project = sample_project(self.base, self.work / item.id, item, self.suite.grader)
        handed = await service.next_task(AppContext.load(project), item.target)
        [task] = handed["tasks"]
        context = await service.get_context(AppContext.load(project), task["task_id"])
        reply = scripted_review(item, context["review"], project)
        reply.update(self.overrides.get(item.id) or {})
        result = await service.submit(
            AppContext.load(project), task["task_id"], service.SubmitReport(review=reply)
        )
        answer = {
            "id": item.id,
            "review": read_review(project, item),
            "accepted": result["accepted"],
            "reasons": result["reasons"],
        }
        details = {
            "agent": task["agent"],
            "tier": task["tier"],
            "changed": context["review"]["changed"],
            "base_from": context["review"]["base_from"],
        }
        status = "accepted" if result["accepted"] else "rejected"
        return SampleRun(answer, note=f"review {status}", details=details)


def foreign_calls(sid: str, calls: list[dict[str, Any]]) -> list[str]:
    """Calls a review run must not make: main-session tools other than the loop's, critic
    tools other than its own, and any subagent other than the critic."""
    out = []
    for call in calls:
        allowed = MAIN_ALLOWED if call["parent"] is None else CRITIC_ALLOWED
        if call["name"] not in allowed:
            out.append(f"{sid}:{call['name']}")
    out += [f"{sid}:Agent({t})" for t, _ in streams.agents(calls) if t != CRITIC_AGENT]
    return out


class ClaudeCodeReviewRuntime:
    """One headless `claude -p "/chipgraph:run <target>"` per sample, in its own copy."""

    name: RuntimeName = "claude-code"

    def __init__(
        self,
        suite: Suite,
        *,
        base: Path,
        work: Path,
        plugin_dir: Path,
        streams_dir: Path,
        main_model: str,
        spend: Spend,
        claude: str,
        env: dict[str, str],
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.suite = suite
        self.base = base
        self.work = work
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
            " ".join(REVIEW_TOOLS),
            "--disallowedTools",
            " ".join(REVIEW_DENIED),
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
        project = sample_project(self.base, self.work / item.id, item, self.suite.grader)
        stream_path = self.streams_dir / f"{item.id}.jsonl"
        stderr_path = self.streams_dir / f"{item.id}.stderr"
        timeout = min(SAMPLE_TIMEOUT_S, self.spend.remaining_s)
        note = ""
        with stream_path.open("wb") as out, stderr_path.open("wb") as err:
            proc = await asyncio.create_subprocess_exec(
                *self.command(item),
                cwd=project,
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
        submits = [c for c in calls if c["parent"] is None and c["name"] == f"{TOOL}submit"]
        last = (submits[-1]["result"] or {}) if submits else {}
        answer = {
            "id": item.id,
            "review": read_review(project, item),
            "accepted": bool(last.get("accepted")),
            "reasons": list(last.get("reasons") or []),
            "submits": len(submits),
        }
        foreign = foreign_calls(item.id, calls)
        self.foreign += foreign
        result = streams.result_event(stream)
        details = {
            "main_tools": [c["name"].rsplit("__", 1)[-1] for c in calls if c["parent"] is None],
            "agents": streams.agents(calls),
            "critic_tools": [c["name"] for c in calls if c["parent"] is not None],
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
    "CRITIC_AGENT",
    "REVIEW_DENIED",
    "REVIEW_TOOLS",
    "ClaudeCodeReviewRuntime",
    "FakeReviewRuntime",
    "enable_review",
    "foreign_calls",
    "read_review",
    "sample_project",
    "scripted_review",
]
