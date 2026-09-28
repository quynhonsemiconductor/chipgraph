"""Tests for `CmdTool`, with a fake runner and with the real `LocalRunner`."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from chipgraph.adapters.parser.verilator import VerilatorParser
from chipgraph.adapters.runner.local import LocalRunner
from chipgraph.adapters.tool.cmd import CmdTool
from chipgraph.core.contracts import CheckResult, CheckSpec
from chipgraph.core.plugin_api.protocols import LogParser, Runner, ToolAdapter
from chipgraph.core.plugin_api.types import RunResult, ToolContext


class FakeRunner:
    """Fake `Runner`: records the last argv/cwd/env/timeout and returns a canned result."""

    name = "fake-runner"

    def __init__(self, result: RunResult) -> None:
        self._result = result
        self.calls: list[dict[str, object]] = []

    async def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> RunResult:
        self.calls.append(
            {"argv": tuple(cmd), "cwd": cwd, "env": dict(env or {}), "timeout_s": timeout_s}
        )
        return self._result


def _spec(**args: object) -> CheckSpec:
    return CheckSpec(id="pack/lint_block", capability="lint", adapter="cmd", args=args)


def _ctx(runner: Runner, tmp_path: Path, **params: str) -> ToolContext:
    return ToolContext(repo_root=tmp_path, runner=runner, params=params)


def _run(tool: CmdTool, spec: CheckSpec, ctx: ToolContext) -> CheckResult:
    return asyncio.run(tool.run(spec, ctx))


def test_is_a_tool_adapter() -> None:
    tool = CmdTool()
    assert isinstance(tool, ToolAdapter)
    assert tool.name == "cmd"
    assert tool.capability == "any"


def test_local_runner_is_a_runner() -> None:
    assert isinstance(LocalRunner(), Runner)


def test_builtin_parsers_are_log_parsers() -> None:
    assert isinstance(VerilatorParser(), LogParser)


def test_pass_with_no_parser(tmp_path: Path) -> None:
    runner = FakeRunner(RunResult(returncode=0, stdout="all good\n", stderr="", duration_s=0.1))
    spec = _spec(cmd="make lint BLOCK={block}")
    ctx = _ctx(runner, tmp_path, block="rom")
    result = _run(CmdTool(), spec, ctx)

    assert result.status == "pass"
    assert result.issues == ()
    assert result.check_id == "pack/lint_block"
    assert runner.calls[0]["argv"] == ("make", "lint", "BLOCK=rom")
    assert runner.calls[0]["cwd"] == tmp_path


def test_fail_with_parser_issues(tmp_path: Path) -> None:
    log = "%Error-PINNOTFOUND: file.sv:5:2: Pin not found: 'x'\n"
    runner = FakeRunner(RunResult(returncode=0, stdout=log, stderr="", duration_s=0.1))
    spec = _spec(cmd="make lint BLOCK={block}", parser="verilator")
    ctx = _ctx(runner, tmp_path, block="rom")
    result = _run(CmdTool(), spec, ctx)

    assert result.status == "fail"
    assert len(result.issues) == 1
    assert result.issues[0].rule == "PINNOTFOUND"


def test_fail_returncode_no_parser_gets_synthetic_issue(tmp_path: Path) -> None:
    runner = FakeRunner(RunResult(returncode=2, stdout="", stderr="boom", duration_s=0.1))
    spec = _spec(cmd="make lint BLOCK={block}")
    ctx = _ctx(runner, tmp_path, block="rom")
    result = _run(CmdTool(), spec, ctx)

    assert result.status == "fail"
    assert len(result.issues) == 1
    assert "exited with 2" in result.issues[0].msg
    assert result.issues[0].severity == "error"


def test_fail_returncode_with_parser_issues_present_no_duplicate(tmp_path: Path) -> None:
    log = "%Warning-UNUSEDSIGNAL: file.sv:5:2: Signal is not used: 'w_x'\n"
    runner = FakeRunner(RunResult(returncode=1, stdout=log, stderr="", duration_s=0.1))
    spec = _spec(cmd="make lint BLOCK={block}", parser="verilator")
    ctx = _ctx(runner, tmp_path, block="rom")
    result = _run(CmdTool(), spec, ctx)

    assert result.status == "fail"
    assert len(result.issues) == 1
    assert result.issues[0].rule == "UNUSEDSIGNAL"


def test_pass_allows_custom_ok_returncodes(tmp_path: Path) -> None:
    runner = FakeRunner(RunResult(returncode=1, stdout="", stderr="", duration_s=0.1))
    spec = _spec(cmd="make lint BLOCK={block}", ok_returncodes=[0, 1])
    ctx = _ctx(runner, tmp_path, block="rom")
    result = _run(CmdTool(), spec, ctx)
    assert result.status == "pass"


def test_missing_param_is_error(tmp_path: Path) -> None:
    runner = FakeRunner(RunResult(returncode=0, stdout="", stderr="", duration_s=0.0))
    spec = _spec(cmd="make lint BLOCK={block}")
    ctx = _ctx(runner, tmp_path)  # no "block" param
    result = _run(CmdTool(), spec, ctx)

    assert result.status == "error"
    assert len(result.issues) == 1
    assert "block" in result.issues[0].msg
    assert runner.calls == []


def test_timeout_is_error(tmp_path: Path) -> None:
    runner = FakeRunner(
        RunResult(returncode=-9, stdout="partial", stderr="", duration_s=0.2, timed_out=True)
    )
    spec = _spec(cmd="make lint BLOCK={block}", timeout_s=0.2)
    ctx = _ctx(runner, tmp_path, block="rom")
    result = _run(CmdTool(), spec, ctx)

    assert result.status == "error"
    assert any("timed out" in issue.msg for issue in result.issues)
    assert runner.calls[0]["timeout_s"] == 0.2


def test_missing_executable_is_error(tmp_path: Path) -> None:
    runner = FakeRunner(RunResult(returncode=127, stdout="", stderr="not found", duration_s=0.0))
    spec = _spec(cmd="make lint BLOCK={block}")
    ctx = _ctx(runner, tmp_path, block="rom")
    result = _run(CmdTool(), spec, ctx)

    assert result.status == "error"
    assert any("not found" in issue.msg for issue in result.issues)


def test_list_form_cmd_no_shlex_split(tmp_path: Path) -> None:
    runner = FakeRunner(RunResult(returncode=0, stdout="", stderr="", duration_s=0.0))
    spec = _spec(cmd=["make", "lint", "BLOCK={block}"])
    ctx = _ctx(runner, tmp_path, block="a block with spaces")
    result = _run(CmdTool(), spec, ctx)

    assert result.status == "pass"
    assert runner.calls[0]["argv"] == ("make", "lint", "BLOCK=a block with spaces")


def test_parser_selection_generic_regex(tmp_path: Path) -> None:
    log = "myrule: a.sv:3: something bad\n"
    runner = FakeRunner(RunResult(returncode=0, stdout=log, stderr="", duration_s=0.0))
    spec = _spec(
        cmd="make lint BLOCK={block}",
        regex=r"(?P<rule>\w+): (?P<file>\S+):(?P<line>\d+): (?P<msg>.*)",
    )
    ctx = _ctx(runner, tmp_path, block="rom")
    result = _run(CmdTool(), spec, ctx)

    assert result.status == "fail"
    assert result.issues[0].file == "a.sv"
    assert result.issues[0].rule == "myrule"


def test_unknown_parser_name_is_error(tmp_path: Path) -> None:
    runner = FakeRunner(RunResult(returncode=0, stdout="", stderr="", duration_s=0.0))
    spec = _spec(cmd="make lint BLOCK={block}", parser="does-not-exist")
    ctx = _ctx(runner, tmp_path, block="rom")
    result = _run(CmdTool(), spec, ctx)
    assert result.status == "error"


def test_log_tail_is_truncated_to_4000_chars(tmp_path: Path) -> None:
    big = "x" * 5000
    runner = FakeRunner(RunResult(returncode=0, stdout=big, stderr="", duration_s=0.0))
    spec = _spec(cmd="make lint BLOCK={block}")
    ctx = _ctx(runner, tmp_path, block="rom")
    result = _run(CmdTool(), spec, ctx)
    assert len(result.log_tail) <= 4000


def test_cwd_is_resolved_relative_to_repo_root(tmp_path: Path) -> None:
    runner = FakeRunner(RunResult(returncode=0, stdout="", stderr="", duration_s=0.0))
    spec = _spec(cmd="make lint BLOCK={block}", cwd="design/rom")
    ctx = _ctx(runner, tmp_path, block="rom")
    _run(CmdTool(), spec, ctx)
    assert runner.calls[0]["cwd"] == tmp_path / "design/rom"


def test_idempotency_key_stable_and_sensitive_to_params(tmp_path: Path) -> None:
    runner = FakeRunner(RunResult(returncode=0, stdout="", stderr="", duration_s=0.0))
    spec = _spec(cmd="make lint BLOCK={block}")
    ctx1 = _ctx(runner, tmp_path, block="rom")
    ctx2 = _ctx(runner, tmp_path, block="rom")
    ctx3 = _ctx(runner, tmp_path, block="scrc")

    result1 = _run(CmdTool(), spec, ctx1)
    result2 = _run(CmdTool(), spec, ctx2)
    result3 = _run(CmdTool(), spec, ctx3)

    assert result1.idempotency_key == result2.idempotency_key
    assert result1.idempotency_key != result3.idempotency_key
    assert result1.idempotency_key == CmdTool.key_for(spec, ctx1)


def test_idempotency_key_sensitive_to_env(tmp_path: Path) -> None:
    runner = FakeRunner(RunResult(returncode=0, stdout="", stderr="", duration_s=0.0))
    spec = _spec(cmd="make lint BLOCK={block}")
    ctx1 = ToolContext(repo_root=tmp_path, runner=runner, env={"A": "1"}, params={"block": "rom"})
    ctx2 = ToolContext(repo_root=tmp_path, runner=runner, env={"A": "2"}, params={"block": "rom"})
    assert CmdTool.key_for(spec, ctx1) != CmdTool.key_for(spec, ctx2)


# --- real LocalRunner integration tests -------------------------------------------------


def test_pass_with_real_local_runner(tmp_path: Path) -> None:
    spec = _spec(cmd=[sys.executable, "-c", "print('hello {block}')"])
    ctx = ToolContext(repo_root=tmp_path, runner=LocalRunner(), params={"block": "rom"})
    result = _run(CmdTool(), spec, ctx)
    assert result.status == "pass"
    assert "hello rom" in result.log_tail


def test_fail_with_real_local_runner(tmp_path: Path) -> None:
    spec = _spec(cmd=[sys.executable, "-c", "import sys; sys.exit(1)"])
    ctx = ToolContext(repo_root=tmp_path, runner=LocalRunner(), params={})
    result = _run(CmdTool(), spec, ctx)
    assert result.status == "fail"


def test_timeout_with_real_local_runner(tmp_path: Path) -> None:
    spec = _spec(cmd=[sys.executable, "-c", "import time; time.sleep(5)"], timeout_s=0.2)
    ctx = ToolContext(repo_root=tmp_path, runner=LocalRunner(), params={})
    result = _run(CmdTool(), spec, ctx)
    assert result.status == "error"
    assert any("timed out" in issue.msg for issue in result.issues)


def test_missing_executable_with_real_local_runner(tmp_path: Path) -> None:
    spec = _spec(cmd=["chipgraph-definitely-not-a-real-executable-xyz"])
    ctx = ToolContext(repo_root=tmp_path, runner=LocalRunner(), params={})
    result = _run(CmdTool(), spec, ctx)
    assert result.status == "error"


def test_conflicting_parser_and_regex_is_error(tmp_path: Path) -> None:
    runner = FakeRunner(RunResult(returncode=0, stdout="", stderr="", duration_s=0.0))
    spec = _spec(
        cmd="make lint BLOCK={block}",
        parser="verilator",
        regex=r"(?P<file>\S+):(?P<line>\d+): (?P<msg>.*)",
    )
    ctx = _ctx(runner, tmp_path, block="rom")
    result = _run(CmdTool(), spec, ctx)
    assert result.status == "error"


def test_missing_cmd_arg_is_error(tmp_path: Path) -> None:
    runner = FakeRunner(RunResult(returncode=0, stdout="", stderr="", duration_s=0.0))
    spec = _spec()
    ctx = _ctx(runner, tmp_path)
    result = _run(CmdTool(), spec, ctx)
    assert result.status == "error"


def test_invalid_cmd_type_is_error(tmp_path: Path) -> None:
    runner = FakeRunner(RunResult(returncode=0, stdout="", stderr="", duration_s=0.0))
    spec = _spec(cmd=42)
    ctx = _ctx(runner, tmp_path)
    result = _run(CmdTool(), spec, ctx)
    assert result.status == "error"
