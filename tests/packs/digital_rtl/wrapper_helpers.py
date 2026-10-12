"""Shared by the M2-12 tests: a tinysoc copy with the `digital-rtl` pack and the sample
vendor IP's wrapper, emacs detection, a scripted wrapper author and a canned runner.

Not a test module itself; imported by the `test_wrapper_*.py` modules. Everything that
needs a real emacs with verilog-mode is marked `needs_emacs` and skips cleanly without.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from chipgraph.adapters.runner.local import LocalRunner
from chipgraph.adapters.runtime.claude_code import service
from chipgraph.app.context import AppContext
from chipgraph.core.contracts import AgentResult
from chipgraph.core.plugin_api.types import AgentTask, RunResult, ToolContext

REPO = Path(__file__).resolve().parents[3]
EXAMPLE = REPO / "examples" / "tinysoc"
WRAPPER = "tiny_fifo_wrap"
BLOCK = "top"
SRC = f"rtl/emacs/{WRAPPER}.src.sv"
OUT = f"rtl/{WRAPPER}.sv"
IP_FILE = "vendor/vendor_fifo/vendor_fifo.sv"
AUTHOR = f"digital-rtl/wrapper[block={BLOCK},wrapper={WRAPPER}]"
GEN = f"digital-rtl/wrapper_gen[block={BLOCK},wrapper={WRAPPER}]"
PARAMS = {"block": BLOCK, "wrapper": WRAPPER}


def _has_verilog_mode() -> bool:
    if shutil.which("emacs") is None:
        return False
    probe = subprocess.run(
        ["emacs", "-Q", "--batch", "--eval", "(require 'verilog-mode)"],
        capture_output=True,
        check=False,
        timeout=120,
    )
    return probe.returncode == 0


HAS_EMACS = _has_verilog_mode()
HAS_VERILATOR = shutil.which("verilator") is not None and shutil.which("make") is not None
needs_emacs = pytest.mark.skipif(not HAS_EMACS, reason="needs emacs with verilog-mode")
needs_verilator = pytest.mark.skipif(not HAS_VERILATOR, reason="needs verilator and make")
PASS = {"use": "cmd", "cmd": ["python3", "-c", "pass"]}
"""A check adapter that always passes."""


def source_text() -> str:
    """The sample wrapper's source, as tinysoc commits it."""
    return (EXAMPLE / SRC).read_text(encoding="utf-8")


def generated_text() -> str:
    """The sample wrapper's generated file, as tinysoc commits it."""
    return (EXAMPLE / OUT).read_text(encoding="utf-8")


# --- sources a model might write ---------------------------------------------------------

#: The template of the sample wrapper, as a model would write it (regexps), with the module
#: named by the project's naming rules.
GOOD_TEMPLATE = """\
  /* vendor_fifo AUTO_TEMPLATE (
      .rst_ni     (i_rst_n),
      .testmode_i (1'b0),
      .err_o      (unused_err),
      .\\(.*\\)_i   (i_\\1[]),
      .\\(.*\\)_o   (o_\\1[]),
  ); */
"""


def model_style_source(template: str = GOOD_TEMPLATE) -> str:
    """A `.src.sv` as the author writes it: port groups, template, instance, no expansion."""
    return f"""\
// SPDX-License-Identifier: Apache-2.0
// tiny_fifo_wrap: wrapper of the vendored FIFO with tinysoc's port names.
module {WRAPPER} (
  /*AUTOINPUT("^i_clk\\|^i_rst\\|^i_clr\\|^i_push\\|^i_data\\|^i_pop")*/
  /*AUTOOUTPUT("^o_full\\|^o_data\\|^o_empty")*/
  /*AUTOINPUT*/
  /*AUTOOUTPUT*/
);

  /*AUTOWIRE*/

  /*AUTO_LISP(setq verilog-auto-output-ignore-regexp "^unused_")*/

{template}  vendor_fifo #(
    .Width (8),
    .Depth (4)
  ) u_fifo (/*AUTOINST*/);

endmodule
"""


UNMAPPED_OUTPUTS = GOOD_TEMPLATE.replace("      .\\(.*\\)_o   (o_\\1[]),\n", "")
"""The template without the `*_o` rule: `full_o`, `data_o` and `empty_o` map to nothing."""


# --- a project ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def make_project(
    dest: Path,
    *,
    source: bool = True,
    generated: bool = True,
    runtime: str = "generic",
    ingest: bool = False,
    commit: bool = True,
) -> Path:
    """`examples/tinysoc` at `dest` with the `digital-rtl` pack: the sample wrapper's
    source and generated file are kept or removed; a new git repo (branch main).

    Without Verilator the project's `lint` always passes.
    """
    shutil.copytree(EXAMPLE, dest, ignore=shutil.ignore_patterns(".chipgraph/state"))
    with (dest / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write(".chipgraph/state/\n")
    if not source:
        (dest / SRC).unlink()
    if not generated:
        (dest / OUT).unlink()
    profile_path = dest / ".chipgraph.yml"
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    profile["packs"] = ["digital-rtl"]
    profile["runtime"] = runtime
    if not HAS_VERILATOR:
        profile["adapters"]["lint"] = PASS
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
    if commit:
        _git(dest, "init", "-q", "-b", "main")
        _git(dest, "config", "user.email", "m2-12@example.invalid")
        _git(dest, "config", "user.name", "m2-12")
        _git(dest, "add", "-A")
        _git(dest, "commit", "-q", "-m", "tinysoc with a wrapper")
    if ingest:
        from chipgraph.app.ingest import run_ingest

        run_ingest(AppContext.load(dest))
    return dest


def edit_profile(root: Path, change: Any) -> None:
    """Apply `change(profile_dict)` to the project's `.chipgraph.yml`."""
    path = root / ".chipgraph.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def tool_context(
    root: Path, runner: Any | None = None, params: Mapping[str, str] | None = None
) -> ToolContext:
    """A tool context (a real `LocalRunner` unless a runner is given); `params` is the
    sample wrapper's unless given (`{}` is a project-wide run)."""
    return ToolContext(
        repo_root=root,
        runner=runner or LocalRunner(),
        params=dict(PARAMS if params is None else params),
    )


# --- runners -----------------------------------------------------------------------------


@dataclass
class CannedRunner:
    """A `Runner` that never starts a process: it answers `emacs` commands from a script.

    `probe` is the answer to the version probe; `expand` is the text emacs "writes" into the
    scratch copy (None: emacs fails with `fail_log`). Every command is remembered.
    """

    expanded: str | None = None
    probe_code: int = 0
    probe_out: str = "chipgraph-emacs 31.1|2026-02-02\n"
    probe_err: str = ""
    fail_log: str = ""
    fail_code: int = 255
    timed_out: bool = False
    name: str = "canned"
    commands: list[list[str]] = field(default_factory=list)
    missing: bool = False

    async def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> RunResult:
        self.commands.append(list(cmd))
        if self.missing:
            raise FileNotFoundError(cmd[0])
        if "-f" not in cmd:  # the probe
            return RunResult(
                returncode=self.probe_code,
                stdout=self.probe_out,
                stderr=self.probe_err,
                duration_s=0.0,
            )
        if self.timed_out:
            return RunResult(returncode=-9, stdout="", stderr="", duration_s=1.0, timed_out=True)
        target = Path(cmd[cmd.index("-f") - 1])
        assert target.parent == cwd, "emacs must run on a scratch copy, in the scratch dir"
        if self.expanded is None:
            return RunResult(
                returncode=self.fail_code, stdout="", stderr=self.fail_log, duration_s=0.0
            )
        target.write_text(self.expanded, encoding="utf-8")
        return RunResult(returncode=0, stdout="", stderr="", duration_s=0.0)


# --- a scripted author --------------------------------------------------------------------


@dataclass
class WrapperRuntime:
    """A scripted in-process `AgentRuntime` for a wrapper task: call n creates the task's
    output with `texts[n]` (the last repeats), overwriting. With `create_only` it behaves
    like Claude Code for an agent that cannot read: a file already there is a
    `PermissionError`."""

    root: Path
    texts: list[str]
    name: str = "generic"
    create_only: bool = False
    tasks: list[AgentTask] = field(default_factory=list)

    async def run_task(self, task: AgentTask) -> AgentResult:
        text = self.texts[min(len(self.tasks), len(self.texts) - 1)]
        self.tasks.append(task)
        for rel in task.allowed_writes:
            path = self.root / rel
            if self.create_only and path.exists():
                raise PermissionError("File has not been read yet. Read it first.")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return AgentResult(status="done", files_written=task.allowed_writes)


# --- the Claude Code runtime's tools -------------------------------------------------------


def next_task(root: Path, target: str = AUTHOR) -> dict[str, Any]:
    return asyncio.run(service.next_task(AppContext.load(root), target))


def get_context(root: Path, task_id: str = AUTHOR) -> dict[str, Any]:
    return asyncio.run(service.get_context(AppContext.load(root), task_id))


def submit(root: Path, task_id: str = AUTHOR, **report: Any) -> dict[str, Any]:
    return asyncio.run(
        service.submit(AppContext.load(root), task_id, service.SubmitReport(**report))
    )


def create(root: Path, rel: str, text: str) -> None:
    """Claude Code's rule for an agent that cannot Read: a Write may only create."""
    path = root / rel
    if path.exists():
        raise PermissionError("File has not been read yet. Read it first before writing to it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
