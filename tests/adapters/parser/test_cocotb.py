"""Tests for `CocotbParser`: cocotb `results.xml` + simulator logs -> `Issue`s and a status.

The fixtures under `logs/cocotb/` are real outputs of the S3 harness (docs/spikes/s3,
cocotb 2.1.0, Verilator 5.052, Icarus 13.0, macOS), with machine paths replaced by
`/work/chipgraph`, `/work/sim/<case>`, `/venv` and `/python`. `S3_CASES` is the S3
harness's 20-case matrix (`run_all.py`), each case mapped onto the fixture its run wrote,
with the outcome S3 expected.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from chipgraph.adapters.parser.cocotb import (
    NO_RESULTS_RULE,
    CocotbParser,
    build_issues,
    parse_icarus_log,
)
from chipgraph.core.contracts import CheckResult
from chipgraph.core.plugin_api import Registry
from chipgraph.core.plugin_api.protocols import LogParser

LOGS = Path(__file__).parent / "logs" / "cocotb"
TB = "/work/chipgraph/docs/spikes/s3/gpio_tb.py"


def _text(name: str) -> str:
    return (LOGS / name).read_text()


@dataclass(frozen=True)
class S3Case:
    name: str
    tool: str
    results: str | None  # fixture copied to results.xml, or None (cocotb wrote none)
    build_log: str | None
    run_log: str | None
    build_rc: int
    status: str
    rule: str | None  # rule of the first issue
    where: str | None = None  # "<basename>:<line>" of the first issue


def _s3_cases() -> list[S3Case]:
    cases: list[S3Case] = []
    for tool in ("verilator", "icarus"):
        build = (None, f"{tool}_build_error.log", None, 2, "error", f"{tool}/error", "broken.sv:2")
        assert_ = (f"{tool}_assert.xml", None, None, 0, "fail", "cocotb/AssertionError")
        assert_ = (*assert_, "gpio_tb.py:72")
        passed = (f"{tool}_pass.xml", None, None, 0, "pass", None, None)
        cases += [
            S3Case(f"a-runner-{tool}-pass", tool, *passed),
            S3Case(f"a-runner-{tool}-assert", tool, *assert_),
            S3Case(f"a-runner-{tool}-build", tool, *build),
            S3Case(f"bc-edalize-{tool}-pass", tool, *passed),
            S3Case(f"bc-edalize-{tool}-assert", tool, *assert_),
            S3Case(
                f"bc-edalize-{tool}-crash",
                tool,
                f"{tool}_crash.xml",
                None,
                None,
                0,
                "fail",
                "cocotb/AttributeError",
                "gpio_tb.py:82",
            ),
            S3Case(
                f"bc-edalize-{tool}-import",
                tool,
                None,
                None,
                f"{tool}_import_error.log",
                0,
                "error",
                NO_RESULTS_RULE,
            ),
            S3Case(f"bc-edalize-{tool}-build", tool, *build),
            S3Case(f"bc-edalize-{tool}-waves", tool, *passed),
            S3Case(
                f"bc-edalize-{tool}-own-run",
                tool,
                None,
                None,
                f"{tool}_no_gpi_users.log",
                0,
                "error",
                NO_RESULTS_RULE,
            ),
        ]
    return cases


S3_CASES = _s3_cases()


def test_the_s3_matrix_has_its_20_cases() -> None:
    assert len(S3_CASES) == 20
    assert len({c.name for c in S3_CASES}) == 20


@pytest.mark.parametrize("case", S3_CASES, ids=lambda c: c.name)
def test_s3_case_outcome(case: S3Case, tmp_path: Path) -> None:
    results = tmp_path / "results.xml"
    if case.results is not None:
        results.write_text(_text(case.results))
    outcome = CocotbParser().parse_run(
        results,
        simulator=case.tool,  # type: ignore[arg-type]
        build_rc=case.build_rc,
        build_log=_text(case.build_log) if case.build_log else "",
        run_rc=None if case.build_rc else 0,
        run_log=_text(case.run_log) if case.run_log else "",
    )
    assert outcome.status == case.status
    first = outcome.issues[0] if outcome.issues else None
    assert (first.rule if first else None) == case.rule
    if case.where is not None:
        assert first is not None and first.file is not None
        assert f"{Path(first.file).name}:{first.line}" == case.where
    if case.status != "pass":
        assert any(i.severity == "error" for i in outcome.issues)


# --- results.xml ---------------------------------------------------------------------


def test_assertion_failure_points_at_the_assert_line_in_the_test_file() -> None:
    outcome = CocotbParser().parse_results(_text("verilator_assert.xml"))
    assert outcome.status == "fail"
    assert [t.outcome for t in outcome.tests] == ["fail", "pass", "pass"]
    (issue,) = outcome.issues
    assert issue.rule == "cocotb/AssertionError"
    assert (issue.file, issue.line) == (TB, 72)  # the assert, not the `def` (line 65)
    assert issue.severity == "error"
    assert issue.msg.startswith("gpio_tb.test_data_out_readback: DATA_OUT readback: got 0xa5")


def test_exception_uses_the_last_frame_in_the_test_file_not_cocotb_internals() -> None:
    outcome = CocotbParser().parse_results(_text("icarus_crash.xml"))
    (issue,) = outcome.issues
    # The traceback's last frame is cocotb/handle.py; the test's own frame is line 82.
    assert issue.rule == "cocotb/AttributeError"
    assert (issue.file, issue.line) == (TB, 82)
    assert "no child object named no_such_signal" in issue.msg


def test_passing_tests_produce_no_issue() -> None:
    outcome = CocotbParser().parse_results(_text("icarus_pass.xml"))
    assert outcome.status == "pass"
    assert outcome.issues == ()
    assert len(outcome.tests) == 3
    assert outcome.summary == "tests=3 pass=3 fail=0 skipped=0"


def test_error_element_is_a_failure_and_def_line_is_the_fallback() -> None:
    xml = """<testsuites><testsuite name="m"><testcase classname="m" name="t">
      <error type="RuntimeError" message="boom">no traceback here</error>
      <properties><property name="file" value="/w/m.py" /><property name="line" value="9" />
      </properties></testcase></testsuite></testsuites>"""
    (issue,) = CocotbParser().parse_results(xml).issues
    assert (issue.rule, issue.file, issue.line) == ("cocotb/RuntimeError", "/w/m.py", 9)
    assert issue.msg == "m.t: boom"


def test_skipped_is_info_and_never_makes_a_pass_out_of_nothing() -> None:
    one_ran = """<testsuites><testsuite><testcase classname="m" name="a" />
      <testcase classname="m" name="b"><skipped /></testcase></testsuite></testsuites>"""
    outcome = CocotbParser().parse_results(one_ran)
    assert outcome.status == "pass"
    assert [(i.rule, i.severity) for i in outcome.issues] == [("cocotb/skipped", "info")]

    all_skipped = """<testsuites><testsuite>
      <testcase classname="m" name="b"><skipped /></testcase></testsuite></testsuites>"""
    outcome = CocotbParser().parse_results(all_skipped)
    assert outcome.status == "error"
    assert [i.rule for i in outcome.issues] == ["cocotb/skipped", NO_RESULTS_RULE]


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   \n",
        "<testsuites><testsuite",
        "not xml at all",
        "<testsuites><testsuite name='m' tests='0'/></testsuites>",
        "<testsuites/>",
    ],
    ids=["missing", "empty", "blank", "truncated", "garbage", "zero-tests", "no-suite"],
)
def test_no_usable_results_is_an_error_never_a_pass(text: str | None) -> None:
    outcome = CocotbParser().parse_results(text)
    assert outcome.status == "error"
    assert [i.rule for i in outcome.issues] == [NO_RESULTS_RULE]
    assert outcome.issues[0].severity == "error"


def test_no_results_reason_comes_from_the_run_log() -> None:
    parser = CocotbParser()
    imp = parser.parse_results(None, run_log=_text("verilator_import_error.log"))
    assert "ImportError: S3_BREAK=import" in imp.issues[0].msg
    gpi = parser.parse_results(None, run_log=_text("icarus_no_gpi_users.log"))
    assert "gpi: No GPI_USERS specified, exiting..." in gpi.issues[0].msg


# --- LogParser view ------------------------------------------------------------------


def test_is_a_log_parser_and_is_registered() -> None:
    assert isinstance(CocotbParser(), LogParser)
    registry = Registry()
    registry.discover()
    assert isinstance(registry.get("parser", "cocotb"), CocotbParser)


def test_parse_reads_results_xml_text() -> None:
    issues = CocotbParser().parse(_text("verilator_crash.xml"))
    assert [i.rule for i in issues] == ["cocotb/AttributeError"]
    assert CocotbParser().parse(_text("verilator_pass.xml")) == ()


def test_parse_of_nothing_yields_an_error_issue_so_no_caller_reads_a_pass() -> None:
    for text in ("", "<testsuites/>", "garbage"):
        issues = CocotbParser().parse(text)
        assert [(i.rule, i.severity) for i in issues] == [(NO_RESULTS_RULE, "error")]
        with pytest.raises(ValueError, match="must not have any issue of severity 'error'"):
            CheckResult(
                check_id="sim", status="pass", issues=issues, duration_s=0, idempotency_key="k"
            )


# --- build logs, run exit codes, timeouts -------------------------------------------


def test_verilator_build_error_reuses_the_verilator_parser() -> None:
    issues = build_issues(_text("verilator_build_error.log"), "verilator")
    assert [(i.rule, Path(i.file or "").name, i.line) for i in issues] == [
        ("verilator/error", "broken.sv", 2)
    ]
    assert "syntax error, unexpected ';'" in issues[0].msg


def test_icarus_build_error_lines() -> None:
    issues = parse_icarus_log(_text("icarus_build_error.log"))
    assert [(i.rule, Path(i.file or "").name, i.line, i.severity) for i in issues] == [
        ("icarus/error", "broken.sv", 2, "error"),
        ("icarus/error", "broken.sv", 2, "error"),
    ]
    assert issues[1].msg == "Syntax error in continuous assignment"


def test_icarus_sorry_and_warning_are_warnings() -> None:
    log = (
        "/w/a.sv:34: vvp.tgt sorry: Case unique/unique0 qualities are ignored.\n"
        "/w/a.sv:7: warning: Port 3 (x) of m expects 8 bits, got 1.\n"
        "/w/a.sv:9: sorry: constant selects in always_* processes are not supported.\n"
    )
    issues = parse_icarus_log(log)
    assert [(i.rule, i.line, i.severity) for i in issues] == [
        ("icarus/sorry", 34, "warning"),
        ("icarus/warning", 7, "warning"),
        ("icarus/sorry", 9, "warning"),
    ]


def test_unrecognised_build_failure_is_still_an_error() -> None:
    outcome = CocotbParser().parse_run(
        None, simulator="verilator", build_rc=2, build_log="g++: fatal error: no input\nmake: ***"
    )
    assert outcome.status == "error"
    assert [i.rule for i in outcome.issues] == ["sim/build"]
    assert "g++: fatal error" in outcome.issues[0].msg


def test_build_failure_is_an_error_even_with_a_results_file(tmp_path: Path) -> None:
    results = tmp_path / "results.xml"
    results.write_text(_text("verilator_pass.xml"))
    outcome = CocotbParser().parse_run(results, simulator="verilator", build_rc=2)
    assert outcome.status == "error"


def test_run_not_started_is_an_error(tmp_path: Path) -> None:
    outcome = CocotbParser().parse_run(tmp_path / "results.xml", simulator="icarus", build_rc=0)
    assert outcome.status == "error"
    assert outcome.issues[0].rule == NO_RESULTS_RULE


def test_all_pass_but_nonzero_exit_is_an_error(tmp_path: Path) -> None:
    results = tmp_path / "results.xml"
    results.write_text(_text("icarus_pass.xml"))
    outcome = CocotbParser().parse_run(results, simulator="icarus", build_rc=0, run_rc=139)
    assert outcome.status == "error"
    assert [i.rule for i in outcome.issues] == ["sim/exit"]


def test_timeouts_are_errors(tmp_path: Path) -> None:
    results = tmp_path / "results.xml"
    results.write_text(_text("icarus_pass.xml"))
    parser = CocotbParser()
    run = parser.parse_run(results, simulator="icarus", build_rc=0, run_rc=-9, run_timed_out=True)
    assert run.status == "error"
    assert "sim/timeout" in [i.rule for i in run.issues]
    build = parser.parse_run(results, simulator="verilator", build_rc=-9, build_timed_out=True)
    assert build.status == "error"
    assert "sim/timeout" in [i.rule for i in build.issues]


def test_relativize_maps_issue_files(tmp_path: Path) -> None:
    results = tmp_path / "results.xml"
    results.write_text(_text("verilator_assert.xml"))
    outcome = CocotbParser().parse_run(
        results,
        simulator="verilator",
        build_rc=0,
        run_rc=0,
        relativize=lambda p: p.removeprefix("/work/chipgraph/"),
    )
    assert outcome.issues[0].file == "docs/spikes/s3/gpio_tb.py"
