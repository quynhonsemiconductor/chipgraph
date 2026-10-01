"""M1-14: the deterministic triage rules and the log parser, on generic logs (no model).

The logs here are written for these tests with made-up names (`src/core/alu.sv`,
`verif/alu_tb.sv`, ...), not taken from the sample set in `evals/triage/`: a rule that
only works on the samples would fail here. The last test checks that no rule names a
sample or a file of the sample set (anti-overfitting).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from chipgraph.packs.assist.triage import (
    RULES,
    TriageFacts,
    evaluate,
    is_rtl_path,
    is_tb_path,
    parse_log,
)
from chipgraph.packs.assist.triage.rules import tb_patterns

REPO = Path(__file__).resolve().parents[3]
TRIAGE_PKG = REPO / "src" / "chipgraph" / "packs" / "assist" / "triage"
EVALS = REPO / "evals" / "triage"


def _hit(log: str, **kw: object) -> tuple[str | None, str | None]:
    facts = TriageFacts(parsed=parse_log(log), **kw)  # type: ignore[arg-type]
    hit = evaluate(facts)
    return (hit.label, hit.rule) if hit is not None else (None, None)


# --- infra ------------------------------------------------------------------------------

INFRA_LOGS = {
    "bash_command_not_found": (
        "vcs -full64 -f files.f\nbash: line 1: vcs: command not found\n",
        "tool_missing",
    ),
    "sh_not_found": ("/bin/sh: 1: xrun: not found\nmake: *** [sim] Error 127\n", "tool_missing"),
    "make_exec_missing": (
        "iverilog -o build/a.out -c files.f\nmake: iverilog: No such file or directory\n"
        "make: *** [build] Error 1\n",
        "tool_missing",
    ),
    "gnu_make_127": ("make: *** [Makefile:12: lint] Error 127\n", "tool_missing"),
    "missing_filelist_entry": (
        "%Error: Cannot find file containing module: 'src/core/old/alu.sv'\n"
        "%Error: Exiting due to 1 error(s)\n",
        "input_missing",
    ),
    "file_not_found": ("%Error: File not found: src/core/alu_pkg.sv\n", "input_missing"),
    "permission_denied": (
        "slang: error: src/core/alu.sv: Permission denied\n",
        "input_missing",
    ),
    "include_missing": (
        "%Error: src/core/alu.sv:3:10: Cannot find include file: 'alu_defs.svh'\n",
        "input_missing",
    ),
    "no_make_target": ("make: *** No rule to make target 'sim'.  Stop.\n", "make_target_missing"),
    "killed": ("running regression...\nKilled\n", "time_limit"),
    "timed_out": ("error: the job timed out after 3600 s\n", "time_limit"),
    "licence": (
        "Error: FlexNet licence checkout failed for feature VCSCompiler_Net\n",
        "tool_failure",
    ),
    "segfault": ("verilator: Segmentation fault (core dumped)\n", "tool_failure"),
    "check_could_not_run": (
        "ERROR  lint alu 1 issues\n  -:-  [] command not found: make lint BLOCK=alu\n",
        "check_could_not_run",
    ),
}


@pytest.mark.parametrize("name", sorted(INFRA_LOGS))
def test_infra_rules(name: str) -> None:
    log, rule = INFRA_LOGS[name]
    assert _hit(log) == ("infra", rule)


def test_a_spec_check_that_could_not_run_is_infra() -> None:
    log = (
        "ERROR  ports_diff alu 1 issues\n"
        "  -:-  [model] no Design Model at .x/model.db; run `chipgraph ingest` first\n"
    )
    assert _hit(log) == ("infra", "check_could_not_run")


def test_a_testbench_timeout_message_is_not_infra() -> None:
    log = (
        "tb: waiting for irq\nFAIL timeout waiting for irq: expected 0x1 got 0x0\n"
        "%Error: verif/alu_tb.sv:80: Verilog $stop\n"
    )
    assert _hit(log) == (None, None)


# --- spec -------------------------------------------------------------------------------


def test_a_failing_spec_cross_check_is_spec() -> None:
    log = (
        "FAIL   ports_diff alu 1 issues\n"
        "  docs/alu_spec.md:40  [ports_diff.width] port 'a' of IP 'alu': spec width 16 "
        "but RTL width 8\n"
    )
    assert _hit(log) == ("spec", "spec_cross_check")


@pytest.mark.parametrize("check", ["spec_schema", "trace", "cross_chip"])
def test_every_spec_cross_check_kind(check: str) -> None:
    log = f"FAIL   {check} - 1 issues\n  contract.yml:12  [x.y] something disagrees\n"
    assert _hit(log) == ("spec", "spec_cross_check")


def test_a_renamed_spec_check_is_found_through_its_adapter() -> None:
    log = "FAIL   reqs alu 1 issues\n  docs/alu_spec.md:9  [req.no_test] no test for REQ-1\n"
    assert _hit(log) == (None, None)
    assert _hit(log, check_kinds=(("reqs", "trace"),)) == ("spec", "spec_cross_check")


def test_a_spec_check_from_json() -> None:
    result = {
        "check_id": "spec_schema",
        "status": "fail",
        "issues": [{"file": "docs/alu_spec.md", "line": 7, "rule": "r", "msg": "no access"}],
        "duration_s": 0.1,
        "idempotency_key": "k",
    }
    assert _hit(json.dumps([result])) == ("spec", "spec_cross_check")
    assert _hit(json.dumps({"ok": False, "results": [result]})) == ("spec", "spec_cross_check")


def test_a_non_spec_check_is_not_spec() -> None:
    log = "FAIL   naming alu 1 issues\n  src/core/alu.sv:3  [naming.module] bad name 'ALU'\n"
    assert _hit(log) == ("rtl", "rtl_lint")


# --- tb and rtl ---------------------------------------------------------------------------


def test_lint_errors_in_rtl_sources_are_rtl() -> None:
    log = (
        "verilator --lint-only -Wall -f files.f\n"
        "%Warning-UNUSEDSIGNAL: src/core/alu.sv:12:16: Signal is not used: 'carry'\n"
        "%Error: src/core/alu.sv:30:5: Can't find definition of variable: 'sumx'\n"
        "%Error: Exiting due to 1 error(s)\n"
    )
    assert _hit(log) == ("rtl", "rtl_lint")


def test_compile_errors_in_testbenches_are_tb() -> None:
    log = (
        "%Error: verif/alu_tb.sv:44:3: syntax error, unexpected end\n"
        "%Error-PINNOTFOUND: verif/alu_tb.sv:20:8: Pin not found: 'cin_x'\n"
    )
    assert _hit(log) == ("tb", "testbench_compile")


def test_other_tool_formats() -> None:
    assert _hit("tests/test_alu.sv:12: syntax error\n") == ("tb", "testbench_compile")
    assert _hit("src/core/alu.sv:7: error: Unknown module type: addr\n") == ("rtl", "rtl_lint")


def test_the_profile_layout_names_testbench_paths() -> None:
    log = "%Error: bench/alu/top.sv:5:1: syntax error, unexpected endmodule\n"
    assert _hit(log) == ("rtl", "rtl_lint")
    patterns = tb_patterns({"tb": "bench/{block}"})
    assert _hit(log, tb_patterns=patterns) == ("tb", "testbench_compile")


def test_errors_in_both_rtl_and_testbench_are_ambiguous() -> None:
    log = (
        "%Error: src/core/alu.sv:30:5: Can't find definition of variable: 'sumx'\n"
        "%Error: verif/alu_tb.sv:44:3: syntax error, unexpected end\n"
    )
    assert _hit(log) == (None, None)


def test_an_error_in_a_non_hdl_file_is_ambiguous() -> None:
    assert _hit("%Error: files.f:3:1: Unknown option\n") == (None, None)


# --- what goes to the model ------------------------------------------------------------------

SIM_MISMATCHES = {
    "verilator_self_check": (
        "tb: write addr=0x1 data=0x5\n"
        "FAIL ctrl readback: expected 0x5 got 0x4\n"
        "[2000] %Fatal: alu_tb.sv:90: Assertion failed in alu_tb: 1 check(s) failed\n"
        "%Error: verif/alu_tb.sv:90: Verilog $stop\n"
    ),
    "uvm": (
        "UVM_INFO @ 0: reporter [RNTST] Running test alu_test...\n"
        "UVM_ERROR alu_scoreboard.sv(40) @ 500: uvm_test_top.env.sb [MISMATCH] "
        "expected 0x10 actual 0x11\n"
    ),
    "assertion": (
        "[1500] %Error: alu.sv:55: Assertion failed in TOP.alu: a_sum_ok\n"
        "%Error: src/core/alu.sv:55: Verilog $stop\n"
    ),
    "questa": (
        "# ** Error: data mismatch: expected 3 got 2\n"
        "#    Time: 120 ns  Iteration: 0  Instance: /alu_tb\n"
    ),
}


@pytest.mark.parametrize("name", sorted(SIM_MISMATCHES))
def test_no_rule_decides_a_simulation_mismatch(name: str) -> None:
    parsed = parse_log(SIM_MISMATCHES[name])
    assert parsed.sim_ran
    assert evaluate(TriageFacts(parsed=parsed)) is None


def test_no_rule_decides_an_empty_log() -> None:
    assert _hit("") == (None, None)
    assert _hit("make: *** [lint] Error 1\n") == (None, None)


def test_rules_return_none_rather_than_guess() -> None:
    parsed = parse_log("something went wrong\n")
    assert all(rule(TriageFacts(parsed=parsed)) is None for rule in RULES)


# --- paths and the parser ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "tb/top.sv",
        "dv/env/scoreboard.sv",
        "verif/alu_tb.sv",
        "src/tb_alu.sv",
        "tests/test_alu.py",
        "ip/alu/test/alu_test.sv",
    ],
)
def test_testbench_paths(path: str) -> None:
    assert is_tb_path(path)
    assert not is_rtl_path(path)


@pytest.mark.parametrize("path", ["src/core/alu.sv", "rtl/top.v", "design/uart/uart.vhd"])
def test_rtl_paths(path: str) -> None:
    assert is_rtl_path(path)
    assert not is_tb_path(path)


def test_parse_strips_ansi_and_keeps_check_issues() -> None:
    parsed = parse_log(
        "\x1b[31mFAIL\x1b[0m   trace alu 2 issues\n"
        "  docs/a.md:3  [req.no_test] no test for REQ-1\n"
        "  -:-  [model] could not read\n"
    )
    [check] = parsed.checks
    assert (check.check_id, check.block, check.status) == ("trace", "alu", "fail")
    assert [(i.file, i.line) for i in check.issues] == [("docs/a.md", 3), (None, None)]


def test_parse_separates_runtime_messages_from_compile_issues() -> None:
    parsed = parse_log(SIM_MISMATCHES["verilator_self_check"])
    assert parsed.tool_issues == ()
    assert [i.file for i in parsed.runtime_issues] == ["verif/alu_tb.sv"]
    assert parsed.sim_failures[0].startswith("FAIL ctrl readback")


def test_the_excerpt_drops_hints_and_cuts_long_logs() -> None:
    hint = "        ... For warning description see https://example.invalid/warn"
    lines = [f"info line {n}" for n in range(200)]
    lines[100] = "%Error: src/core/alu.sv:30:5: Can't find definition of variable: 'x'"
    lines[101] = hint
    parsed = parse_log("\n".join(lines))
    assert "Can't find definition" in parsed.excerpt
    assert "For warning description" not in parsed.excerpt
    assert len(parsed.excerpt.splitlines()) <= 62
    assert parsed.first_error.startswith("%Error: src/core/alu.sv:30:5")


# --- anti-overfitting -------------------------------------------------------------------------


def _sample_names() -> set[str]:
    """Every sample id, and every file name (and long stem) the sample set touches."""
    data = yaml.safe_load((EVALS / "faults.yml").read_text())
    names: set[str] = set()
    paths: set[str] = set()
    for sample in data["samples"]:
        names.add(sample["id"])
        paths.update(sample.get("tb") or [])
        fault = sample.get("fault") or {}
        paths.update(e["path"] for e in fault.get("edit") or [])
        paths.update(e["path"] for e in fault.get("chmod") or [])
        paths.update(fault.get("delete") or [])
        paths.update(fault.get("after_ingest_delete") or [])
        paths.update(re.findall(r"[\w./-]+\.(?:sv|f|md|yml)\b", sample["cmd"]))
    paths.update(p.name for p in (EVALS / "tb").iterdir())
    for path in paths:
        name = Path(path).name
        names.add(name)
        stem = name.split(".", 1)[0]
        if len(stem) >= 6:
            names.add(stem)
    return names


def test_no_rule_names_a_sample_or_its_files() -> None:
    names = _sample_names()
    assert "log-01" in names and "tb_tiny_gpio.sv" in names and "tiny_gpio" in names
    for source in sorted(TRIAGE_PKG.glob("*.py")):
        text = source.read_text().lower()
        found = sorted(n for n in names if n.lower() in text)
        assert not found, f"{source.name} names sample files or ids: {found}"
